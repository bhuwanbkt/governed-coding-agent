"""A real job (MCP servers, Git, pytest, scripted LLM) produces the expected trace shape."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from coding_agent.agent.fake_llm import ScriptedModels, demo_coder
from coding_agent.agent.runner import JobRequest, run_coding_job
from coding_agent.config import Settings
from coding_agent.observability import tracing
from coding_agent.observability.tracing import Observation, Tracer

TASK = "Add a divide(a, b) function that raises ValueError on zero, with tests"
JOB_ID = "00000000-0000-4000-8000-0000000000f6"


class RecordingTracer(Tracer):
    """Records every span with its parent and updates."""

    enabled = True

    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []
        self.scores: list[tuple[str, str, float | str]] = []
        self._stack: list[str] = []

    @contextmanager
    def _span(self, name: str, as_type: str, **fields: Any) -> Iterator[Observation]:
        record: dict[str, Any] = {
            "name": name,
            "type": as_type,
            "parent": self._stack[-1] if self._stack else None,
            "updates": {},
            **fields,
        }
        self.spans.append(record)
        self._stack.append(name)

        class _Obs:
            def update(self, **f: Any) -> None:
                record["updates"].update({k: v for k, v in f.items() if v is not None})

        try:
            yield _Obs()
        finally:
            self._stack.pop()

    @contextmanager
    def job(self, job_id: str, name: str, **fields: Any) -> Iterator[Observation]:
        with self._span(name, "job", job_id=job_id, **fields) as obs:
            yield obs

    @contextmanager
    def observe(self, name: str, *, as_type: str = "span", **fields: Any) -> Iterator[Observation]:
        with self._span(name, as_type, **fields) as obs:
            yield obs

    def score(self, job_id: str, name: str, value: float | str, *, comment: str | None = None) -> None:
        self.scores.append((job_id, name, value))


@pytest.fixture
def recorder() -> Iterator[RecordingTracer]:
    recording = RecordingTracer()
    previous = tracing.set_tracer(recording)
    yield recording
    tracing.set_tracer(previous)


def test_a_job_produces_one_trace_with_steps_llm_calls_and_tools(
    tmp_path: Path, sample_remote: str, recorder: RecordingTracer
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        workspaces_dir=tmp_path / "workspaces",
        allowed_test_commands="python -m pytest",
    )
    state = asyncio.run(
        run_coding_job(
            JobRequest(
                repo_url=sample_remote, task=TASK, test_command="python -m pytest -q", requested_by="alice"
            ),
            settings,
            job_id=JOB_ID,
            models=ScriptedModels(coder=demo_coder()),
        )
    )
    assert state["status"] == "awaiting_approval", state.get("error")

    spans = recorder.spans
    root = spans[0]
    assert (root["name"], root["type"], root["job_id"], root["user"]) == ("job.code", "job", JOB_ID, "alice")
    assert root["updates"]["output"]["status"] == "awaiting_approval"
    assert all(s["parent"] is not None for s in spans[1:])  # everything nests under the job

    nodes = [s["name"] for s in spans if s["name"].startswith("node.")]
    assert nodes[:3] == ["node.prepare", "node.index", "node.plan"]
    assert {"node.code", "node.test", "node.review", "node.package"} <= set(nodes)

    generations = [s for s in spans if s["type"] == "generation"]
    assert generations and {g["name"] for g in generations} >= {"planner.llm", "coder.llm"}
    coder_call = next(g for g in generations if g["name"] == "coder.llm")
    assert coder_call["parent"] == "node.code"
    assert coder_call["metadata"]["prompt"].startswith("coder@")
    assert "usage_details" in coder_call["updates"]

    tools = [s for s in spans if s["type"] == "tool"]
    assert any(t["name"] == "tool.git_clone" and t["parent"] == "node.prepare" for t in tools)
    assert any(t["name"] == "tool.run_tests" for t in tools)

    names = {name for _, name, _ in recorder.scores}
    assert {"status", "cost_usd", "duration_s", "risk"} <= names
