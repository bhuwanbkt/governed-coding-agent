# ADR 0009: Code retrieval with LlamaIndex: per-job hybrid index behind an MCP server

- **Status:** Accepted
- **Date:** 2026-10-20

## Context

Agents found code by listing files and reading them one by one, which wastes LLM calls and
context on larger repositories. We want agents to ask "where is X handled?" and get the few
relevant places, with file and line references.

## Decision

- **Chunking:** LlamaIndex `CodeSplitter` (tree-sitter) splits code along functions and classes;
  `SentenceSplitter` handles other text. Each chunk carries path, language, line range, defined
  symbols and identifier words (`divide_by_zero` → divide, by, zero).
- **What is never indexed:** `.git`, virtualenvs, vendored and build folders, binaries, files over
  200 KB and secret-looking files (`.env*`, keys, certificates).
- **Search:** BM25 (`llama-index-retrievers-bm25`) always; vector search with a local fastembed
  model (`BAAI/bge-small-en-v1.5`) when `RETRIEVAL_EMBEDDINGS=true`. Rankings are merged with
  reciprocal rank fusion, implemented directly (a few lines, no LLM involved).
- **Delivery:** a separate `code_index` MCP server exposes `build_index` and `search_codebase`.
  It runs in its own process with no tokens in its environment, and access is granted per role
  by the Toolbox like any other tool.
- **Lifetime:** built in memory per job, after the clone. Results describe the repository as
  cloned; agents use `read_file` for files they have edited.

## Consequences

- Embeddings are off in production at first: the model and ONNX runtime need more memory than
  the free instance may have. Turning them on is a configuration change; if the model can't load,
  search falls back to BM25 and the index summary says so.
- No vectors leave the server and no embedding API key is needed.
- Rebuilding per job is fine for small repositories. Persisting chunks and vectors in pgvector,
  keyed by repository and commit, is the follow-up when repositories or job volume grow (phase 6).
- A small retrieval eval (hit@3 on golden queries) runs in the unit tests and grows in phase 6.
