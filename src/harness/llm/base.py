"""LLM client interface — the only thing the agent depends on.

Adding a new provider == one new class implementing `LLMClient`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclass
class Message:
    """Chat message in the OpenAI-compatible shape.

    `provider_meta` is an escape hatch for provider-specific fields that don't
    fit OpenAI's schema -- e.g. GigaChat's `functions_state_id`. The agent loop
    treats it opaquely; only the LLM adapter reads/writes it.
    """

    role: str    # "system" | "user" | "assistant" | "tool"
    content: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    provider_meta: Optional[Dict[str, Any]] = None


@dataclass
class Usage:
    """Per-call usage; provider may leave fields empty."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class CompletionResult:
    message: Message
    usage: Usage = field(default_factory=Usage)
    raw: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMClient(Protocol):
    """Provider-agnostic LLM contract. Sync simple, async preferred."""

    async def chat(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> CompletionResult:
        """One chat completion turn. May or may not return `tool_calls`."""
        ...
