"""Scripted chat model for tests and offline demos: no network, no API key, deterministic.

`LLM_PROVIDER=fake` uses `demo_model()`, which solves the sample task in `evals/sample_repo`
("add a divide function"), so the full workflow can be demonstrated without a real LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

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


def demo_model() -> ScriptedChatModel:
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
