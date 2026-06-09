"""File-plugin tool registry.

Discovery rules:
    * Every `*.py` file in `tools_dir/` is loaded.
    * The file must export `get_tools() -> list[ToolEntry]`.
    * Each `ToolEntry` carries an OpenAI-compatible JSON schema and a handler.

Both the operator's external `tools/` dir AND the bundled `harness.tools.*`
sub-modules contribute. External dir wins on name collision.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pathlib
import pkgutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from harness.bus import EventBus
    from harness.memory.base import MemoryStore

log = logging.getLogger("harness.tools.registry")


@dataclass
class ToolContext:
    """Runtime context passed to every tool handler.

    Tools that don't need it (e.g. pure-compute like `now`, `echo`) ignore it.
    Tools that touch memory, the bus, or sandboxed files take what they need.
    """

    memory: "MemoryStore"
    state_dir: pathlib.Path
    bus: "EventBus"
    extras: Dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[ToolContext, Dict[str, Any]], Awaitable[str]]


@dataclass
class ToolEntry:
    name: str
    schema: Dict[str, Any]    # OpenAI-style: {"name", "description", "parameters"}
    handler: ToolHandler
    timeout_sec: int = 60


class ToolRegistry:
    def __init__(
        self,
        tools_dir: pathlib.Path,
        *,
        ctx: Optional[ToolContext] = None,
    ) -> None:
        self._entries: Dict[str, ToolEntry] = {}
        self._tools_dir = pathlib.Path(tools_dir)
        self._ctx = ctx
        self._discover_bundled()
        self._discover_external()

    def set_context(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    # ---- discovery ----

    def _discover_bundled(self) -> None:
        """Tools shipped inside the harness package (e.g. harness.tools.echo)."""
        import harness.tools as pkg

        for _finder, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
            if modname in {"registry"} or modname.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"harness.tools.{modname}")
                self._register_from_module(mod)
            except Exception:    # noqa: BLE001
                log.warning("failed to load bundled tool '%s'", modname, exc_info=True)

    def _discover_external(self) -> None:
        """Operator-supplied tools in `tools_dir/*.py`. Names override bundled."""
        if not self._tools_dir.is_dir():
            return
        for p in sorted(self._tools_dir.glob("*.py")):
            spec = importlib.util.spec_from_file_location(f"_tools_ext.{p.stem}", p)
            if spec is None or spec.loader is None:
                continue
            try:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                self._register_from_module(mod)
            except Exception:    # noqa: BLE001
                log.warning("failed to load external tool '%s'", p, exc_info=True)

    def _register_from_module(self, mod: Any) -> None:
        get_tools = getattr(mod, "get_tools", None)
        if not callable(get_tools):
            return
        for entry in get_tools():
            self._entries[entry.name] = entry

    # ---- contract ----

    def names(self) -> List[str]:
        return sorted(self._entries.keys())

    def schemas(self) -> List[Dict[str, Any]]:
        """OpenAI-compatible `tools` payload."""
        return [{"type": "function", "function": e.schema} for e in self._entries.values()]

    def get_timeout(self, name: str) -> int:
        entry = self._entries.get(name)
        return entry.timeout_sec if entry else 60

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        entry = self._entries.get(name)
        if entry is None:
            return f"⚠️ unknown tool: {name}. available: {', '.join(self.names())}"
        if self._ctx is None:
            return f"⚠️ tool context not initialised; cannot execute {name}"
        return await entry.handler(self._ctx, args)
