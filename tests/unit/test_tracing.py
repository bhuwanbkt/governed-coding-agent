"""Tracing: masking, the Langfuse adapter (against a fake client) and the no-op default."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from coding_agent.config import Settings
from coding_agent.observability import tracing
from coding_agent.observability.tracing import LangfuseTracer, Tracer, mask

FAKE_TOKEN = "ghp_" + "Z9y8X7w6" * 5  # built at runtime: not a real secret


class FakeSpan:
    def __init__(self, client: FakeClient, fields: dict[str, Any]) -> None:
        self.client, self.fields, self.updates = client, fields, []

    def __enter__(self) -> FakeSpan:
        self.client.stack.append(self.fields["name"])
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.stack.pop()

    def update(self, **fields: Any) -> None:
        self.updates.append(fields)


class FakeClient:
    """The subset of `langfuse.Langfuse` the tracer uses."""

    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []
        self.parents: list[str | None] = []
        self.stack: list[str] = []
        self.scores: list[dict[str, Any]] = []
        self.url_calls = 0
        self.fail_start = False

    @staticmethod
    def create_trace_id(*, seed: str) -> str:
        return f"{abs(hash(seed)) % 16**32:032x}"

    def start_as_current_observation(self, **fields: Any) -> FakeSpan:
        if self.fail_start:
            raise RuntimeError("exporter down")
        span = FakeSpan(self, fields)
        self.spans.append(span)
        self.parents.append(self.stack[-1] if self.stack else None)
        return span

    def create_score(self, **fields: Any) -> None:
        self.scores.append(fields)

    def get_trace_url(self, *, trace_id: str) -> str:
        self.url_calls += 1
        return f"https://lf.example.test/project/p1/traces/{trace_id}"

    def flush(self) -> None:
        raise RuntimeError("network down")  # must not escape


class Propagated:
    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        Propagated.calls.append(kwargs)

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def client() -> FakeClient:
    Propagated.calls = []
    return FakeClient()


@pytest.fixture
def tracer(client: FakeClient) -> LangfuseTracer:
    return LangfuseTracer(client, propagate=Propagated, tags=["model:m"])


# --------------------------------------------------------------------------- masking
def test_mask_redacts_secrets_and_personal_data_at_any_depth() -> None:
    data = {"msg": f"token {FAKE_TOKEN}", "nested": [{"who": "bob@example.com"}], "n": 3}
    masked = mask(data)
    assert FAKE_TOKEN not in str(masked)
    assert "[REDACTED:github_token]" in masked["msg"]
    assert masked["nested"][0]["who"] == "[REDACTED:email]"
    assert masked["n"] == 3


def test_mask_keeps_payloads_small() -> None:
    assert mask("x" * 20_000).endswith("[20000 chars]")
    assert len(mask("x" * 20_000)) < 8_100
    items = mask(list(range(100)))
    assert len(items) == 61 and items[-1] == "… 40 more"


# --------------------------------------------------------------------------- adapter
def test_job_trace_id_comes_from_the_job_id(tracer: LangfuseTracer, client: FakeClient) -> None:
    with tracer.job("job-1", "job.code", input={"task": "t"}, user="alice", tags=["x"]) as root:
        root.update(output={"status": "ok"}, level=None)
    [span] = client.spans
    assert span.fields["trace_context"] == {"trace_id": client.create_trace_id(seed="job-1")}
    assert span.fields["name"] == "job.code"
    assert span.updates == [{"output": {"status": "ok"}}]  # None values are dropped
    [attrs] = Propagated.calls
    assert attrs["user_id"] == "alice" and attrs["session_id"] == "job-1"
    assert attrs["tags"] == ["model:m", "x"]


def test_spans_nest_under_the_job(tracer: LangfuseTracer, client: FakeClient) -> None:
    with (
        tracer.job("job-1", "job.code"),
        tracer.observe("node.plan", as_type="agent"),
        tracer.observe("planner.llm", as_type="generation", model="m1") as gen,
    ):
        gen.update(usage_details={"input": 10, "output": 2})
    assert [s.fields["name"] for s in client.spans] == ["job.code", "node.plan", "planner.llm"]
    assert client.parents == [None, "job.code", "node.plan"]
    assert client.spans[2].fields["model"] == "m1"
    assert "model" not in client.spans[1].fields


def test_tracer_failures_never_break_the_job(tracer: LangfuseTracer, client: FakeClient) -> None:
    client.fail_start = True
    with tracer.job("job-1", "job.code") as root, tracer.observe("node.plan") as span:
        root.update(output="x")
        span.update(output="y")
    tracer.flush()  # the fake raises; the tracer logs and carries on


def test_errors_inside_a_span_still_propagate(tracer: LangfuseTracer) -> None:
    with pytest.raises(ValueError), tracer.observe("tool.read_file", as_type="tool"):
        raise ValueError("boom")


def test_scores_have_the_right_type(tracer: LangfuseTracer, client: FakeClient) -> None:
    tracer.score("job-1", "status", "succeeded")
    tracer.score("job-1", "cost_usd", 0.004)
    assert [s["data_type"] for s in client.scores] == ["CATEGORICAL", "NUMERIC"]
    assert {s["trace_id"] for s in client.scores} == {client.create_trace_id(seed="job-1")}


def test_trace_url_looks_up_the_project_once(tracer: LangfuseTracer, client: FakeClient) -> None:
    first, second = tracer.trace_url("job-1"), tracer.trace_url("job-2")
    assert first == f"https://lf.example.test/project/p1/traces/{client.create_trace_id(seed='job-1')}"
    assert second is not None and second.endswith(client.create_trace_id(seed="job-2"))
    assert client.url_calls == 1


# --------------------------------------------------------------------------- default
def test_without_keys_tracing_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.setattr(tracing, "_tracer", Tracer())
    active = tracing.configure(Settings(_env_file=None))  # type: ignore[call-arg]
    assert not active.enabled
    with active.job("j", "job.code") as root, active.observe("x") as span:
        root.update(output=1)
        span.update(output=2)
    assert active.trace_url("j") is None


def test_langfuse_keys_must_be_set_together() -> None:
    with pytest.raises(ValueError, match="LANGFUSE"):
        Settings(_env_file=None, langfuse_public_key="pk-lf-x")  # type: ignore[call-arg]


@pytest.mark.parametrize("name", ["LANGFUSE_BASE_URL", "LANGFUSE_HOST"])
def test_langfuse_url_accepts_both_names(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "https://us.cloud.langfuse.com")
    assert Settings(_env_file=None).langfuse_base_url == "https://us.cloud.langfuse.com"  # type: ignore[call-arg]
