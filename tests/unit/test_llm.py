from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from coding_agent.agent.fake_llm import ScriptedChatModel
from coding_agent.agent.llm import LLMConfigError, build_chat_model, message_text
from coding_agent.config import Settings


def test_groq_requires_api_key() -> None:
    settings = Settings(_env_file=None, llm_provider="groq", groq_api_key="")  # type: ignore[call-arg]
    with pytest.raises(LLMConfigError, match="GROQ_API_KEY"):
        build_chat_model(settings, [])


def test_fake_provider_needs_no_key() -> None:
    settings = Settings(_env_file=None, llm_provider="fake")  # type: ignore[call-arg]
    assert isinstance(build_chat_model(settings, []), ScriptedChatModel)


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
