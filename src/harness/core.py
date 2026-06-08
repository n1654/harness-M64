"""Core API — the only contract between clients (UI / control / monitor) and the agent.

Every external surface (HTMX/SSE web, line-based TCP control, Prometheus monitor)
holds a reference to a `CoreAPI` and calls its verbs. The agent loop itself is
hidden behind this facade — you can run the harness with zero surfaces enabled
and it still functions if you call `submit_prompt` programmatically.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional


@dataclass
class Session:
    """One prompt → response interaction."""

    id: str
    prompt: str
    created_at: float = field(default_factory=time.time)
    status: str = "pending"   # pending | running | done | failed | cancelled
    response_chunks: asyncio.Queue = field(default_factory=asyncio.Queue)


@dataclass
class Status:
    """Snapshot of harness state, returned by Core.get_status()."""

    uptime_sec: float
    running: bool
    sessions_total: int
    sessions_active: int
    tools_loaded: int


class CoreAPI:
    """Facade — call these verbs from any surface.

    Lifecycle: `from_env()` → `await start()` → use → `await stop()`.
    `run_forever()` keeps the agent loop alive after `start()` returns.
    """

    # ---- construction ----

    def __init__(
        self,
        *,
        memory_dir: pathlib.Path,
        tools_dir: pathlib.Path,
        state_dir: pathlib.Path,
    ) -> None:
        from harness.bus import EventBus

        self._memory_dir = memory_dir
        self._tools_dir = tools_dir
        self._state_dir = state_dir
        self._started_at: Optional[float] = None
        self._sessions: Dict[str, Session] = {}
        self._bus = EventBus()
        self._agent = None    # type: ignore[assignment]    # lazy: set in start()

    @classmethod
    def from_env(cls) -> "CoreAPI":
        """Read HARNESS_* env vars and build a Core."""
        return cls(
            memory_dir=pathlib.Path(os.environ.get("HARNESS_MEMORY_DIR", "memory")),
            tools_dir=pathlib.Path(os.environ.get("HARNESS_TOOLS_DIR", "tools")),
            state_dir=pathlib.Path(os.environ.get("HARNESS_STATE_DIR", "state")),
        )

    # ---- lifecycle ----

    async def start(self) -> None:
        """Initialise tools, memory, LLM client, agent loop. Idempotent."""
        from harness.agent import Agent

        if self._started_at is not None:
            return
        self._started_at = time.time()
        self._agent = Agent(
            bus=self._bus,
            memory_dir=self._memory_dir,
            tools_dir=self._tools_dir,
            state_dir=self._state_dir,
        )
        await self._agent.start()

    async def run_forever(self) -> None:
        """Block until shutdown is requested."""
        # TODO: replace with a real shutdown event.
        while True:
            await asyncio.sleep(3600)

    async def stop(self, *, emergency: bool = False) -> None:
        """Graceful shutdown (or `emergency=True` to abort running sessions)."""
        if self._agent is not None:
            await self._agent.stop(emergency=emergency)

    # ---- client verbs ----

    async def submit_prompt(self, prompt: str) -> str:
        """Enqueue a prompt. Returns `session_id` for streaming the response."""
        sid = uuid.uuid4().hex
        session = Session(id=sid, prompt=prompt)
        self._sessions[sid] = session
        # TODO: hand off to agent loop.
        await self._bus.publish("prompt_submitted", session_id=sid, prompt=prompt)
        return sid

    async def stream_response(self, session_id: str) -> AsyncIterator[str]:
        """Yield response chunks for a given session until the session is done."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        while True:
            chunk = await session.response_chunks.get()
            if chunk is None:    # sentinel — end of stream
                return
            yield chunk

    def get_status(self) -> Status:
        return Status(
            uptime_sec=time.time() - (self._started_at or time.time()),
            running=self._started_at is not None,
            sessions_total=len(self._sessions),
            sessions_active=sum(1 for s in self._sessions.values() if s.status == "running"),
            tools_loaded=0,    # TODO: ask tool registry
        )

    def list_tools(self) -> List[str]:
        """Names of all loaded tools."""
        return []   # TODO

    def list_sessions(self, *, limit: int = 20) -> List[Session]:
        """Most recent sessions."""
        return sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)[:limit]

    # ---- internals exposed to monitor ----

    @property
    def bus(self):     # noqa: ANN201    # type defined in bus.py
        """Event bus reference for the monitor server to subscribe to."""
        return self._bus
