"""Golden tasks load, validate, and are themselves correct (hidden tests fail before the change
and pass on the reference solution)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.evals.golden import validate_task
from coding_agent.evals.tasks import EVALS_DIR, FAKE_GITHUB_TOKEN, TaskError, load_golden, load_task

TASKS = load_golden()


def test_the_suite_covers_every_kind() -> None:
    kinds = {t.kind for t in TASKS}
    assert kinds == {"solve", "safety", "handoff"}
    assert len(TASKS) >= 10
    assert {t.fixture for t in TASKS} == {"calculator", "shop"}


@pytest.mark.parametrize("task", [t for t in TASKS if t.kind == "solve"], ids=lambda t: t.id)
def test_hidden_tests_fail_before_and_pass_on_the_reference(task, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    assert validate_task(task, tmp_path) == []


def test_placeholders_are_filled_at_load_time() -> None:
    secret_task = next(t for t in TASKS if t.id == "gov-secret-in-task")
    assert FAKE_GITHUB_TOKEN in secret_task.task
    assert FAKE_GITHUB_TOKEN in str(secret_task.offline)
    on_disk = (EVALS_DIR / "golden" / "gov-secret-in-task" / "task.yaml").read_text()
    assert FAKE_GITHUB_TOKEN not in on_disk  # the repository never holds a token-shaped value


def test_solve_tasks_default_to_replaying_the_reference() -> None:
    divide = next(t for t in TASKS if t.id == "calc-divide")
    assert divide.offline == ({"reference": True},)


def _task(tmp_path: Path, yaml_text: str) -> Path:
    folder = tmp_path / "golden" / "t1"
    folder.mkdir(parents=True)
    (folder / "task.yaml").write_text(yaml_text)
    return folder


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("fixture: calculator\nkind: magic\ntask: x\nexpect: [no_changes]\n", "kind must be"),
        ("fixture: calculator\nkind: handoff\ntask: x\nexpect: [merged]\n", "expect must list"),
        ("fixture: nowhere\nkind: handoff\ntask: x\nexpect: [no_changes]\n", "unknown fixture"),
        ("fixture: calculator\nkind: handoff\ntask: ''\nexpect: [no_changes]\n", "task text"),
        ("fixture: calculator\nkind: solve\ntask: x\nexpect: [awaiting_approval]\n", "hidden/ tests"),
        ("fixture: calculator\nkind: handoff\ntask: x\nexpect: [no_changes]\ntypo: 1\n", "unknown keys"),
    ],
)
def test_bad_task_specs_are_rejected(tmp_path: Path, yaml_text: str, message: str) -> None:
    with pytest.raises(TaskError, match=message):
        load_task(_task(tmp_path, yaml_text), EVALS_DIR)


def test_unknown_task_ids_are_reported() -> None:
    with pytest.raises(TaskError, match="unknown task id"):
        load_golden(ids=["calc-divide", "nope"])
