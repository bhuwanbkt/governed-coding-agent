"""Golden tasks: one folder per task under `evals/golden/<task-id>/`.

    task.yaml        fixture, kind, the task text, expected outcomes and path rules
    hidden/...       acceptance tests the agent never sees; copied in when grading
    reference/...    a known-good solution: used to validate the task and by the offline run
    extra/...        files added to the fixture for this task only (e.g. an injected README)

`{FAKE_GITHUB_TOKEN}` in a task is replaced at load time with a fake, token-shaped value, so the
repository never contains anything a secret scanner would flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import yaml

EVALS_DIR = Path(__file__).resolve().parents[3] / "evals"
FAKE_GITHUB_TOKEN = "ghp_" + "Q7w8E9r0" * 5  # built at runtime: not a real secret
PLACEHOLDERS = {"{FAKE_GITHUB_TOKEN}": FAKE_GITHUB_TOKEN}
OUTCOMES = frozenset({"awaiting_approval", "no_changes", "handed_off", "failed"})
Kind = Literal["solve", "safety", "handoff"]
KINDS: tuple[Kind, ...] = ("solve", "safety", "handoff")


class TaskError(ValueError):
    pass


@dataclass(frozen=True)
class GoldenTask:
    id: str
    fixture: str
    kind: Kind
    task: str
    expect: tuple[str, ...]
    allowed_paths: tuple[str, ...] = ()  # empty: any path the policy allows
    must_not_touch: tuple[str, ...] = ()
    must_keep: tuple[str, ...] = ()
    hidden: dict[str, str] = field(default_factory=dict)
    reference: dict[str, str] = field(default_factory=dict)
    extra: dict[str, str] = field(default_factory=dict)
    offline: tuple[dict[str, Any], ...] = ()  # scripted coder actions for the offline run


def fixture_dir(evals_dir: Path, name: str) -> Path:
    path = evals_dir / "sample_repo" if name == "calculator" else evals_dir / "fixtures" / name
    if not path.is_dir():
        raise TaskError(f"unknown fixture {name!r} (looked in {path})")
    return path


def _fill(value: Any) -> Any:
    if isinstance(value, str):
        for key, replacement in PLACEHOLDERS.items():
            value = value.replace(key, replacement)
        return value
    if isinstance(value, dict):
        return {k: _fill(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v) for v in value]
    return value


def _files(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {
        p.relative_to(root).as_posix(): _fill(p.read_text(encoding="utf-8"))
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def load_task(folder: Path, evals_dir: Path = EVALS_DIR) -> GoldenTask:
    spec = yaml.safe_load((folder / "task.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(spec, dict):
        raise TaskError(f"{folder.name}: task.yaml must be a mapping")
    spec = _fill(spec)
    known = {"fixture", "kind", "task", "expect", "allowed_paths", "must_not_touch", "must_keep", "offline"}
    if unknown := set(spec) - known:
        raise TaskError(f"{folder.name}: unknown keys {sorted(unknown)}")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise TaskError(f"{folder.name}: kind must be one of {KINDS}")
    expect = tuple(spec.get("expect") or ())
    if not expect or not set(expect) <= OUTCOMES:
        raise TaskError(f"{folder.name}: expect must list outcomes from {sorted(OUTCOMES)}")
    if not str(spec.get("task", "")).strip():
        raise TaskError(f"{folder.name}: task text is missing")
    fixture = str(spec.get("fixture", ""))
    fixture_dir(evals_dir, fixture)
    hidden, reference = _files(folder / "hidden"), _files(folder / "reference")
    if kind == "solve" and not (hidden and reference):
        raise TaskError(f"{folder.name}: solve tasks need hidden/ tests and a reference/ solution")
    offline = spec.get("offline")
    if offline is None:
        offline = [{"reference": True}] if kind == "solve" else []
    return GoldenTask(
        id=folder.name,
        fixture=fixture,
        kind=cast(Kind, kind),
        task=" ".join(str(spec["task"]).split()),
        expect=expect,
        allowed_paths=tuple(spec.get("allowed_paths") or ()),
        must_not_touch=tuple(spec.get("must_not_touch") or ()),
        must_keep=tuple(spec.get("must_keep") or ()),
        hidden=hidden,
        reference=reference,
        extra=_files(folder / "extra"),
        offline=tuple(offline),
    )


def load_golden(evals_dir: Path = EVALS_DIR, ids: list[str] | None = None) -> list[GoldenTask]:
    folders = sorted(p for p in (evals_dir / "golden").iterdir() if (p / "task.yaml").is_file())
    tasks = [load_task(folder, evals_dir) for folder in folders]
    if ids:
        missing = set(ids) - {t.id for t in tasks}
        if missing:
            raise TaskError(f"unknown task id(s): {', '.join(sorted(missing))}")
        tasks = [t for t in tasks if t.id in ids]
    return tasks
