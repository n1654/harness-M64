"""Line-based TCP control shell.

Connect with `nc localhost 7000`. Commands:
    show {status|version|tools|sessions|config}
    restart
    stop
    emergency stop
    exit | quit
    ? | help

Hierarchical Cisco/Juniper-ish parsing kept minimal: split on whitespace,
walk a nested dict of handlers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Dict

from harness import __version__

if TYPE_CHECKING:
    from harness.core import CoreAPI

log = logging.getLogger("harness.control.shell")

_PROMPT = "harness> "
_BANNER = "Harness M64 control shell. Type '?' for help.\n"


CommandHandler = Callable[["_Session"], Awaitable[None]]


class _Session:
    """Per-connection state + writer helpers."""

    def __init__(self, core: "CoreAPI", reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.core = core
        self.reader = reader
        self.writer = writer
        self.argv: list[str] = []
        self.closing = False

    async def send(self, text: str) -> None:
        self.writer.write(text.encode("utf-8", "replace"))
        await self.writer.drain()

    async def line(self, text: str = "") -> None:
        await self.send(text + "\n")


# ---- handlers ----

async def _show_status(s: _Session) -> None:
    st = s.core.get_status()
    await s.line(
        f"uptime={int(st.uptime_sec)}s sessions_total={st.sessions_total} "
        f"sessions_active={st.sessions_active} tools_loaded={st.tools_loaded} "
        f"running={st.running}"
    )


async def _show_version(s: _Session) -> None:
    await s.line(f"harness {__version__}")


async def _show_tools(s: _Session) -> None:
    names = s.core.list_tools()
    if not names:
        await s.line("(no tools loaded yet)")
        return
    for n in names:
        await s.line(f"  {n}")


async def _show_sessions(s: _Session) -> None:
    rows = s.core.list_sessions()
    if not rows:
        await s.line("(no sessions)")
        return
    for r in rows:
        await s.line(f"  {r.id[:8]}  {r.status:10s}  {r.prompt[:60]}")


async def _show_queue(s: _Session) -> None:
    rows = s.core.list_pending_queue()
    if not rows:
        await s.line("(queue empty)")
        return
    for r in rows:
        await s.line(
            f"  {r['session_id'][:8]}  hist={r['history_len']:3d}  {r['prompt'][:60]}"
        )


async def _show_config(s: _Session) -> None:
    # Operator-safe view; redact secrets.
    import os

    keys = ["HARNESS_UI_PORT", "HARNESS_MONITOR_PORT", "HARNESS_CONTROL_PORT",
            "HARNESS_LOG_LEVEL", "GIGACHAT_MODEL", "GIGACHAT_SCOPE"]
    for k in keys:
        await s.line(f"  {k}={os.environ.get(k, '')}")


async def _restart(s: _Session) -> None:
    await s.core.stop()
    await s.core.start()
    await s.line("ok, restarted")


async def _stop(s: _Session) -> None:
    await s.core.stop()
    await s.line("ok, stopped")


async def _emergency_stop(s: _Session) -> None:
    await s.core.stop(emergency=True)
    await s.line("ok, emergency stop")


async def _reset_chat(s: _Session) -> None:
    info = await s.core.reset(scope="chat")
    await s.line(f"ok, cleared {info.get('chat_messages_dropped', 0)} chat messages")
    for p in info.get("paths") or []:
        await s.line(f"  removed: {p}")


async def _reset_metrics(s: _Session) -> None:
    await s.core.reset(scope="metrics")
    await s.line("ok, metrics counters + task history cleared")


async def _reset_all(s: _Session) -> None:
    info = await s.core.reset(scope="all")
    await s.line(
        f"ok, factory reset complete "
        f"(dropped {info.get('chat_messages_dropped', 0)} chat messages)"
    )
    for p in info.get("paths") or []:
        await s.line(f"  removed: {p}")
    await s.line("note: memory/level_*.md and tools/ are NOT touched.")


async def _help(s: _Session) -> None:
    await s.line("commands:")
    await s.line("  show {status,version,tools,sessions,queue,config}")
    await s.line("  restart")
    await s.line("  stop")
    await s.line("  emergency stop")
    await s.line("  reset {chat,metrics,all}")
    await s.line("  exit | quit")
    await s.line("  ? | help")


# Command tree: leaf is a coroutine; branch is a nested dict.
_TREE: Dict[str, object] = {
    "show": {
        "status": _show_status,
        "version": _show_version,
        "tools": _show_tools,
        "sessions": _show_sessions,
        "queue": _show_queue,
        "config": _show_config,
    },
    "restart": _restart,
    "stop": _stop,
    "emergency": {"stop": _emergency_stop},
    "reset": {
        "chat":    _reset_chat,
        "metrics": _reset_metrics,
        "all":     _reset_all,
    },
    "help": _help,
    "?": _help,
}


async def _dispatch(s: _Session, line: str) -> None:
    parts = line.strip().split()
    if not parts:
        return
    if parts[0] in ("exit", "quit"):
        s.closing = True
        return
    node: object = _TREE
    i = 0
    while i < len(parts):
        if not isinstance(node, dict):
            await s.line(f"unexpected argument: {parts[i]!r}")
            return
        token = parts[i]
        if token not in node:
            await s.line(f"unknown command: {' '.join(parts[: i + 1])!r} (try '?')")
            return
        node = node[token]
        i += 1
    if callable(node):
        s.argv = parts
        await node(s)
    else:
        # Partial match -- list children.
        keys = ", ".join(sorted(node.keys())) if isinstance(node, dict) else "<leaf>"
        await s.line(f"incomplete; expected one of: {keys}")


# ---- server ----

async def _handle_client(core: "CoreAPI", reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    log.info("control connect from %s", peer)
    s = _Session(core, reader, writer)
    await s.send(_BANNER)
    try:
        while not s.closing:
            await s.send(_PROMPT)
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            try:
                await _dispatch(s, line_bytes.decode("utf-8", "replace"))
            except Exception as e:    # noqa: BLE001
                log.exception("control handler crashed")
                await s.line(f"error: {e}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:    # noqa: BLE001
            pass
        log.info("control disconnect %s", peer)


async def run_control_shell(core: "CoreAPI", *, host: str = "0.0.0.0", port: int = 7000) -> None:
    """Block-serving TCP listener for the control shell."""
    server = await asyncio.start_server(
        lambda r, w: _handle_client(core, r, w), host=host, port=port,
    )
    async with server:
        await server.serve_forever()
