"""Scripted chat models for tests and offline demos: no network, no API key, deterministic.

`LLM_PROVIDER=fake` uses `demo_models()`: a planner, coder and reviewer that solve the sample
task in `evals/sample_repo` ("add a divide function"), so the whole multi-agent workflow can be
demonstrated without a real LLM.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from langchain_core.messages import AIMessage, BaseMessage


class ScriptedChatModel:
    """Returns the scripted responses in order, then a plain 'done' message."""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self._responses = list(responses)
        self.received: list[list[BaseMessage]] = []

    async def ainvoke(self, input: list[BaseMessage], /) -> BaseMessage:
        self.received.append(list(input))
        if self._responses:
            return self._responses.pop(0)
        return AIMessage(content="Done.")


def tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def submit_plan(**overrides: Any) -> AIMessage:
    plan: dict[str, Any] = {
        "summary": "Make the smallest change that completes the task, with tests.",
        "files_to_change": [],
        "steps": ["Implement the change", "Add tests"],
        "test_strategy": "Run the existing test suite plus the new tests.",
        "risks": [],
    }
    return tool_call("submit_plan", {**plan, **overrides}, "plan")


def submit_review(verdict: str = "approve", findings: list[dict[str, Any]] | None = None) -> AIMessage:
    return tool_call(
        "submit_review",
        {"verdict": verdict, "summary": f"Scripted review: {verdict}.", "findings": findings or []},
        "review",
    )


class ScriptedModels:
    """Per-role scripted models. Unscripted roles get a fresh default each time: the planner
    submits a minimal plan, the reviewer approves, the supervisor gives no answer (so routing
    falls back to the deterministic choice)."""

    DEFAULTS: ClassVar[dict[str, Callable[[], list[AIMessage]]]] = {
        "planner": lambda: [submit_plan()],
        "reviewer": lambda: [submit_review()],
    }

    def __init__(self, **scripts: ScriptedChatModel) -> None:
        self.scripts = scripts
        self.tools_seen: dict[str, list[str]] = {}

    def for_role(self, role: str, tools: list[dict[str, Any]]) -> ScriptedChatModel:
        self.tools_seen[role] = sorted(t["function"]["name"] for t in tools)
        if role in self.scripts:
            return self.scripts[role]
        return ScriptedChatModel(self.DEFAULTS.get(role, list)())


DIVIDE_IMPL = """

def divide(a: float, b: float) -> float:
    if b == 0:
        raise ValueError("cannot divide by zero")
    return a / b
"""

DIVIDE_TESTS = """import pytest

from calculator import divide


def test_divide() -> None:
    assert divide(10, 4) == 2.5


def test_divide_by_zero() -> None:
    with pytest.raises(ValueError):
        divide(1, 0)
"""


def demo_models() -> ScriptedModels:
    return ScriptedModels(
        planner=ScriptedChatModel(
            [
                tool_call("search_codebase", {"query": "calculator arithmetic functions"}, "p1"),
                submit_plan(
                    summary="Add divide() next to the other operations in calculator.py and test it.",
                    files_to_change=[
                        {"path": "calculator.py", "change": "Add divide(a, b) raising ValueError on zero"},
                        {"path": "tests/test_divide.py", "change": "New tests for division and zero"},
                    ],
                    steps=["Add divide() after multiply()", "Write tests for 10/4 and division by zero"],
                    test_strategy="pytest: normal division and the ValueError case.",
                    risks=["Integer vs float results"],
                ),
            ]
        ),
        coder=demo_coder(),
        reviewer=ScriptedChatModel([submit_review()]),
    )


def demo_coder() -> ScriptedChatModel:
    return ScriptedChatModel(
        [
            tool_call("read_file", {"path": "calculator.py"}, "call_1"),
            tool_call(
                "replace_in_file",
                {
                    "path": "calculator.py",
                    "old": "def multiply(a: float, b: float) -> float:\n    return a * b\n",
                    "new": "def multiply(a: float, b: float) -> float:\n    return a * b\n" + DIVIDE_IMPL,
                },
                "call_2",
            ),
            tool_call("write_file", {"path": "tests/test_divide.py", "content": DIVIDE_TESTS}, "call_3"),
            AIMessage(
                content="Added divide(a, b) to calculator.py, raising ValueError on zero, "
                "and tests in tests/test_divide.py."
            ),
        ]
    )
