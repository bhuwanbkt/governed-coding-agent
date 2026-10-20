"""Turn a repository into searchable chunks.

- Picks source files and skips everything that should never be indexed: `.git`, virtualenvs,
  build output, binaries, very large files and anything that looks like a secret (`.env`, keys).
- Splits code by syntax with LlamaIndex's `CodeSplitter` (tree-sitter), so chunks follow
  functions and classes; other text uses `SentenceSplitter`. If a parser is unavailable the
  file falls back to the text splitter instead of failing the whole index.
- Attaches metadata to every chunk: path, language, line range and the symbols it defines.
  Search results can then point at `calculator.py:9-11 multiply` instead of a bare blob.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        "target",
    }
)
# Never index secrets, even if someone committed them.
SECRET_NAME = re.compile(
    r"(^\.env(\..*)?$)|(\.(pem|key|p12|pfx|jks|keystore)$)|(^id_(rsa|dsa|ecdsa|ed25519))|(^credentials)"
    r"|(secret)",
    re.IGNORECASE,
)
LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}
TEXT_EXTENSIONS = frozenset(
    {".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".sql", ".sh", ".html", ".css"}
)
MAX_FILE_BYTES = 200_000
MAX_FILES = 2_000

_SYMBOL_PATTERNS = {
    "python": re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)", re.MULTILINE),
    "default": re.compile(
        r"\b(?:function|class|interface|func|fn|def|type)\s+([A-Za-z_]\w*)|"
        r"\b(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(",
        re.MULTILINE,
    ),
}
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class Chunk:
    path: str
    language: str
    start_line: int
    end_line: int
    text: str
    symbols: tuple[str, ...] = ()
    splitter: str = "text"

    @property
    def id(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"

    @property
    def keywords(self) -> str:
        """Identifier parts, so a search for 'divide zero' also matches `divide_by_zero` or `divideByZero`."""
        words: set[str] = set()
        for name in (*self.symbols, *re.split(r"[/._-]", self.path)):
            for part in re.split(r"_+", _CAMEL.sub("_", name)):
                if len(part) > 1:
                    words.add(part.lower())
        return " ".join(sorted(words))


@dataclass
class IndexStats:
    files: int = 0
    chunks: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    splitters: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def language_for(path: Path) -> str | None:
    """Language name for code files, 'text' for indexable text files, None for everything else."""
    suffix = path.suffix.lower()
    if suffix in LANGUAGES:
        return LANGUAGES[suffix]
    if suffix in TEXT_EXTENSIONS or path.name in {"Dockerfile", "Makefile"}:
        return "text"
    return None


def iter_source_files(root: Path, stats: IndexStats | None = None) -> list[Path]:
    """Indexable files under `root`, sorted, with the reason every other file was skipped."""
    stats = stats if stats is not None else IndexStats()
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]) or not path.is_file() or path.is_symlink():
            continue
        if SECRET_NAME.search(path.name):
            stats.skip("secret-like name")
            continue
        if language_for(path) is None:
            stats.skip("unsupported type")
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            stats.skip("too large")
            continue
        with path.open("rb") as handle:
            if b"\x00" in handle.read(8192):
                stats.skip("binary")
                continue
        if len(found) >= MAX_FILES:
            stats.skip("file limit")
            continue
        found.append(path)
    return found


def symbols_in(text: str, language: str) -> tuple[str, ...]:
    if language == "text":
        return ()
    pattern = _SYMBOL_PATTERNS.get(language, _SYMBOL_PATTERNS["default"])
    names: list[str] = []
    for match in pattern.finditer(text):
        name = next((g for g in match.groups() if g), None)
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _code_splitter(language: str) -> Any:
    from llama_index.core.node_parser import CodeSplitter

    return CodeSplitter(language=language, chunk_lines=40, chunk_lines_overlap=10, max_chars=1500)


def _text_splitter() -> Any:
    from llama_index.core.node_parser import SentenceSplitter

    return SentenceSplitter(chunk_size=400, chunk_overlap=40)


def split_text(text: str, language: str) -> tuple[list[str], str]:
    """Split with the syntax-aware splitter when possible. Returns (pieces, splitter name)."""
    if language not in ("text", "default"):
        try:
            pieces = _code_splitter(language).split_text(text)
            return [p for p in pieces if p.strip()], "code"
        except Exception as exc:  # missing grammar, parse failure: degrade, don't fail the index
            logger.warning("code splitter unavailable, using text splitter", extra={"error": str(exc)})
    return [p for p in _text_splitter().split_text(text) if p.strip()], "text"


def line_span(content: str, piece: str, cursor: int) -> tuple[int, int, int]:
    """1-based (start, end) lines of `piece` inside `content`, searching from `cursor` first
    (chunks come in order but may overlap). Returns the new cursor too."""
    probe = piece.strip()
    index = content.find(probe, cursor)
    if index < 0:
        index = content.find(probe)
    if index < 0:  # splitter changed whitespace; anchor on the first non-empty line
        first = next((line.strip() for line in probe.splitlines() if line.strip()), "")
        index = max(content.find(first, cursor), 0)
    start = content.count("\n", 0, index) + 1
    end = start + probe.count("\n")
    return start, end, index + 1


def chunk_file(root: Path, path: Path) -> list[Chunk]:
    rel = path.relative_to(root).as_posix()
    language = language_for(path) or "text"
    content = path.read_text(encoding="utf-8", errors="replace")
    if not content.strip():
        return []
    pieces, splitter = split_text(content, language)
    chunks: list[Chunk] = []
    cursor = 0
    for piece in pieces:
        start, end, cursor = line_span(content, piece, cursor)
        chunks.append(
            Chunk(
                path=rel,
                language=language,
                start_line=start,
                end_line=end,
                text=piece.strip("\n"),
                symbols=symbols_in(piece, language),
                splitter=splitter,
            )
        )
    return chunks


def chunk_repository(root: Path) -> tuple[list[Chunk], IndexStats]:
    stats = IndexStats()
    chunks: list[Chunk] = []
    for path in iter_source_files(root, stats):
        file_chunks = chunk_file(root, path)
        stats.files += 1
        for chunk in file_chunks:
            stats.splitters[chunk.splitter] = stats.splitters.get(chunk.splitter, 0) + 1
        chunks.extend(file_chunks)
    stats.chunks = len(chunks)
    return chunks, stats
