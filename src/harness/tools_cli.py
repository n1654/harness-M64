"""CLI runner for a single tool — used for manual testing / demo / debugging.

Usage:
    python -m harness.tools_cli <tool-name> '<json-args>'
    python -m harness.tools_cli --list
    python -m harness.tools_cli --schemas

Examples:
    python -m harness.tools_cli echo '{"text": "hi"}'
    python -m harness.tools_cli now '{"format": "human"}'
    python -m harness.tools_cli knowledge_write '{"topic": "demo", "content": "hello"}'
    python -m harness.tools_cli read_url '{"url": "https://example.com/"}'
    python -m harness.tools_cli file_write '{"path": "notes.md", "content": "# stuff"}'

The runner builds a `ToolContext` from the same env vars the harness uses
(`HARNESS_MEMORY_DIR`, `HARNESS_TOOLS_DIR`, `HARNESS_STATE_DIR`) so that
filesystem side-effects land where the agent would normally land them.
The agent loop, LLM, web UI, monitoring server -- none of those are started.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import sys


def _build_registry() -> "object":
    """Construct a ToolRegistry with a real ToolContext, no agent / LLM / servers."""
    from harness.bus import EventBus
    from harness.memory.files import FileMemoryStore
    from harness.tools.registry import ToolContext, ToolRegistry

    mem_dir   = pathlib.Path(os.environ.get("HARNESS_MEMORY_DIR", "memory"))
    tools_dir = pathlib.Path(os.environ.get("HARNESS_TOOLS_DIR", "tools"))
    state_dir = pathlib.Path(os.environ.get("HARNESS_STATE_DIR", "state"))

    mem_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    ctx = ToolContext(
        memory=FileMemoryStore(mem_dir),
        state_dir=state_dir,
        bus=EventBus(),
    )
    return ToolRegistry(tools_dir, ctx=ctx)


async def _run(name: str, args: dict) -> str:
    reg = _build_registry()
    return await reg.execute(name, args)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="harness.tools_cli",
        description="Run a single tool without starting the agent.",
    )
    parser.add_argument("tool", nargs="?", help="tool name")
    parser.add_argument("args", nargs="?", default="{}", help="JSON object of arguments")
    parser.add_argument("--list", action="store_true", help="list available tool names")
    parser.add_argument("--schemas", action="store_true", help="print all tool JSON schemas")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress runner logs")
    ns = parser.parse_args()

    if not ns.quiet:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if ns.list:
        reg = _build_registry()
        print("\n".join(reg.names()))    # type: ignore[attr-defined]
        return 0

    if ns.schemas:
        reg = _build_registry()
        print(json.dumps(reg.schemas(), ensure_ascii=False, indent=2))    # type: ignore[attr-defined]
        return 0

    if not ns.tool:
        parser.print_usage()
        return 2

    try:
        args = json.loads(ns.args) if ns.args.strip() else {}
        if not isinstance(args, dict):
            print(f"args must be a JSON object, got {type(args).__name__}", file=sys.stderr)
            return 2
    except json.JSONDecodeError as e:
        print(f"bad JSON args: {e}", file=sys.stderr)
        return 2

    try:
        result = asyncio.run(_run(ns.tool, args))
    except Exception as e:    # noqa: BLE001
        print(f"runner crashed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
