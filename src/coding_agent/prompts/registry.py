"""Prompt registry: prompts are versioned files, not strings buried in code.

Layout (inside this package):

    templates/<name>/v<N>.md              default template for every provider
    templates/<name>/v<N>.<provider>.md   optional provider-specific variant
    templates/partials/*.md               shared blocks pulled in with {% include %}

Each template starts with a front-matter block::

    ---
    name: coder
    version: 1
    description: ...
    ---

Rendering returns the text plus name, version and a content hash. Every LLM call logs these,
so any agent output can be traced back to the exact prompt that produced it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES_DIR = Path(__file__).parent / "templates"
_VERSION_FILE = re.compile(r"^v(?P<version>\d+)(?:\.(?P<provider>[a-z0-9_-]+))?\.md$")
_FRONT_MATTER = re.compile(r"\A---\n(?P<meta>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
_INCLUDE = re.compile(r"""{%-?\s*include\s+["']([^"']+)["']""")


class PromptNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class RenderedPrompt:
    name: str
    version: int
    provider_variant: str | None
    sha256: str
    content: str

    @property
    def ref(self) -> str:
        """Short identifier for logs and traces, e.g. `coder@v1#3f2a9c1d`."""
        return f"{self.name}@v{self.version}#{self.sha256[:8]}"


def _parse_front_matter(raw: str, path: Path) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER.match(raw)
    if not match:
        raise ValueError(f"{path} is missing a '---' front-matter block")
    meta: dict[str, str] = {}
    for line in match["meta"].splitlines():
        if line.strip():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, match["body"]


class PromptRegistry:
    def __init__(self, root: Path = TEMPLATES_DIR) -> None:
        self.root = root
        # Prompts are plain text for an LLM, not HTML, so autoescaping would corrupt them.
        self._env = Environment(  # noqa: S701
            loader=FileSystemLoader(root),
            undefined=StrictUndefined,  # a missing variable is an error, never a silent blank
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def versions(self, name: str) -> list[int]:
        folder = self.root / name
        found = {
            int(m["version"]) for p in folder.glob("v*.md") if (m := _VERSION_FILE.match(p.name)) is not None
        }
        if not found:
            raise PromptNotFoundError(f"no versions found for prompt {name!r}")
        return sorted(found)

    def _resolve(self, name: str, version: int | None, provider: str | None) -> tuple[Path, int, str | None]:
        version = version if version is not None else self.versions(name)[-1]
        if provider:
            variant = self.root / name / f"v{version}.{provider}.md"
            if variant.is_file():
                return variant, version, provider
        default = self.root / name / f"v{version}.md"
        if not default.is_file():
            raise PromptNotFoundError(f"prompt {name!r} has no version {version}")
        return default, version, None

    def render(
        self, name: str, variables: dict[str, Any], *, version: int | None = None, provider: str | None = None
    ) -> RenderedPrompt:
        path, resolved_version, variant = self._resolve(name, version, provider)
        raw = path.read_text(encoding="utf-8")
        meta, body = _parse_front_matter(raw, path)
        if meta.get("name") != name or meta.get("version") != str(resolved_version):
            raise ValueError(
                f"{path}: front matter must declare name: {name} and version: {resolved_version}"
            )
        content = self._env.from_string(body).render(**variables).strip() + "\n"
        return RenderedPrompt(
            name=name,
            version=resolved_version,
            provider_variant=variant,
            sha256=hashlib.sha256(_with_includes(raw, self.root).encode()).hexdigest(),
            content=content,
        )


def _with_includes(raw: str, root: Path, seen: frozenset[str] = frozenset()) -> str:
    """Template source followed by every partial it includes (recursively), so the prompt hash
    changes when a shared partial changes, not only when the template file itself does."""
    parts = [raw]
    for name in _INCLUDE.findall(raw):
        if name not in seen:
            partial = (root / name).read_text(encoding="utf-8")
            parts.append(_with_includes(partial, root, seen | {name}))
    return "\n".join(parts)


@lru_cache
def get_registry() -> PromptRegistry:
    return PromptRegistry()
