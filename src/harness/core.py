"""Core API — the only contract between clients (UI / control / monitor) and the agent.

Every external surface (HTMX/SSE web, line-based TCP control, Prometheus monitor)
holds a reference to a `CoreAPI` and calls its verbs. The agent loop itself is
hidden behind this facade — you can run the harness with zero surfaces enabled
and it still functions if you call `submit_prompt` programmatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Set

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
class ChatMessage:
    """One turn in the visible chat thread (user prompt or final assistant reply).

    Tool calls / tool results live INSIDE one round and are never logged here --
    only the final assistant answer the user sees.
    """

    role: str      # "user" | "assistant"
    content: str
    ts: float = field(default_factory=time.time)


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

        # Persistent chat thread (multi-turn memory across prompts).
        self._chat_history: List[ChatMessage] = []
        self._chat_lock = asyncio.Lock()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._chat_log_path = self._state_dir / "chat.jsonl"

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
        self._chat_history = self._load_chat_history()
        log.info("chat history: loaded %d messages from %s", len(self._chat_history), self._chat_log_path)
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

        # Snapshot the chat history BEFORE adding the new user turn -- the agent
        # gets the prior conversation as context, plus the prompt as the user
        # message. We append the user turn and persist it under the same lock.
        async with self._chat_lock:
            prior_history = list(self._chat_history)
            user_msg = ChatMessage(role="user", content=prompt)
            self._chat_history.append(user_msg)
            self._append_chat_log(user_msg)

        await self._bus.publish("prompt_submitted", session_id=sid, prompt=prompt)

        task = asyncio.create_task(
            self._run_session(session, prior_history), name=f"session-{sid[:8]}",
        )
        self._session_tasks.add(task)
        task.add_done_callback(self._session_tasks.discard)

        return sid

    async def _run_session(self, session: Session, prior_history: List[ChatMessage]) -> None:
        """Drive one session: agent.handle -> push chunks -> persist assistant turn."""
        assert self._agent is not None
        session.status = "running"
        try:
            content = await self._agent.handle(session.id, session.prompt, history=prior_history)
            content = content or "(empty response)"
            await session.response_chunks.put(content)

            async with self._chat_lock:
                asst_msg = ChatMessage(role="assistant", content=content)
                self._chat_history.append(asst_msg)
                self._append_chat_log(asst_msg)

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

    def get_chat_history(self, *, limit: Optional[int] = None) -> List[ChatMessage]:
        """Snapshot of the visible chat thread. Most recent at the end."""
        if limit is None:
            return list(self._chat_history)
        return list(self._chat_history[-limit:])

    # ---- factory reset ----

    async def reset(self, scope: str = "chat") -> Dict[str, Any]:
        """Wipe ephemeral state. `scope`:

            'chat'  -- only the visible chat thread (state/chat.jsonl + in-memory).
            'all'   -- chat + agent scratchpad + knowledge + episodes + state files.
                       Operator-managed files (memory/level_*.md, tools/) are NEVER touched.

        Returns a dict describing what was removed.
        """
        scope = (scope or "chat").strip().lower()
        if scope not in ("chat", "all"):
            raise ValueError(f"unknown reset scope: {scope!r} (expected 'chat' or 'all')")

        removed: Dict[str, Any] = {"scope": scope, "paths": []}

        async with self._chat_lock:
            n = len(self._chat_history)
            self._chat_history = []
            if self._chat_log_path.exists():
                self._chat_log_path.unlink()
                removed["paths"].append(str(self._chat_log_path))
            removed["chat_messages_dropped"] = n

        if scope == "all":
            # Wipe agent-writable memory subtrees (level_*.md NOT touched).
            for sub in ("scratchpad.md",):
                p = self._memory_dir / sub
                if p.exists():
                    p.unlink()
                    removed["paths"].append(str(p))
            for sub in ("knowledge", "episodes"):
                p = self._memory_dir / sub
                if p.is_dir():
                    shutil.rmtree(p)
                    p.mkdir(parents=True, exist_ok=True)    # keep the dir alive
                    removed["paths"].append(str(p) + "/*")

            # Wipe state files except the chat log we already removed.
            if self._state_dir.is_dir():
                for entry in self._state_dir.iterdir():
                    if entry.is_file():
                        entry.unlink()
                        removed["paths"].append(str(entry))
                    elif entry.is_dir():
                        shutil.rmtree(entry)
                        removed["paths"].append(str(entry) + "/")

        await self._bus.publish("factory_reset", scope=scope, removed_paths=removed["paths"])
        log.info("factory reset (scope=%s): removed %s", scope, removed["paths"])
        return removed

    # ---- internals exposed to monitor ----

    @property
    def bus(self):    # noqa: ANN201
        return self._bus

    # ---- chat persistence ----

    def _load_chat_history(self) -> List[ChatMessage]:
        if not self._chat_log_path.exists():
            return []
        out: List[ChatMessage] = []
        try:
            for line in self._chat_log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(ChatMessage(
                        role=str(d.get("role") or "user"),
                        content=str(d.get("content") or ""),
                        ts=float(d.get("ts") or time.time()),
                    ))
                except (ValueError, TypeError):
                    log.warning("skipping malformed chat log line: %s", line[:80])
        except OSError:
            log.warning("failed to read chat log %s", self._chat_log_path, exc_info=True)
        return out

    def _append_chat_log(self, msg: ChatMessage) -> None:
        try:
            with self._chat_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"role": msg.role, "content": msg.content, "ts": msg.ts},
                    ensure_ascii=False,
                ) + "\n")
        except OSError:
            log.warning("failed to append to chat log %s", self._chat_log_path, exc_info=True)
