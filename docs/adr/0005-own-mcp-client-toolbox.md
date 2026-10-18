# ADR 0005: Wrap MCP tools in our own governed Toolbox

- **Status:** Accepted
- **Date:** 2026-10-18

## Context

Agents reach the repository through the workspace MCP server. Adapters such as
`langchain-mcp-adapters` can expose every MCP tool directly as a LangChain tool, but then the model
can call any tool the server offers, and there is no single place to enforce policy or record
what happened.

## Decision

Agents call tools only through `coding_agent.agent.toolbox.Toolbox`, a thin client over the
official `mcp` SDK (`ClientSession` over stdio). Every call passes through one choke point that:

1. checks the calling role's allowlist (Coder can edit files; only Publisher can push),
2. stops identical repeated calls (loop detection),
3. emits a structured audit event (persisted to `audit_log` from phase 2).

Only the allow-listed tool schemas are bound to each role's model (`Toolbox.schemas(allowed)`), so
the model never even sees tools it may not use. If it still asks for one, the call is blocked,
audited, and the model gets an error message instead.

Control flow (clone, test, approve, publish) is deterministic graph code that calls tools
directly; the LLM decides only *how* to edit code.

## Consequences

- Governance doesn't depend on prompt compliance; it's enforced in code and tested.
- About 100 lines of adapter code to maintain instead of a dependency.
- The workspace server itself still enforces sandbox rules (path checks, command allowlist), so a
  bug in the client can't widen access (defence in depth).
