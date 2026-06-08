"""Build a Mermaid `graph LR` source from bus events.

Kept deliberately simple — only flat node/edge syntax. No subgraphs,
no styling macros, nothing that depends on Mermaid version quirks.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict

from harness.bus import Event


@dataclass
class _NodeStats:
    calls: int = 0
    errors: int = 0
    active: int = 0


@dataclass
class MermaidGraph:
    """Stateful graph builder. Updated from bus events; rendered on demand."""

    tools: Dict[str, _NodeStats] = field(default_factory=lambda: defaultdict(_NodeStats))
    llm_calls: int = 0
    agent_rounds: int = 0

    async def consume(self, evt: Event) -> None:
        """Update graph state from one bus event. TODO: hook every event kind."""
        kind = evt.kind
        if kind == "tool_call_start":
            name = evt.data.get("name", "?")
            self.tools[name].active += 1
        elif kind == "tool_call_end":
            name = evt.data.get("name", "?")
            stats = self.tools[name]
            stats.active = max(0, stats.active - 1)
            stats.calls += 1
            if evt.data.get("error"):
                stats.errors += 1
        elif kind == "llm_usage":
            self.llm_calls += 1
        elif kind == "agent_round_start":
            self.agent_rounds += 1

    def render(self) -> str:
        """Emit Mermaid `graph LR` source. Stable, plain syntax only."""
        lines = ["graph LR"]
        lines.append('  agent["Agent loop"]')
        lines.append('  llm["LLM (GigaChat)"]')
        lines.append(f'  agent -- "{self.llm_calls} calls" --> llm')

        if not self.tools:
            lines.append('  agent -. "no tool calls yet" .-> noop[" "]')
            return "\n".join(lines)

        for name, stats in sorted(self.tools.items()):
            node_id = _sanitize(name)
            label = f"{name}\\n{stats.calls} calls, {stats.errors} err"
            if stats.active:
                label += f"\\n[{stats.active} running]"
            lines.append(f'  {node_id}["{label}"]')
            lines.append(f'  agent --> {node_id}')
        return "\n".join(lines)


def _sanitize(name: str) -> str:
    """Mermaid node id -- letters/digits/underscore only."""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
