"""Structured outputs of the planner, reviewer and supervisor agents.

Agents return these by calling a "submit" tool whose arguments must validate against the model.
That works with any tool-calling LLM, and an invalid answer is sent back to the agent with the
validation error instead of crashing the job.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class FileChange(BaseModel):
    path: str = Field(description="Repository-relative path, e.g. calculator.py")
    change: str = Field(description="What to change in this file and why, in one or two sentences")


class Plan(BaseModel):
    summary: str = Field(description="One or two sentences: the approach")
    files_to_change: list[FileChange] = Field(description="Files to create or edit")
    steps: list[str] = Field(description="Ordered implementation steps")
    test_strategy: str = Field(description="Which tests to add or update, and what they prove")
    risks: list[str] = Field(default_factory=list, description="Edge cases or things that could break")

    def as_markdown(self) -> str:
        files = "\n".join(f"- `{f.path}`: {f.change}" for f in self.files_to_change) or "- (none listed)"
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(self.steps, start=1)) or "1. (none listed)"
        risks = "\n".join(f"- {r}" for r in self.risks) or "- (none listed)"
        return (
            f"**Approach:** {self.summary}\n\n**Files:**\n{files}\n\n**Steps:**\n{steps}\n\n"
            f"**Tests:** {self.test_strategy}\n\n**Risks:**\n{risks}"
        )


Severity = Literal["high", "medium", "low"]


class Finding(BaseModel):
    severity: Severity = Field(description="high: wrong or unsafe; medium: should fix; low: nit")
    file: str
    line: int | None = Field(default=None, description="Line in the new version of the file, if known")
    issue: str
    suggestion: str = ""


class ReviewVerdict(BaseModel):
    verdict: Literal["approve", "request_changes"]
    summary: str = Field(description="Two or three sentences for the human approver")
    findings: list[Finding] = Field(default_factory=list)

    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in ("high", "medium")]

    def as_markdown(self) -> str:
        lines = [f"**Verdict:** {self.verdict}", "", self.summary]
        for f in self.findings:
            where = f"{f.file}:{f.line}" if f.line else f.file
            lines.append(
                f"- [{f.severity}] `{where}`: {f.issue}" + (f" Fix: {f.suggestion}" if f.suggestion else "")
            )
        return "\n".join(lines)


class RouteDecision(BaseModel):
    next: str = Field(description="One of the options you were given")
    reason: str = Field(description="One sentence")


def submit_tool_schema(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    """OpenAI-style function schema for a submit tool, with $refs inlined (some providers reject them)."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(definitions[node["$ref"].rsplit("/", 1)[-1]])
            # drop "title" annotations (noise for the model), but not a property named "title"
            return {k: inline(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": inline(schema)},
    }
