"""Client side of MCP: connects to the workspace server and enforces governance on every call.

Agents never talk to the MCP session directly. They go through `Toolbox.call`, which:
- rejects tools outside the calling agent's allowlist (least privilege)
- stops a run that repeats the identical call too often (loop detection)
- writes one structured audit log line per call (persisted to Postgres from phase 2)
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("coding_agent.audit")

MAX_IDENTICAL_CALLS = 3
_SECRET_ARG_NAMES = {"token", "password", "secret", "api_key"}


class ToolNotAllowedError(PermissionError):
    pass


class LoopDetectedError(RuntimeError):
    pass


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
    session: ClientSession
    specs: dict[str, ToolSpec]
    job_id: str = "local"
    _call_counts: Counter[str] = field(default_factory=Counter)

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, workspace_dir: Path, *, job_id: str, env: Mapping[str, str] | None = None
    ) -> AsyncIterator[Toolbox]:
        """Start the workspace MCP server as a subprocess and open a session to it."""
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "coding_agent.mcp_servers.workspace_server"],
            env={"WORKSPACE_DIR": str(workspace_dir), **(env or {})},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            specs = {
                t.name: ToolSpec(t.name, t.description or "", dict(t.input_schema or {}))
                for t in listed.tools
            }
            yield cls(session=session, specs=specs, job_id=job_id)

    def schemas(self, allowed: Collection[str]) -> list[dict[str, Any]]:
        return [self.specs[name].to_openai() for name in allowed if name in self.specs]

    async def call(self, name: str, args: Mapping[str, Any], *, actor: str, allowed: Collection[str]) -> str:
        """Call a tool on behalf of `actor`. Returns text; tool errors come back as 'ERROR: ...'."""
        start = time.perf_counter()
        if name not in allowed or name not in self.specs:
            self._audit(actor, name, args, allowed=False, result="blocked: not in allowlist", start=start)
            raise ToolNotAllowedError(f"{actor} is not allowed to call {name!r}")

        fingerprint = f"{actor}:{name}:{json.dumps(args, sort_keys=True, default=str)}"
        self._call_counts[fingerprint] += 1
        if self._call_counts[fingerprint] > MAX_IDENTICAL_CALLS:
            self._audit(actor, name, args, allowed=False, result="blocked: loop detected", start=start)
            raise LoopDetectedError(f"{actor} repeated {name} with identical arguments too many times")

        result = await self.session.call_tool(name, dict(args))
        text = "\n".join(getattr(part, "text", "") for part in getattr(result, "content", []) or [])
        if getattr(result, "is_error", False):
            text = f"ERROR: {text}"
        self._audit(actor, name, args, allowed=True, result=text, start=start)
        return text

    def _audit(
        self, actor: str, tool: str, args: Mapping[str, Any], *, allowed: bool, result: str, start: float
    ) -> None:
        logger.info(
            "tool_call",
            extra={
                "event_type": "tool_call" if allowed else "policy_block",
                "job_id": self.job_id,
                "actor": actor,
                "tool": tool,
                "allowed": allowed,
                "args_masked": _mask_args(args),
                "result_summary": result[:300],
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            },
        )


def _mask_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Keep audit lines small and secret-free: mask secret-like keys, shorten long values."""
    masked: dict[str, Any] = {}
    for key, value in args.items():
        if key.lower() in _SECRET_ARG_NAMES:
            masked[key] = "***"
        elif isinstance(value, str) and len(value) > 200:
            masked[key] = f"<{len(value)} chars>"
        else:
            masked[key] = value
    return masked
