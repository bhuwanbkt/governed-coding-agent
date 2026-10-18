from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import UndefinedError

from coding_agent.prompts.registry import PromptNotFoundError, PromptRegistry, get_registry

CODER_VARS = {
    "task": "Add divide()",
    "test_command": "pytest -q",
    "repo_files": "calculator.py",
    "test_output": "",
    "attempt": 1,
    "max_attempts": 3,
}


def test_coder_prompt_renders_with_partials_and_reference() -> None:
    rendered = get_registry().render("coder", CODER_VARS)
    assert rendered.name == "coder"
    assert rendered.version == 1
    assert rendered.ref.startswith("coder@v1#")
    assert "Add divide()" in rendered.content
    assert "Security rules" in rendered.content  # partials/safety.md
    assert "How to use tools" in rendered.content  # partials/tool_rules.md
    assert "Previous attempt failed" not in rendered.content


def test_failed_attempt_section_wraps_test_output_as_untrusted() -> None:
    rendered = get_registry().render("coder", {**CODER_VARS, "test_output": "AssertionError", "attempt": 2})
    assert "Previous attempt failed (attempt 2 of 3)" in rendered.content
    assert '<untrusted kind="test-output">\nAssertionError\n</untrusted>' in rendered.content


def test_missing_variable_is_an_error_not_a_blank() -> None:
    with pytest.raises(UndefinedError):
        get_registry().render("coder", {"task": "x"})


def test_unknown_prompt_raises() -> None:
    with pytest.raises(PromptNotFoundError):
        get_registry().render("does-not-exist", {})


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_latest_version_and_provider_variant(tmp_path: Path) -> None:
    _write(tmp_path, "greet/v1.md", "---\nname: greet\nversion: 1\n---\nHi {{ who }}")
    _write(tmp_path, "greet/v2.md", "---\nname: greet\nversion: 2\n---\nHello {{ who }}")
    _write(tmp_path, "greet/v2.groq.md", "---\nname: greet\nversion: 2\n---\nHey {{ who }}")
    registry = PromptRegistry(tmp_path)

    assert registry.versions("greet") == [1, 2]
    assert registry.render("greet", {"who": "A"}).content == "Hello A\n"
    assert registry.render("greet", {"who": "A"}, version=1).content == "Hi A\n"
    groq = registry.render("greet", {"who": "A"}, provider="groq")
    assert groq.content == "Hey A\n"
    assert groq.provider_variant == "groq"
    assert registry.render("greet", {"who": "A"}, provider="other").provider_variant is None


def test_front_matter_must_match_file(tmp_path: Path) -> None:
    _write(tmp_path, "greet/v1.md", "---\nname: wrong\nversion: 1\n---\nHi")
    with pytest.raises(ValueError, match="front matter"):
        PromptRegistry(tmp_path).render("greet", {})


def test_every_shipped_prompt_has_valid_front_matter() -> None:
    registry = get_registry()
    for folder in registry.root.iterdir():
        if folder.is_dir() and folder.name != "partials":
            assert registry.versions(folder.name), folder
