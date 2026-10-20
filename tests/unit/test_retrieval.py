"""LlamaIndex retrieval: what gets indexed, how code is chunked, and whether search finds the
right code. The last test is a small retrieval evaluation on the sample repository; phase 6
grows it into the full eval suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.retrieval.indexer import (
    Chunk,
    IndexStats,
    chunk_file,
    iter_source_files,
    line_span,
    symbols_in,
)
from coding_agent.retrieval.search import CodeIndex, format_hits, reciprocal_rank_fusion

SAMPLE_REPO = Path(__file__).resolve().parents[2] / "evals" / "sample_repo"


def _write(root: Path, rel: str, content: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)


def test_secrets_vendored_and_binary_files_are_never_indexed(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def main() -> None:\n    pass\n")
    _write(tmp_path, ".env", "GROQ_API_KEY=not-a-real-key\n")
    _write(tmp_path, "certs/server.pem", "-----BEGIN-----\n")
    _write(tmp_path, ".venv/lib/site.py", "x = 1\n")
    _write(tmp_path, "node_modules/pkg/index.js", "module.exports = 1\n")
    _write(tmp_path, ".git/config", "[core]\n")
    _write(tmp_path, "logo.png", b"\x89PNG\x00\x00")
    _write(tmp_path, "data.json", "\x00binary")
    _write(tmp_path, "big.py", "x = 1\n" * 50_000)
    stats = IndexStats()

    files = [p.relative_to(tmp_path).as_posix() for p in iter_source_files(tmp_path, stats)]

    assert files == ["app.py"]
    assert stats.skipped == {"secret-like name": 2, "unsupported type": 1, "binary": 1, "too large": 1}


def test_python_is_split_by_syntax_with_line_numbers_and_symbols() -> None:
    chunks = chunk_file(SAMPLE_REPO, SAMPLE_REPO / "calculator.py")
    source = (SAMPLE_REPO / "calculator.py").read_text().splitlines()

    assert {c.splitter for c in chunks} == {"code"}
    multiply = next(c for c in chunks if "multiply" in c.symbols)
    # Small functions may share a chunk (up to 40 lines); the chunk must contain the definition
    # and its line range must point at exactly its text in the file.
    def_line = next(i for i, line in enumerate(source, start=1) if line.startswith("def multiply"))
    assert multiply.start_line <= def_line <= multiply.end_line
    assert "\n".join(source[multiply.start_line - 1 : multiply.end_line]).strip() == multiply.text.strip()
    assert multiply.id == f"calculator.py:{multiply.start_line}-{multiply.end_line}"


def test_identifier_keywords_split_snake_and_camel_case() -> None:
    chunk = Chunk(
        path="src/math_utils.py",
        language="python",
        start_line=1,
        end_line=2,
        text="",
        symbols=(
            "divide_by_zero",
            "safeDivide",
        ),
    )
    assert set(chunk.keywords.split()) >= {"divide", "by", "zero", "safe", "math", "utils", "src", "py"}


def test_symbols_by_language() -> None:
    assert symbols_in("class A:\n    async def run(self): ...\ndef helper(): ...", "python") == (
        "A",
        "run",
        "helper",
    )
    assert symbols_in("export function load() {}\nconst save = async () => {}", "typescript") == (
        "load",
        "save",
    )
    assert symbols_in("# def not_code", "text") == ()


def test_line_span_handles_overlapping_chunks() -> None:
    content = "a\nb\nc\nd\n"
    start, end, cursor = line_span(content, "b\nc", 0)
    assert (start, end) == (2, 3)
    assert line_span(content, "c\nd", cursor)[:2] == (3, 4)


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    fused = dict(reciprocal_rank_fusion([["a", "b", "c"], ["b", "c", "a"]]))
    assert max(fused, key=lambda k: fused[k]) == "b"  # ranked high by both
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)


def test_empty_repository_gives_a_clear_answer(tmp_path: Path) -> None:
    index = CodeIndex.build(tmp_path)
    assert index.mode == "empty"
    assert index.search("anything") == []
    assert "No matches" in format_hits([])


def test_hybrid_mode_with_an_embedding_model() -> None:
    from llama_index.core import MockEmbedding

    index = CodeIndex.build(SAMPLE_REPO, embed_model=MockEmbedding(embed_dim=8))
    assert index.mode == "hybrid"
    hits = index.search("multiply", k=3)
    assert hits
    assert any("vector" in h.sources for h in hits)


# Retrieval eval: (query, file that must appear in the top 3)
GOLDEN_QUERIES = [
    ("multiply two numbers", "calculator.py"),
    ("subtract", "calculator.py"),
    ("add function", "calculator.py"),
    ("tests for multiply", "tests/test_calculator.py"),
    ("example task: divide raising ValueError", "README.md"),
    ("project configuration pytest settings", "pyproject.toml"),
]


def test_retrieval_eval_hit_rate_at_3() -> None:
    index = CodeIndex.build(SAMPLE_REPO)
    hits = {query: [h.chunk.path for h in index.search(query, k=3)] for query, _ in GOLDEN_QUERIES}
    misses = [(q, expected, hits[q]) for q, expected in GOLDEN_QUERIES if expected not in hits[q]]
    hit_rate = 1 - len(misses) / len(GOLDEN_QUERIES)
    assert hit_rate >= 0.8, f"hit@3 = {hit_rate:.0%}; misses: {misses}"


def test_index_summary_reports_what_happened() -> None:
    summary = CodeIndex.build(SAMPLE_REPO).summary()
    assert summary.startswith("INDEXED files=")
    assert "mode=bm25" in summary
    assert "code" in summary  # python went through the syntax-aware splitter
