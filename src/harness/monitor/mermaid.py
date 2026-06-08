"""Build a Mermaid `graph LR` source from bus events.

Stable plain syntax only: `id["label"]`, `A --> B`, `classDef`, `class id name`.
No subgraphs, no fancy macros -- those break across mermaid.js versions.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from harness.bus import Event


@dataclass
class _ToolStats:
    calls: int = 0
    errors: int = 0
    active: int = 0
    last_used_ts: float = 0.0


@dataclass
class _LLMStats:
    calls: int = 0
    errors: int = 0
    active: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    last_latency_sec: float = 0.0
    last_used_ts: float = 0.0


@dataclass
class _AgentStats:
    prompts: int = 0
    rounds_ok: int = 0
    rounds_fail: int = 0
    active: int = 0
    last_round_latency_sec: float = 0.0


_ACTIVE_HIGHLIGHT_SEC = 2.0    # node is "active" for this long after its last event


@dataclass
class MermaidGraph:
    """Stateful graph. Updated from bus events; rendered on demand."""

    agent: _AgentStats = field(default_factory=_AgentStats)
    llm: _LLMStats = field(default_factory=_LLMStats)
    tools: Dict[str, _ToolStats] = field(default_factory=lambda: defaultdict(_ToolStats))

    async def consume(self, evt: "Event") -> None:
        d = evt.data
        kind = evt.kind
        now = time.time()

        if kind == "prompt_submitted":
            self.agent.prompts += 1
            self.agent.active += 1

        elif kind == "agent_round_end":
            if d.get("ok"):
                self.agent.rounds_ok += 1
            else:
                self.agent.rounds_fail += 1
            self.agent.active = max(0, self.agent.active - 1)
            if (lat := d.get("latency_sec")) is not None:
                self.agent.last_round_latency_sec = float(lat)

        elif kind == "llm_call_start":
            self.llm.active += 1
            self.llm.last_used_ts = now

        elif kind == "llm_call_end":
            self.llm.calls += 1
            self.llm.active = max(0, self.llm.active - 1)
            if not d.get("ok"):
                self.llm.errors += 1
            if (lat := d.get("latency_sec")) is not None:
                self.llm.last_latency_sec = float(lat)
            self.llm.last_used_ts = now

        elif kind == "llm_usage":
            self.llm.prompt_tokens += int(d.get("prompt_tokens") or 0)
            self.llm.completion_tokens += int(d.get("completion_tokens") or 0)

        elif kind == "tool_call_start":
            name = str(d.get("name") or "?")
            t = self.tools[name]
            t.active += 1
            t.last_used_ts = now

        elif kind == "tool_call_end":
            name = str(d.get("name") or "?")
            t = self.tools[name]
            t.active = max(0, t.active - 1)
            t.calls += 1
            if d.get("error"):
                t.errors += 1
            t.last_used_ts = now

    def register_tool_names(self, names: List[str]) -> None:
        """Pre-populate the graph with discovered tools, so they appear even before any call."""
        for n in names:
            _ = self.tools[n]    # touch -> defaultdict creates an empty _ToolStats

    def render(self) -> str:
        """Emit Mermaid `graph LR` source. ASCII-safe, no version-fragile macros."""
        now = time.time()
        lines: List[str] = ["graph LR"]

        # User -> Agent.
        lines.append(f'  user(["User"])')

        agent_label = (
            f"Agent\\n"
            f"prompts: {self.agent.prompts}\\n"
            f"ok: {self.agent.rounds_ok}  fail: {self.agent.rounds_fail}\\n"
            f"active: {self.agent.active}"
        )
        lines.append(f'  agent["{agent_label}"]')

        llm_label = (
            f"LLM\\n"
            f"calls: {self.llm.calls}  err: {self.llm.errors}\\n"
            f"tokens in/out: {self.llm.prompt_tokens}/{self.llm.completion_tokens}\\n"
            f"last latency: {self.llm.last_latency_sec:.2f}s"
        )
        lines.append(f'  llm["{llm_label}"]')

        lines.append("  user --> agent")
        lines.append("  agent --> llm")

        # Tool nodes (only if any are known).
        for name in sorted(self.tools):
            stats = self.tools[name]
            node_id = _sanitize(name)
            label = f"{name}\\ncalls: {stats.calls}  err: {stats.errors}"
            if stats.active:
                label += f"\\n[{stats.active} running]"
            lines.append(f'  {node_id}["{label}"]')
            lines.append(f"  agent --> {node_id}")

        # Active highlighting.
        active_ids: List[str] = []
        if self.agent.active:
            active_ids.append("agent")
        if self.llm.active or (now - self.llm.last_used_ts) < _ACTIVE_HIGHLIGHT_SEC:
            active_ids.append("llm")
        for name, stats in self.tools.items():
            if stats.active or (now - stats.last_used_ts) < _ACTIVE_HIGHLIGHT_SEC:
                active_ids.append(_sanitize(name))

        lines.append("  classDef active fill:#fa5,stroke:#a30,stroke-width:2px")
        lines.append("  classDef err fill:#fcc,stroke:#a00")
        if active_ids:
            lines.append(f"  class {','.join(active_ids)} active")

        # Mark nodes with non-zero error counters as `err` (overrides active if both -- last class wins).
        err_ids: List[str] = []
        if self.llm.errors:
            err_ids.append("llm")
        if self.agent.rounds_fail:
            err_ids.append("agent")
        for name, stats in self.tools.items():
            if stats.errors:
                err_ids.append(_sanitize(name))
        if err_ids:
            lines.append(f"  class {','.join(err_ids)} err")

        return "\n".join(lines)


def _sanitize(name: str) -> str:
    """Mermaid node id — letters/digits/underscore only."""
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
    # Mermaid IDs can't start with a digit.
    if out and out[0].isdigit():
        out = "_" + out
    return out or "_"
