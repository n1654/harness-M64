"""Agent loop — owns the LLM client, tools, memory; executes prompts.

Not exposed to clients directly; everything goes through `CoreAPI`.
"""

from __future__ import annotations

import logging
import pathlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.bus import EventBus

log = logging.getLogger("harness.agent")


class Agent:
    """Single-tenant agent loop. One per Core."""

    def __init__(
        self,
        *,
        bus: "EventBus",
        memory_dir: pathlib.Path,
        tools_dir: pathlib.Path,
        state_dir: pathlib.Path,
    ) -> None:
        self._bus = bus
        self._memory_dir = memory_dir
        self._tools_dir = tools_dir
        self._state_dir = state_dir
        self._running = False
        # Wired in start():
        self._llm = None    # harness.llm.base.LLMClient
        self._memory = None    # harness.memory.base.MemoryStore
        self._tools = None    # harness.tools.registry.ToolRegistry

    async def start(self) -> None:
        from harness.llm.gigachat import GigaChatClient
        from harness.memory.files import FileMemoryStore
        from harness.tools.registry import ToolRegistry

        self._llm = GigaChatClient.from_env()
        self._memory = FileMemoryStore(self._memory_dir)
        self._tools = ToolRegistry(self._tools_dir)
        self._running = True
        log.info("agent started")

    async def stop(self, *, emergency: bool = False) -> None:
        self._running = False
        log.info("agent stopped (emergency=%s)", emergency)

    async def handle(self, session_id: str, prompt: str) -> None:
        """Run one prompt -> response cycle. TODO: real loop with tool calls."""
        await self._bus.publish("agent_round_start", session_id=session_id)
        # TODO:
        # 1. build messages = memory.read_levels() + prompt
        # 2. while not terminal:
        #      msg = await llm.chat(messages, tools=tools.schemas())
        #      if msg.tool_calls: execute and append; else: stream content & finish
        # 3. write episode to memory
        await self._bus.publish("agent_round_end", session_id=session_id)
        raise NotImplementedError("agent loop -- next milestone")
