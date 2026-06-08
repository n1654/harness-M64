"""Filesystem-backed `MemoryStore`. Default implementation."""

from __future__ import annotations

import json
import logging
import pathlib
import re
import time
from typing import List, Optional

from harness.memory.base import MemoryLayer, MemoryStore

log = logging.getLogger("harness.memory.files")

_LEVEL_RE = re.compile(r"^level_(\d+)\.md$")


class FileMemoryStore(MemoryStore):
    """All operations resolve to files under `memory_dir`.

    Layout:
        <memory_dir>/level_0.md
        <memory_dir>/level_1.md
        <memory_dir>/scratchpad.md
        <memory_dir>/knowledge/*.md
        <memory_dir>/episodes/*.jsonl
    """

    def __init__(self, memory_dir: pathlib.Path) -> None:
        self._root = pathlib.Path(memory_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / "knowledge").mkdir(parents=True, exist_ok=True)
        (self._root / "episodes").mkdir(parents=True, exist_ok=True)

    def read_levels(self) -> List[MemoryLayer]:
        layers: List[MemoryLayer] = []
        for p in self._root.iterdir():
            m = _LEVEL_RE.match(p.name)
            if not m:
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except OSError:
                log.warning("failed to read %s", p, exc_info=True)
                continue
            layers.append(MemoryLayer(level=int(m.group(1)), content=content, path=str(p)))
        layers.sort(key=lambda l: l.level)
        return layers

    def read_scratchpad(self) -> str:
        p = self._root / "scratchpad.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def write_scratchpad(self, content: str) -> None:
        (self._root / "scratchpad.md").write_text(content, encoding="utf-8")

    def read_knowledge(self, topic: str) -> Optional[str]:
        p = self._root / "knowledge" / f"{topic}.md"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def write_knowledge(self, topic: str, content: str) -> None:
        (self._root / "knowledge" / f"{topic}.md").write_text(content, encoding="utf-8")

    def list_knowledge(self) -> List[str]:
        return sorted(p.stem for p in (self._root / "knowledge").glob("*.md"))

    def append_episode(self, event: dict) -> None:
        # One file per day; rotate on day boundary.
        day = time.strftime("%Y-%m-%d", time.gmtime())
        path = self._root / "episodes" / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
