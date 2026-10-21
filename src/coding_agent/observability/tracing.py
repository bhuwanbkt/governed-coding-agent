"""Tracing: one Langfuse trace per job, with every step, LLM call and tool call inside it.

The trace ID is derived from the job ID, so the coding run and the later publish run (after a
human approves) land in the same trace, and the UI can link to it without storing anything.

    trace (job)                      tags: model, policy version; user: requester
      job.code                       root span of the coding run
        node.plan / node.code / ...  one span per graph step
          planner.llm                generation: model, prompt version, tokens, cost
          tool.read_file             tool span: masked args, redacted result, allowed or blocked
      job.publish                    root span of the publish run, after approval

Rules:
- Optional. Without LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY every call is a no-op.
- Nothing leaves the process unredacted: Langfuse's `mask` hook runs the same secret redactor as
  the audit log on every input, output and metadata value, and long values are cut.
- Tracing never breaks a job: errors inside the tracer are logged and swallowed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Protocol, cast

from coding_agent.governance.redact import redact_value

if TYPE_CHECKING:
    from coding_agent.config import Settings

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 8_000  # per string sent to Langfuse; prompts can include whole files
MAX_LIST_ITEMS = 60


class Observation(Protocol):
    def update(self, **fields: Any) -> None: ...


class _NoopObservation:
    def update(self, **fields: Any) -> None:
        return None


NOOP = _NoopObservation()


class Tracer:
    """No-op tracer: the default, used in tests and whenever Langfuse isn't configured."""

    enabled = False

    @contextmanager
    def job(
        self,
        job_id: str,
        name: str,
        *,
        input: Any = None,
        user: str | None = None,
        tags: list[str] | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> Iterator[Observation]:
        yield NOOP

    @contextmanager
    def observe(
        self,
        name: str,
        *,
        as_type: str = "span",
        input: Any = None,
        metadata: Any = None,
        model: str | None = None,
    ) -> Iterator[Observation]:
        yield NOOP

    def score(self, job_id: str, name: str, value: float | str, *, comment: str | None = None) -> None:
        return None

    def trace_url(self, job_id: str) -> str | None:
        return None

    def flush(self) -> None:
        return None


def mask(data: Any) -> Any:
    """Redact secrets and personal data, and keep payloads small."""
    if isinstance(data, str):
        text: str = redact_value(data)
        return text if len(text) <= MAX_TEXT_CHARS else f"{text[:MAX_TEXT_CHARS]}… [{len(text)} chars]"
    if isinstance(data, Mapping):
        return {str(k): mask(v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        items = [mask(v) for v in data[:MAX_LIST_ITEMS]]
        if len(data) > MAX_LIST_ITEMS:
            items.append(f"… {len(data) - MAX_LIST_ITEMS} more")
        return items
    return data


def _langfuse_mask(*, data: Any, **_: Any) -> Any:
    """Langfuse calls this on every input, output and metadata value before export."""
    return mask(data)


def _clean(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None}


class _LangfuseObservation:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def update(self, **fields: Any) -> None:
        try:
            self._inner.update(**_clean(fields))
        except Exception:
            logger.warning("tracing: update failed", exc_info=True)


class LangfuseTracer(Tracer):
    """Sends traces to Langfuse (SDK v5, OpenTelemetry based). `client` is a `langfuse.Langfuse`;
    tests pass a fake with the same methods."""

    enabled = True

    def __init__(self, client: Any, *, propagate: Any = None, tags: list[str] | None = None) -> None:
        self.client = client
        self._propagate = propagate  # langfuse.propagate_attributes
        self._tags = tags or []
        self._url_prefix: str | None = None
        self._url_lock = threading.Lock()
        self._url_failed = False

    def trace_id(self, job_id: str) -> str:
        return str(self.client.create_trace_id(seed=job_id))

    @contextmanager
    def job(
        self,
        job_id: str,
        name: str,
        *,
        input: Any = None,
        user: str | None = None,
        tags: list[str] | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> Iterator[Observation]:
        try:
            trace_id = self.trace_id(job_id)
            attributes = self._propagate(
                user_id=user,
                session_id=job_id,
                tags=[*self._tags, *(tags or [])],
                trace_name="job",
                metadata={k: str(v)[:200] for k, v in (metadata or {}).items()},
            )
            span = self.client.start_as_current_observation(
                as_type="span", name=name, input=input, trace_context={"trace_id": trace_id}
            )
        except Exception:
            logger.warning("tracing: could not start the job trace", exc_info=True)
            yield NOOP
            return
        with attributes, span as inner:
            yield _LangfuseObservation(inner)

    @contextmanager
    def observe(
        self,
        name: str,
        *,
        as_type: str = "span",
        input: Any = None,
        metadata: Any = None,
        model: str | None = None,
    ) -> Iterator[Observation]:
        try:
            span = self.client.start_as_current_observation(
                **_clean(
                    {"as_type": as_type, "name": name, "input": input, "metadata": metadata, "model": model}
                )
            )
        except Exception:
            logger.warning("tracing: could not start %s", name, exc_info=True)
            yield NOOP
            return
        with span as inner:
            yield _LangfuseObservation(inner)

    def score(self, job_id: str, name: str, value: float | str, *, comment: str | None = None) -> None:
        try:
            self.client.create_score(
                **_clean(
                    {
                        "name": name,
                        "value": value,
                        "trace_id": self.trace_id(job_id),
                        "data_type": "CATEGORICAL" if isinstance(value, str) else "NUMERIC",
                        "comment": comment,
                    }
                )
            )
        except Exception:
            logger.warning("tracing: score %s failed", name, exc_info=True)

    def trace_url(self, job_id: str) -> str | None:
        """Link to the job's trace. The project part of the URL is looked up once, then cached."""
        trace_id = self.trace_id(job_id)
        if self._url_prefix is None and not self._url_failed:
            with self._url_lock:
                if self._url_prefix is None and not self._url_failed:
                    try:
                        url = self.client.get_trace_url(trace_id=trace_id)
                    except Exception:
                        url = None
                    if url and url.endswith(trace_id):
                        self._url_prefix = url[: -len(trace_id)]
                    else:
                        self._url_failed = True
        return f"{self._url_prefix}{trace_id}" if self._url_prefix else None

    def flush(self) -> None:
        try:
            self.client.flush()
        except Exception:
            logger.warning("tracing: flush failed", exc_info=True)


_tracer: Tracer = Tracer()
_configured = False
_lock = threading.Lock()


def tracer() -> Tracer:
    return _tracer


def set_tracer(value: Tracer) -> Tracer:
    """Install a tracer (tests use a recording fake). Returns the previous one."""
    global _tracer, _configured
    previous, _tracer, _configured = _tracer, value, True
    return previous


def configure(settings: Settings) -> Tracer:
    """Create the Langfuse tracer once per process, if keys are set. Safe to call repeatedly."""
    global _tracer, _configured
    if _configured:
        return _tracer
    with _lock:
        if _configured:
            return _tracer
        _configured = True
        if settings.langfuse_public_key is None or settings.langfuse_secret_key is None:
            return _tracer
        try:
            from langfuse import Langfuse, propagate_attributes

            client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                base_url=settings.langfuse_base_url,
                environment=settings.app_env,
                release=settings.app_version,
                sample_rate=settings.langfuse_sample_rate,
                mask=cast(Any, _langfuse_mask),
            )
            _tracer = LangfuseTracer(
                client,
                propagate=propagate_attributes,
                tags=[f"model:{settings.llm_model}", f"supervisor:{settings.supervisor_mode}"],
            )
            logger.info("tracing enabled", extra={"langfuse_base_url": settings.langfuse_base_url})
        except Exception:
            logger.exception("tracing: Langfuse could not be initialised; continuing without tracing")
        return _tracer
