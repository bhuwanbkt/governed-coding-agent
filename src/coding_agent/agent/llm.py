"""LLM construction. Agents depend on the small `ToolCallingModel` protocol, not on a vendor SDK,
so providers can be swapped by configuration and tests can use a scripted fake."""

from __future__ import annotations

from typing import Any, Protocol, cast

from langchain_core.messages import BaseMessage

from coding_agent.config import Settings


class ToolCallingModel(Protocol):
    async def ainvoke(self, input: list[BaseMessage], /) -> BaseMessage: ...


class ModelProvider(Protocol):
    """Gives each agent role a chat model with exactly that role's tools bound."""

    def for_role(self, role: str, tools: list[dict[str, Any]]) -> ToolCallingModel: ...


class LLMConfigError(RuntimeError):
    pass


class GroqModels:
    def __init__(self, settings: Settings) -> None:
        if settings.groq_api_key is None:
            raise LLMConfigError("GROQ_API_KEY is not set (add it to .env or the environment)")
        from langchain_groq import ChatGroq  # imported lazily: tests never need the SDK

        options: dict[str, Any] = {
            "model": settings.llm_model,
            "temperature": settings.llm_temperature,
            "api_key": settings.groq_api_key,
            "timeout": settings.llm_timeout_seconds,
            "max_retries": 3,
        }
        self._base = ChatGroq(**options)

    def for_role(self, role: str, tools: list[dict[str, Any]]) -> ToolCallingModel:
        return cast(ToolCallingModel, self._base.bind_tools(tools) if tools else self._base)


def build_models(settings: Settings) -> ModelProvider:
    """Models for the configured provider."""
    if settings.llm_provider == "groq":
        return GroqModels(settings)
    if settings.llm_provider == "fake":
        from coding_agent.agent.fake_llm import demo_models

        return demo_models()
    raise LLMConfigError(f"unknown LLM provider {settings.llm_provider!r}")


def message_text(message: BaseMessage) -> str:
    """Plain text of a message whose content may be a string or a list of content blocks."""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)
