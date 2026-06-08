"""Core API — the only contract between clients (UI / control / monitor) and the agent.

Every external surface (HTMX/SSE web, line-based TCP control, Prometheus monitor)
holds a reference to a `CoreAPI` and calls its verbs. The agent loop itself is
hidden behind this facade — you can run the harness with zero surfaces enabled
and it still functions if you call `submit_prompt` programmatically.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional, Set

log = logging.getLogger("harness.core")


_END_OF_STREAM = object()    # sentinel pushed to the chunk queue when a session is done


@dataclass
class Session:
    """One prompt -> response interaction."""

    id: str
    prompt: str
    created_at: float = field(default_factory=time.time)
    status: str = "pending"   # pending | running | done | failed
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

    Lifecycle: `from_env()` -> `await start()` -> use -> `await stop()`.
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
        self._session_tasks: Set[asyncio.Task] = set()
        self._bus = EventBus()
        self._agent = None    # set in start()

    @classmethod
    def from_env(cls) -> "CoreAPI":
        return cls(
            memory_dir=pathlib.Path(os.environ.get("HARNESS_MEMORY_DIR", "memory")),
            tools_dir=pathlib.Path(os.environ.get("HARNESS_TOOLS_DIR", "tools")),
            state_dir=pathlib.Path(os.environ.get("HARNESS_STATE_DIR", "state")),
        )

    # ---- lifecycle ----

    async def start(self) -> None:
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
        # Sleeps forever; explicit shutdown signal will be added with control.stop wiring.
        while True:
            await asyncio.sleep(3600)

    async def stop(self, *, emergency: bool = False) -> None:
        # Cancel running sessions on emergency.
        if emergency:
            for t in list(self._session_tasks):
                t.cancel()
        if self._agent is not None:
            await self._agent.stop(emergency=emergency)

    # ---- client verbs ----

    async def submit_prompt(self, prompt: str) -> str:
        """Enqueue a prompt. Returns `session_id` for streaming the response."""
        if self._agent is None:
            raise RuntimeError("CoreAPI.start() not called yet")

        sid = uuid.uuid4().hex
        session = Session(id=sid, prompt=prompt)
        self._sessions[sid] = session

        await self._bus.publish("prompt_submitted", session_id=sid, prompt=prompt)

        task = asyncio.create_task(self._run_session(session), name=f"session-{sid[:8]}")
        self._session_tasks.add(task)
        task.add_done_callback(self._session_tasks.discard)

        return sid

    async def _run_session(self, session: Session) -> None:
        """Drive one session: agent.handle -> push chunks -> close stream."""
        assert self._agent is not None
        session.status = "running"
        try:
            content = await self._agent.handle(session.id, session.prompt)
            await session.response_chunks.put(content or "(empty response)")
            session.status = "done"
        except asyncio.CancelledError:
            session.status = "failed"
            await session.response_chunks.put("⚠️ session cancelled")
            raise
        except Exception as e:    # noqa: BLE001
            log.exception("session %s failed", session.id)
            session.status = "failed"
            await session.response_chunks.put(f"⚠️ error: {type(e).__name__}: {e}")
        finally:
            # Sentinel — signals stream_response() to return.
            await session.response_chunks.put(_END_OF_STREAM)

    async def stream_response(self, session_id: str) -> AsyncIterator[str]:
        """Yield response chunks for a given session until the session is done."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        while True:
            chunk = await session.response_chunks.get()
            if chunk is _END_OF_STREAM:
                return
            yield chunk    # type: ignore[misc]

    def get_status(self) -> Status:
        tools_loaded = 0
        if self._agent is not None and self._agent.tools is not None:
            tools_loaded = len(self._agent.tools.names())
        return Status(
            uptime_sec=time.time() - (self._started_at or time.time()),
            running=self._started_at is not None and self._agent is not None and self._agent.running,
            sessions_total=len(self._sessions),
            sessions_active=sum(1 for s in self._sessions.values() if s.status == "running"),
            tools_loaded=tools_loaded,
        )

    def list_tools(self) -> List[str]:
        if self._agent is None or self._agent.tools is None:
            return []
        return self._agent.tools.names()

    def list_sessions(self, *, limit: int = 20) -> List[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)[:limit]

    # ---- internals exposed to monitor ----

    @property
    def bus(self):    # noqa: ANN201
        return self._bus
