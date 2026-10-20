"""Who may do what. One place that defines each agent's tools, prompt and output.

Least privilege: an agent is only shown, and may only call, the tools in its set. The Toolbox
enforces it on every call, so a prompt injection that asks the reviewer to write a file, or the
coder to push, is blocked and audited as a `policy_block`.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from coding_agent.agent.schemas import Plan, ReviewVerdict, RouteDecision

READ_TOOLS = frozenset({"list_files", "read_file", "search_code", "search_codebase"})

# Deterministic steps (no LLM)
SETUP_TOOLS = frozenset({"git_clone", "list_files"})
INDEXER_TOOLS = frozenset({"build_index"})
TESTER_TOOLS = frozenset({"run_tests"})
PACKAGER_TOOLS = frozenset({"git_diff", "export_patch"})
PUBLISHER_TOOLS = frozenset({"git_clone", "apply_patch", "git_commit_and_push", "open_pull_request"})

# LLM agents
PLANNER_TOOLS = READ_TOOLS
CODER_TOOLS = READ_TOOLS | {"write_file", "replace_in_file"}
REVIEWER_TOOLS = READ_TOOLS | {"git_diff"}
SUPERVISOR_TOOLS: frozenset[str] = frozenset()  # decides from a state summary only


@dataclass(frozen=True)
class Role:
    name: str
    prompt: str  # prompt template name in the registry
    tools: frozenset[str]
    output: type[BaseModel] | None = None  # structured result, returned through `submit_tool`
    submit_tool: str | None = None
    submit_description: str = ""
    max_turns: int = 12


PLANNER = Role(
    name="planner",
    prompt="planner",
    tools=PLANNER_TOOLS,
    output=Plan,
    submit_tool="submit_plan",
    submit_description="Submit the implementation plan. Call this exactly once, when you are done exploring.",
)
CODER = Role(name="coder", prompt="coder", tools=CODER_TOOLS, max_turns=25)
REVIEWER = Role(
    name="reviewer",
    prompt="reviewer",
    tools=REVIEWER_TOOLS,
    output=ReviewVerdict,
    submit_tool="submit_review",
    submit_description="Submit your review verdict and findings. Call this exactly once.",
    max_turns=10,
)
SUPERVISOR = Role(
    name="supervisor",
    prompt="supervisor",
    tools=SUPERVISOR_TOOLS,
    output=RouteDecision,
    submit_tool="choose_next_step",
    submit_description="Choose the next step from the options you were given.",
    max_turns=2,
)

LLM_ROLES = {r.name: r for r in (PLANNER, CODER, REVIEWER, SUPERVISOR)}
