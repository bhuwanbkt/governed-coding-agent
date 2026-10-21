"""Client side of MCP: connects to the tool servers and enforces governance on every call.

Agents never talk to the MCP session directly. They go through `Toolbox.call`, which:
- rejects tools outside the calling agent's allowlist (least privilege)
- applies the governance policy to the arguments (protected paths, test commands)
- scans what agents read for prompt injection; a blocked action after suspicious content stops
  the job for a human
- masks secrets and personal data in everything it records
- stops a run that repeats the identical call too often (loop detection)
- writes one structured audit log line per call (persisted to Postgres from phase 2)
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from coding_agent.governance import injection
from coding_agent.governance.policy import Policy
from coding_agent.governance.redact import redact, redact_value

logger = logging.getLogger("coding_agent.audit")

MAX_IDENTICAL_CALLS = 3
AuditSink = Callable[[dict[str, Any]], Awaitable[None]]
_SECRET_ARG_NAMES = {"token", "password", "secret", "api_key"}
# MCP servers by name. Each runs as its own stdio subprocess for one job.
SERVER_MODULES = {
    "workspace": "coding_agent.mcp_servers.workspace_server",
    "code_index": "coding_agent.mcp_servers.code_index_server",
}


class ToolNotAllowedError(PermissionError):
    pass


class PolicyViolationError(ToolNotAllowedError):
    """The tool is allowed for this agent, but these arguments break the governance policy."""


class LoopDetectedError(RuntimeError):
    pass


class InjectionSuspectedError(RuntimeError):
    """An agent tried a blocked action after reading instruction-like content: stop for a human."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        """OpenAI-style function schema; accepted by LangChain's `bind_tools`."""
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.input_schema},
        }


@dataclass
class Toolbox:
    sessions: dict[str, ClientSession]  # tool name -> session of the server that provides it
    specs: dict[str, ToolSpec]
    job_id: str = "local"
    audit_sink: AuditSink | None = None  # e.g. writes to the audit_log table (phase 2)
    policy: Policy | None = None  # governance policy applied to arguments (phase 4)
    injection_flags: list[str] = field(default_factory=list)
    _call_counts: Counter[str] = field(default_factory=Counter)

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        workspace_dir: Path,
        *,
        job_id: str,
        env: Mapping[str, str] | None = None,
        audit_sink: AuditSink | None = None,
        servers: Sequence[str] = ("workspace",),
        server_env: Mapping[str, Mapping[str, str]] | None = None,
        policy: Policy | None = None,
    ) -> AsyncIterator[Toolbox]:
        """Start the named MCP servers as subprocesses and open a session to each.
        `server_env` replaces `env` for the servers it names (least privilege: e.g. the code
        index never receives the GitHub token)."""
        sessions: dict[str, ClientSession] = {}
        specs: dict[str, ToolSpec] = {}
        async with AsyncExitStack() as stack:
            for server in servers:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", SERVER_MODULES[server]],
                    env={"WORKSPACE_DIR": str(workspace_dir), **_env_for(server, env, server_env)},
                )
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                for tool in (await session.list_tools()).tools:
                    if tool.name in specs:
                        raise RuntimeError(f"tool {tool.name!r} is provided by two servers")
                    specs[tool.name] = ToolSpec(
                        tool.name, tool.description or "", dict(tool.input_schema or {})
                    )
                    sessions[tool.name] = session
            yield cls(sessions=sessions, specs=specs, job_id=job_id, audit_sink=audit_sink, policy=policy)

    def schemas(self, allowed: Collection[str]) -> list[dict[str, Any]]:
        return [self.specs[name].to_openai() for name in allowed if name in self.specs]

    async def call(self, name: str, args: Mapping[str, Any], *, actor: str, allowed: Collection[str]) -> str:
        """Call a tool on behalf of `actor`. Returns text; tool errors come back as 'ERROR: ...'."""
        start = time.perf_counter()
        if name not in allowed or name not in self.specs:
            await self._audit(
                actor, name, args, allowed=False, result="blocked: not in allowlist", start=start
            )
            self._stop_if_injected(actor, f"{name} is not in its allowlist")
            raise ToolNotAllowedError(f"{actor} is not allowed to call {name!r}")
        violation = self.policy.check_tool_call(name, dict(args)) if self.policy else None
        if violation is not None and self.policy is not None:
            await self._audit(
                actor,
                name,
                args,
                allowed=False,
                result=f"blocked by {self.policy.ref}: {violation}",
                start=start,
            )
            self._stop_if_injected(actor, str(violation))
            raise PolicyViolationError(f"blocked by policy: {violation}")

        fingerprint = f"{actor}:{name}:{json.dumps(args, sort_keys=True, default=str)}"
        self._call_counts[fingerprint] += 1
        if self._call_counts[fingerprint] > MAX_IDENTICAL_CALLS:
            await self._audit(actor, name, args, allowed=False, result="blocked: loop detected", start=start)
            raise LoopDetectedError(f"{actor} repeated {name} with identical arguments too many times")

        result = await self.sessions[name].call_tool(name, dict(args))
        text = "\n".join(getattr(part, "text", "") for part in getattr(result, "content", []) or [])
        if getattr(result, "is_error", False):
            text = f"ERROR: {text}"
        await self._audit(actor, name, args, allowed=True, result=text, start=start)
        if name in injection.SCANNED_TOOLS and (hits := injection.detect(text)):
            where = f"{name}({args.get('path') or args.get('query') or args.get('pattern') or ''})"
            self.injection_flags.extend(f"{where}: {hit}" for hit in hits)
            await self.record(
                actor,
                "system",
                f"suspected prompt injection in {where}: {'; '.join(hits[:3])}",
                allowed=False,
            )
        return text

    def _stop_if_injected(self, actor: str, blocked: str) -> None:
        if self.injection_flags:
            raise InjectionSuspectedError(
                f"{actor} attempted a blocked action ({blocked}) after reading suspicious content "
                f"({self.injection_flags[0]}); stopped for human review"
            )

    async def record(self, actor: str, event_type: str, summary: str, *, allowed: bool = True) -> None:
        """Audit a decision that isn't a tool call, e.g. a routing decision or a submitted plan."""
        event = {
            "event_type": event_type,
            "job_id": self.job_id,
            "actor": actor,
            "tool": None,
            "allowed": allowed,
            "args_masked": None,
            "result_summary": redact(summary)[:300],
            "duration_ms": None,
        }
        logger.info(event_type, extra=event)
        if self.audit_sink is not None:
            await self.audit_sink(event)

    async def _audit(
        self, actor: str, tool: str, args: Mapping[str, Any], *, allowed: bool, result: str, start: float
    ) -> None:
        event = {
            "event_type": "tool_call" if allowed else "policy_block",
            "job_id": self.job_id,
            "actor": actor,
            "tool": tool,
            "allowed": allowed,
            "args_masked": _mask_args(args),
            "result_summary": redact(result)[:300],
            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
        }
        logger.info("tool_call", extra=event)
        if self.audit_sink is not None:
            # Fail closed: if the audit row can't be written, the job stops rather than act unrecorded.
            await self.audit_sink(event)


def _env_for(
    server: str, env: Mapping[str, str] | None, server_env: Mapping[str, Mapping[str, str]] | None
) -> dict[str, str]:
    if server_env is not None and server in server_env:
        return dict(server_env[server])
    return dict(env or {})


def _mask_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Keep audit lines small and secret-free: mask secret-like keys, shorten long values."""
    masked: dict[str, Any] = {}
    for key, value in args.items():
        if key.lower() in _SECRET_ARG_NAMES:
            masked[key] = "***"
        elif isinstance(value, str) and len(value) > 200:
            masked[key] = f"<{len(value)} chars>"
        else:
            masked[key] = redact_value(value)
    return masked
