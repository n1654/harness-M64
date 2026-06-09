"""Task history + FSM state tracker.

Subscribes to the bus, accumulates per-task records (one per submitted prompt)
and maintains a single "current state" describing where the agent is right now
(idle / prompted / llm / tool:<name>).

Lives entirely in-memory; cleared by `reset()` (called from the bus
`factory_reset` event). For long-term records use the episodes JSONL in
`memory/episodes/`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from harness.bus import Event


@dataclass
class Step:
    """One step inside a task: LLM call or tool call."""

    kind: str                       # "llm_call" | "tool_call"
    name: str = ""                  # model name or tool name
    started_at: float = 0.0
    finished_at: Optional[float] = None
    ok: Optional[bool] = None
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_sec(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return round(self.finished_at - self.started_at, 3)


@dataclass
class Task:
    """One submitted prompt and everything that happened until its reply."""

    session_id: str
    prompt: str
    started_at: float
    finished_at: Optional[float] = None
    status: str = "running"         # "running" | "done" | "failed"
    steps: List[Step] = field(default_factory=list)
    total_tokens: int = 0
    error: Optional[str] = None
    restored: bool = False

    @property
    def duration_sec(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return round(self.finished_at - self.started_at, 3)


@dataclass
class Counters:
    """Snapshot counters shown in the dashboard; reset on factory_reset."""

    prompts: int = 0
    rounds_ok: int = 0
    rounds_fail: int = 0
    llm_calls: int = 0
    llm_errors: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    last_llm_latency_sec: float = 0.0
    last_round_latency_sec: float = 0.0


# FSM nodes (in display order). Tool sub-name (e.g. "tool:now") is collapsed
# to "tool" for the global state; the active tool name is exposed separately
# so the UI can show e.g. "tool: now".
FSM_NODES = ("idle", "prompted", "llm", "tool", "reply")


class TaskHistorySink:
    """One-stop sink for the dashboard. Subscribes to the bus."""

    def __init__(self, max_tasks: int = 200) -> None:
        self.max = max_tasks
        self.tasks: Dict[str, Task] = {}
        self.order: List[str] = []          # session_ids, oldest first
        self.counters = Counters()

        self.current_state: str = "idle"
        self.current_tool: Optional[str] = None
        self.current_model: Optional[str] = None
        self.current_session: Optional[str] = None

    # ---- bus ----

    async def consume(self, evt: "Event") -> None:
        kind = evt.kind
        d = evt.data
        sid = str(d.get("session_id") or "")

        if kind == "prompt_submitted":
            self._on_prompt(sid, d, evt.ts)
        elif kind == "session_restored":
            self._on_prompt(sid, d, evt.ts, restored=True)
        elif kind == "agent_round_start":
            self._set_state("prompted", session=sid)
        elif kind == "llm_call_start":
            self._open_step(sid, Step(
                kind="llm_call",
                name=str(d.get("model") or ""),
                started_at=evt.ts,
            ))
            self.current_model = str(d.get("model") or "")
            self._set_state("llm", session=sid)
        elif kind == "llm_call_end":
            self._close_last_step(sid, "llm_call", evt.ts, ok=bool(d.get("ok")),
                                   error=d.get("error"),
                                   extra={"latency_sec": d.get("latency_sec")})
            self.counters.llm_calls += 1
            if not d.get("ok"):
                self.counters.llm_errors += 1
            if (lat := d.get("latency_sec")) is not None:
                self.counters.last_llm_latency_sec = float(lat)
            self._set_state("prompted", session=sid)    # back to "between steps"
        elif kind == "llm_usage":
            t = self.tasks.get(sid)
            if t is not None:
                t.total_tokens += int(d.get("total_tokens") or 0)
            self.counters.tokens_prompt += int(d.get("prompt_tokens") or 0)
            self.counters.tokens_completion += int(d.get("completion_tokens") or 0)
        elif kind == "tool_call_start":
            name = str(d.get("name") or "?")
            self._open_step(sid, Step(kind="tool_call", name=name, started_at=evt.ts))
            self.current_tool = name
            self._set_state("tool", session=sid)
        elif kind == "tool_call_end":
            name = str(d.get("name") or "?")
            self._close_last_step(sid, "tool_call", evt.ts,
                                   ok=bool(d.get("ok")),
                                   error=d.get("error"),
                                   name_filter=name)
            self.counters.tool_calls += 1
            if d.get("error"):
                self.counters.tool_errors += 1
            self.current_tool = None
            self._set_state("prompted", session=sid)
        elif kind == "agent_round_end":
            t = self.tasks.get(sid)
            if t is not None:
                t.finished_at = evt.ts
                t.status = "done" if d.get("ok") else "failed"
                if not d.get("ok"):
                    t.error = str(d.get("error") or "")
            if d.get("ok"):
                self.counters.rounds_ok += 1
            else:
                self.counters.rounds_fail += 1
            if (lat := d.get("latency_sec")) is not None:
                self.counters.last_round_latency_sec = float(lat)
            self._set_state("reply", session=sid)
            # Brief "reply" visibility, then idle. Caller polls again -- next
            # poll typically already sees idle because no more events arrive.
            self.current_state = "idle"
            self.current_session = None
            self.current_model = None
        elif kind == "factory_reset":
            if (d.get("scope") or "all") in ("metrics", "all"):
                self.reset()

    # ---- helpers ----

    def _on_prompt(self, sid: str, d: Dict[str, Any], ts: float, *, restored: bool = False) -> None:
        if not sid:
            return
        if sid in self.tasks:    # idempotent (restored on top of existing)
            self.tasks[sid].restored = restored or self.tasks[sid].restored
            return
        t = Task(
            session_id=sid,
            prompt=str(d.get("prompt") or "")[:300],
            started_at=ts,
            restored=restored,
        )
        self.tasks[sid] = t
        self.order.append(sid)
        while len(self.order) > self.max:
            old = self.order.pop(0)
            self.tasks.pop(old, None)
        if not restored:
            self.counters.prompts += 1
        self._set_state("prompted", session=sid)

    def _open_step(self, sid: str, step: Step) -> None:
        t = self.tasks.get(sid)
        if t is None:
            return
        t.steps.append(step)

    def _close_last_step(
        self, sid: str, kind: str, ts: float, *, ok: Optional[bool] = None,
        error: Any = None, name_filter: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        t = self.tasks.get(sid)
        if t is None:
            return
        # Find the most-recent open step of this kind (and name, if provided).
        for step in reversed(t.steps):
            if step.kind != kind or step.finished_at is not None:
                continue
            if name_filter is not None and step.name != name_filter:
                continue
            step.finished_at = ts
            step.ok = ok
            if error:
                step.error = str(error)
            if extra:
                step.extra.update(extra)
            break

    def _set_state(self, state: str, *, session: Optional[str] = None) -> None:
        self.current_state = state
        if session:
            self.current_session = session

    # ---- reset ----

    def reset(self) -> None:
        self.tasks.clear()
        self.order.clear()
        self.counters = Counters()
        self.current_state = "idle"
        self.current_tool = None
        self.current_model = None
        self.current_session = None

    # ---- snapshots for HTTP endpoints ----

    def state_snapshot(self) -> Dict[str, Any]:
        return {
            "state": self.current_state,
            "current_tool": self.current_tool,
            "current_model": self.current_model,
            "current_session": self.current_session,
            "fsm_nodes": list(FSM_NODES),
            "counters": {
                "prompts": self.counters.prompts,
                "rounds_ok": self.counters.rounds_ok,
                "rounds_fail": self.counters.rounds_fail,
                "llm_calls": self.counters.llm_calls,
                "llm_errors": self.counters.llm_errors,
                "tool_calls": self.counters.tool_calls,
                "tool_errors": self.counters.tool_errors,
                "tokens_prompt": self.counters.tokens_prompt,
                "tokens_completion": self.counters.tokens_completion,
                "last_llm_latency_sec": self.counters.last_llm_latency_sec,
                "last_round_latency_sec": self.counters.last_round_latency_sec,
            },
            "ts": time.time(),
        }

    def history_snapshot(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        sids = list(reversed(self.order[-limit:]))    # most recent first
        out: List[Dict[str, Any]] = []
        for sid in sids:
            t = self.tasks.get(sid)
            if t is None:
                continue
            out.append({
                "session_id": t.session_id,
                "prompt": t.prompt,
                "started_at": t.started_at,
                "finished_at": t.finished_at,
                "duration_sec": t.duration_sec,
                "status": t.status,
                "error": t.error,
                "restored": t.restored,
                "total_tokens": t.total_tokens,
                "step_count": len(t.steps),
                "steps": [
                    {
                        "kind": s.kind,
                        "name": s.name,
                        "ok": s.ok,
                        "error": s.error,
                        "duration_sec": s.duration_sec,
                    }
                    for s in t.steps
                ],
            })
        return out
