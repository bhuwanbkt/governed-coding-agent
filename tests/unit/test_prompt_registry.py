from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import UndefinedError

from coding_agent.prompts.registry import PromptNotFoundError, PromptRegistry, get_registry

CODER_VARS = {
    "task": "Add divide()",
    "test_command": "pytest -q",
    "repo_files": "calculator.py",
    "plan": "**Approach:** add it",
    "round": 1,
    "test_output": "",
    "review_findings": "",
}


def test_coder_prompt_renders_with_partials_and_reference() -> None:
    rendered = get_registry().render("coder", CODER_VARS)
    assert rendered.name == "coder"
    assert rendered.version == 2
    assert rendered.ref.startswith("coder@v2#")
    assert "Add divide()" in rendered.content
    assert "**Approach:** add it" in rendered.content
    assert "Security rules" in rendered.content  # partials/safety.md
    assert "How to use tools" in rendered.content  # partials/tool_rules.md
    assert "search_codebase" in rendered.content  # partials/retrieval_rules.md
    assert "Previous attempt failed" not in rendered.content
    assert "Reviewer requested changes" not in rendered.content


def test_failed_attempt_section_wraps_test_output_as_untrusted() -> None:
    rendered = get_registry().render("coder", {**CODER_VARS, "test_output": "AssertionError", "round": 2})
    assert "Previous attempt failed (round 2)" in rendered.content
    assert '<untrusted kind="test-output">\nAssertionError\n</untrusted>' in rendered.content


def test_review_findings_are_wrapped_as_untrusted() -> None:
    rendered = get_registry().render("coder", {**CODER_VARS, "review_findings": "- [high] bug", "round": 2})
    assert "The Reviewer requested changes (round 2)" in rendered.content
    assert '<untrusted kind="review">\n- [high] bug\n</untrusted>' in rendered.content


def test_previous_versions_stay_renderable() -> None:
    v1 = get_registry().render(
        "coder",
        {
            "task": "t",
            "test_command": "pytest",
            "repo_files": "",
            "test_output": "",
            "attempt": 1,
            "max_attempts": 3,
        },
        version=1,
    )
    assert v1.ref.startswith("coder@v1#")


@pytest.mark.parametrize(
    ("name", "variables"),
    [
        (
            "planner",
            {
                "task": "t",
                "test_command": "pytest",
                "repo_files": "a.py",
                "index_summary": "INDEXED files=1",
                "replan_reason": "",
                "previous_plan": "",
            },
        ),
        ("reviewer", {"task": "t", "plan": "p", "test_output": "ok", "diff": "+x"}),
        ("supervisor", {"task": "t", "situation": "s", "options": {"code": "fix it", "hand_off": "stop"}}),
    ],
)
def test_agent_prompts_render(name: str, variables: dict[str, object]) -> None:
    rendered = get_registry().render(name, variables)
    assert rendered.ref.startswith(f"{name}@v1#")
    assert "Security rules" in rendered.content


def test_changing_a_partial_changes_the_prompt_hash(tmp_path: Path) -> None:
    _write(tmp_path, "partials/rules.md", "Be careful.")
    _write(tmp_path, "greet/v1.md", '---\nname: greet\nversion: 1\n---\n{% include "partials/rules.md" %}')
    before = PromptRegistry(tmp_path).render("greet", {}).sha256
    _write(tmp_path, "partials/rules.md", "Be very careful.")
    assert PromptRegistry(tmp_path).render("greet", {}).sha256 != before


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
