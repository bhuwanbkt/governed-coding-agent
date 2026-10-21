from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from coding_agent.agent.fake_llm import ScriptedChatModel, ScriptedModels
from coding_agent.agent.llm import LLMConfigError, build_models, message_text
from coding_agent.config import Settings


def test_groq_requires_api_key() -> None:
    settings = Settings(_env_file=None, llm_provider="groq", groq_api_key="")  # type: ignore[call-arg]
    with pytest.raises(LLMConfigError, match="GROQ_API_KEY"):
        build_models(settings)


def test_fake_provider_needs_no_key() -> None:
    settings = Settings(_env_file=None, llm_provider="fake")  # type: ignore[call-arg]
    models = build_models(settings)
    assert isinstance(models, ScriptedModels)
    assert isinstance(models.for_role("coder", []), ScriptedChatModel)


def test_scripted_models_give_unscripted_roles_a_fresh_default() -> None:
    coder = ScriptedChatModel([])
    models = ScriptedModels(coder=coder)
    assert models.for_role("coder", []) is coder
    assert models.for_role("planner", []) is not models.for_role("planner", [])


def test_scripted_model_replays_then_finishes() -> None:
    model = ScriptedChatModel([AIMessage(content="first")])
    first = asyncio.run(model.ainvoke([HumanMessage("hi")]))
    second = asyncio.run(model.ainvoke([HumanMessage("again")]))
    assert message_text(first) == "first"
    assert message_text(second) == "Done."
    assert len(model.received) == 2


def test_message_text_handles_content_blocks() -> None:
    message = AIMessage(content=[{"type": "text", "text": "a"}, "b", {"type": "image_url", "image_url": {}}])
    assert message_text(message) == "ab"


def test_errors_are_reported_by_their_real_cause() -> None:
    from coding_agent.agent.runner import describe

    wrapped = ExceptionGroup("unhandled errors in a TaskGroup", [ValueError("the real problem")])
    assert describe(wrapped) == "ValueError: the real problem"
    assert describe(KeyError("x")) == "KeyError: 'x'"
