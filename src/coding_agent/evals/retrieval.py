"""Retrieval eval: does code search put the right file in front of the agent?

Labelled questions (evals/retrieval/queries.yaml) are run against the same `CodeIndex` the
agents use. Metrics per mode (BM25 only; hybrid BM25 + embeddings when enabled):

- recall@1, recall@5  share of questions with a result from an expected file in the top 1 / top 5
- MRR@10              mean of 1/rank of the first such result (0 if not in the top 10)
- symbol@5            stricter: the top-5 chunk must also define the expected symbol (when given)

File level is the headline: the agent reads the whole file once search points it there. Symbol
level shows how precise the chunking is (a long function split into chunks can miss it).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from coding_agent.evals.tasks import EVALS_DIR
from coding_agent.retrieval.search import CodeIndex, Hit

TOP_K = 10


@dataclass(frozen=True)
class Query:
    corpus: str
    query: str
    expect: tuple[str, ...]
    symbol: str | None = None


@dataclass
class QueryResult:
    corpus: str
    query: str
    rank: int | None  # 1-based rank of the first result from an expected file, in the top 10
    symbol_rank: int | None = None  # same, but the chunk must define the expected symbol
    top: list[str] = field(default_factory=list)  # top 3 results, for the report


@dataclass
class RetrievalReport:
    mode: str
    queries: int
    recall_at_1: float
    recall_at_5: float
    mrr_at_10: float
    symbol_at_5: float
    by_corpus: dict[str, dict[str, float]]
    results: list[QueryResult]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def corpus_root(name: str, evals_dir: Path = EVALS_DIR) -> Path:
    roots = {
        "calculator": evals_dir / "sample_repo",
        "shop": evals_dir / "fixtures" / "shop",
        "project": evals_dir.parent / "src" / "coding_agent",
    }
    if name not in roots:
        raise ValueError(f"unknown corpus {name!r}")
    return roots[name]


def load_queries(evals_dir: Path = EVALS_DIR) -> list[Query]:
    spec = yaml.safe_load((evals_dir / "retrieval" / "queries.yaml").read_text(encoding="utf-8"))
    queries = [
        Query(
            corpus=str(item["corpus"]),
            query=str(item["query"]),
            expect=tuple(item["expect"]),
            symbol=item.get("symbol"),
        )
        for item in spec["queries"]
    ]
    for q in queries:
        root = corpus_root(q.corpus, evals_dir)
        missing = [p for p in q.expect if not (root / p).exists()]
        if missing:
            raise ValueError(f"query {q.query!r}: expected file(s) not found: {missing}")
    return queries


def is_hit(hit: Hit, query: Query, *, symbol: bool = False) -> bool:
    if hit.chunk.path not in query.expect:
        return False
    return not symbol or query.symbol is None or query.symbol in hit.chunk.symbols


def first_rank(hits: list[Hit], query: Query, *, symbol: bool = False) -> int | None:
    return next((rank for rank, hit in enumerate(hits, start=1) if is_hit(hit, query, symbol=symbol)), None)


def _metrics(ranks: list[int | None]) -> dict[str, float]:
    n = len(ranks) or 1
    return {
        "recall_at_1": round(sum(1 for r in ranks if r == 1) / n, 3),
        "recall_at_5": round(sum(1 for r in ranks if r is not None and r <= 5) / n, 3),
        "mrr_at_10": round(sum(1 / r for r in ranks if r is not None) / n, 3),
    }


def evaluate(
    queries: list[Query], *, embed_model: Any | None = None, evals_dir: Path = EVALS_DIR
) -> RetrievalReport:
    indexes: dict[str, CodeIndex] = {}
    results: list[QueryResult] = []
    for q in queries:
        if q.corpus not in indexes:
            indexes[q.corpus] = CodeIndex.build(corpus_root(q.corpus, evals_dir), embed_model=embed_model)
        hits = indexes[q.corpus].search(q.query, k=TOP_K)
        results.append(
            QueryResult(
                corpus=q.corpus,
                query=q.query,
                rank=first_rank(hits, q),
                symbol_rank=first_rank(hits, q, symbol=True),
                top=[f"{h.chunk.path}:{h.chunk.start_line}" for h in hits[:3]],
            )
        )
    mode = next(iter(indexes.values())).mode if indexes else "empty"
    overall = _metrics([r.rank for r in results])
    by_corpus = {
        corpus: _metrics([r.rank for r in results if r.corpus == corpus])
        for corpus in sorted({r.corpus for r in results})
    }
    return RetrievalReport(
        mode=mode,
        queries=len(results),
        recall_at_1=overall["recall_at_1"],
        recall_at_5=overall["recall_at_5"],
        mrr_at_10=overall["mrr_at_10"],
        symbol_at_5=_metrics([r.symbol_rank for r in results])["recall_at_5"],
        by_corpus=by_corpus,
        results=results,
    )
