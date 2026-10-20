"""Code index MCP server: semantic + keyword search over the job's cloned repository.

Separate from the workspace server so the heavier retrieval dependencies (LlamaIndex, and the
embedding model when enabled) load in their own process, and so search is a capability that can
be granted or withheld per agent like any other tool. Read-only: it never changes files.

Environment:
    WORKSPACE_DIR          job workspace; the repository is at $WORKSPACE_DIR/repo
    RETRIEVAL_EMBEDDINGS   "true" for hybrid BM25 + embeddings, otherwise BM25 only
    EMBEDDING_MODEL        fastembed model name (default BAAI/bge-small-en-v1.5)
    EMBEDDING_CACHE_DIR    where the model is cached
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from coding_agent.retrieval.search import CodeIndex, format_hits, load_embed_model

logger = logging.getLogger(__name__)
server = MCPServer("code_index")

MAX_K = 15
_index: CodeIndex | None = None


def _repo() -> Path:
    repo = Path(os.environ.get("WORKSPACE_DIR", "workspaces/default")).resolve() / "repo"
    if not (repo / ".git").is_dir():
        raise ToolError("repository is not cloned yet; git_clone must succeed first")
    return repo


def _embeddings_enabled() -> bool:
    return os.environ.get("RETRIEVAL_EMBEDDINGS", "false").strip().lower() in {"1", "true", "yes", "on"}


def _build() -> CodeIndex:
    global _index
    embed_model = None
    note = ""
    if _embeddings_enabled():
        try:
            cache = os.environ.get("EMBEDDING_CACHE_DIR")
            embed_model = load_embed_model(
                os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
                Path(cache) if cache else None,
            )
        except Exception as exc:  # e.g. model download blocked: keyword search still works
            note = f" (embeddings unavailable: {type(exc).__name__})"
            logger.warning("embedding model unavailable", extra={"error": str(exc)})
    _index = CodeIndex.build(_repo(), embed_model=embed_model)
    _index.mode += note
    return _index


@server.tool()
def build_index() -> str:
    """(Re)build the search index for the cloned repository. Returns index statistics."""
    return _build().summary()


@server.tool()
def search_codebase(query: str, k: int = 6) -> str:
    """Find the code most relevant to a natural-language or keyword query, e.g.
    'where are calculator operations defined' or 'divide by zero'. Returns file paths, line
    ranges, defined symbols and the matching code. The index reflects the repository as cloned;
    use read_file for the current content of a file you have edited."""
    if not query.strip():
        raise ToolError("query must not be empty")
    index = _index or _build()
    return format_hits(index.search(query, k=max(1, min(k, MAX_K))))


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
