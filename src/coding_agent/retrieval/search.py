"""Hybrid code search over one repository, built per job.

Two LlamaIndex retrievers rank the chunks independently and their rankings are merged with
reciprocal rank fusion (RRF):

- BM25 (`llama-index-retrievers-bm25`): exact identifiers and keywords, e.g. `divide`, `ValueError`.
- Vector search (`VectorStoreIndex` + a local fastembed model, optional): related wording, e.g.
  "handle division by zero" finding `raise ValueError("cannot divide by zero")`.

RRF only uses ranks, so the two very different score scales never need calibrating, and it's
done here in a few lines rather than through a component that would also pull in an LLM.
The index lives in memory for one job (repos the agent edits are small); persisting it in
pgvector per commit is a later optimisation (ADR 0009).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.retrieval.indexer import Chunk, IndexStats, chunk_repository

logger = logging.getLogger(__name__)

RRF_K = 60  # standard constant from the RRF paper; dampens the weight of the very top ranks
CANDIDATE_POOL = 20  # how many results each retriever contributes before fusion


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float
    sources: tuple[str, ...]  # which retrievers found it: "bm25", "vector"


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Merge ranked id lists: score(id) = sum over lists of 1 / (k + rank). Ties keep first-seen order."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def _to_node(chunk: Chunk) -> Any:
    from llama_index.core.schema import TextNode

    return TextNode(
        id_=chunk.id,
        text=chunk.text,
        metadata={
            "path": chunk.path,
            "language": chunk.language,
            "symbols": ", ".join(chunk.symbols),
            "keywords": chunk.keywords,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
        },
        # Path, symbols and identifier words help matching; line numbers would only add noise.
        excluded_embed_metadata_keys=["start_line", "end_line", "language"],
        excluded_llm_metadata_keys=["keywords"],
    )


def load_embed_model(model_name: str, cache_dir: Path | None = None) -> Any:
    """Local embedding model (ONNX via fastembed): free, no API key, code never leaves the server."""
    from llama_index.embeddings.fastembed import FastEmbedEmbedding

    options: dict[str, Any] = {"model_name": model_name}
    if cache_dir is not None:
        options["cache_dir"] = str(cache_dir)
    return FastEmbedEmbedding(**options)


class CodeIndex:
    def __init__(
        self,
        chunks: list[Chunk],
        stats: IndexStats,
        *,
        bm25: Any | None,
        vector: Any | None,
        mode: str,
        build_seconds: float,
    ) -> None:
        self.chunks = {c.id: c for c in chunks}
        self.stats = stats
        self._bm25 = bm25
        self._vector = vector
        self.mode = mode
        self.build_seconds = build_seconds

    @classmethod
    def build(cls, root: Path, *, embed_model: Any | None = None) -> CodeIndex:
        """Index `root`. Pass an embedding model for hybrid search; None means BM25 only."""
        started = time.perf_counter()
        chunks, stats = chunk_repository(root)
        bm25: Any = None
        vector: Any = None
        mode = "empty"
        if chunks:
            from llama_index.retrievers.bm25 import BM25Retriever

            nodes = [_to_node(c) for c in chunks]
            pool = min(CANDIDATE_POOL, len(nodes))
            bm25 = BM25Retriever.from_defaults(nodes=nodes, similarity_top_k=pool)
            mode = "bm25"
            if embed_model is not None:
                try:
                    from llama_index.core import VectorStoreIndex

                    index = VectorStoreIndex(nodes, embed_model=embed_model)
                    vector = index.as_retriever(similarity_top_k=pool)
                    mode = "hybrid"
                except Exception as exc:  # model download/memory problems: keep keyword search
                    logger.warning("vector index failed, using BM25 only", extra={"error": str(exc)})
        return cls(
            chunks,
            stats,
            bm25=bm25,
            vector=vector,
            mode=mode,
            build_seconds=time.perf_counter() - started,
        )

    def _ranked_ids(self, retriever: Any, query: str) -> list[str]:
        return [str(result.node.node_id) for result in retriever.retrieve(query)]

    def search(self, query: str, k: int = 6) -> list[Hit]:
        if self._bm25 is None or not query.strip():
            return []
        rankings: dict[str, list[str]] = {"bm25": self._ranked_ids(self._bm25, query)}
        if self._vector is not None:
            rankings["vector"] = self._ranked_ids(self._vector, query)
        fused = reciprocal_rank_fusion(list(rankings.values()))
        hits: list[Hit] = []
        for chunk_id, score in fused[: max(1, k)]:
            chunk = self.chunks.get(chunk_id)
            if chunk is not None:
                sources = tuple(name for name, ids in rankings.items() if chunk_id in ids)
                hits.append(Hit(chunk=chunk, score=score, sources=sources))
        return hits

    def summary(self) -> str:
        skipped = ", ".join(f"{n} {reason}" for reason, n in sorted(self.stats.skipped.items())) or "none"
        splitters = ", ".join(f"{n} {name}" for name, n in sorted(self.stats.splitters.items())) or "none"
        return (
            f"INDEXED files={self.stats.files} chunks={self.stats.chunks} mode={self.mode} "
            f"splitters=({splitters}) skipped=({skipped}) seconds={self.build_seconds:.2f}"
        )


MAX_SNIPPET_LINES = 30


def format_hits(hits: list[Hit]) -> str:
    """Compact, citeable results for an LLM: location, symbols, then the code."""
    if not hits:
        return "No matches. Try other words, or search_code for an exact text pattern."
    blocks: list[str] = []
    for number, hit in enumerate(hits, start=1):
        chunk = hit.chunk
        lines = chunk.text.splitlines()
        body = "\n".join(lines[:MAX_SNIPPET_LINES])
        if len(lines) > MAX_SNIPPET_LINES:
            body += f"\n... ({len(lines) - MAX_SNIPPET_LINES} more lines; use read_file)"
        symbols = f" [{', '.join(chunk.symbols)}]" if chunk.symbols else ""
        blocks.append(
            f"{number}. {chunk.path}:{chunk.start_line}-{chunk.end_line}{symbols} "
            f"(score {hit.score:.4f}, via {'+'.join(hit.sources)})\n{body}"
        )
    return "\n\n".join(blocks)
