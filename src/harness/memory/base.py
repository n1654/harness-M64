"""Memory store interface.

Layered model:
    level_0.md, level_1.md, ... -- ordered by number, lower index = higher priority.
                                   Operator-only (never written by the agent).
    scratchpad.md               -- working memory, agent-writable.
    knowledge/<topic>.md        -- long-term facts, agent-writable.
    episodes/<ts>.jsonl         -- chat / round history, append-only.

The order of insertion into the LLM prompt is the agent's responsibility, not
the store's; the store just hands files over.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, runtime_checkable


@dataclass
class MemoryLayer:
    """One level of the hierarchy."""

    level: int          # 0 = most important
    content: str
    path: str


@runtime_checkable
class MemoryStore(Protocol):
    """Provider-agnostic memory contract."""

    def read_levels(self) -> List[MemoryLayer]:
        """All `level_N.md` files, sorted by N ascending."""
        ...

    def read_scratchpad(self) -> str:
        ...

    def write_scratchpad(self, content: str) -> None:
        ...

    def read_knowledge(self, topic: str) -> Optional[str]:
        ...

    def write_knowledge(self, topic: str, content: str) -> None:
        ...

    def list_knowledge(self) -> List[str]:
        ...

    def append_episode(self, event: dict) -> None:
        """Append one JSON line to the current episodes file."""
        ...
