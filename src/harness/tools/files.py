"""File tools — read/write/list inside a sandboxed directory.

Sandbox root: `<state_dir>/sandbox/` (created on first use). All paths are
relative to the sandbox; absolute paths or `..` traversals are refused.
This is the only filesystem area the agent can write to via tool calls
(memory writes go through dedicated memory tools).
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Dict, List, Tuple

from harness.tools.registry import ToolContext, ToolEntry


_MAX_READ_BYTES  = 200_000     # cap on a single read
_MAX_WRITE_BYTES = 200_000     # cap on a single write
_MAX_LIST_ITEMS  = 500


def _sandbox_root(ctx: ToolContext) -> pathlib.Path:
    root = ctx.state_dir / "sandbox"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _resolve_safe(ctx: ToolContext, rel: str) -> Tuple[pathlib.Path, pathlib.Path]:
    """Return (sandbox_root, resolved_path). Raises ValueError on escape attempt."""
    if not rel or rel.strip() == "":
        raise ValueError("path is required")
    if pathlib.PurePath(rel).is_absolute():
        raise ValueError("absolute paths are not allowed; use a path relative to the sandbox")
    root = _sandbox_root(ctx)
    candidate = (root / rel).resolve()
    # Allow root itself + anything strictly under it.
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes the sandbox")
    return root, candidate


async def _file_read(ctx: ToolContext, args: Dict[str, Any]) -> str:
    try:
        _root, target = _resolve_safe(ctx, str(args.get("path") or ""))
    except ValueError as e:
        return f"⚠️ {e}"
    if not target.is_file():
        return f"⚠️ not a file: {target.relative_to(_root) if _root in target.parents else target}"
    try:
        size = target.stat().st_size
        if size > _MAX_READ_BYTES:
            return f"⚠️ file too large: {size} bytes (cap {_MAX_READ_BYTES})"
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"⚠️ read failed: {e}"


async def _file_write(ctx: ToolContext, args: Dict[str, Any]) -> str:
    try:
        root, target = _resolve_safe(ctx, str(args.get("path") or ""))
    except ValueError as e:
        return f"⚠️ {e}"
    content = str(args.get("content") or "")
    if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
        return f"⚠️ content too large: {len(content.encode('utf-8'))} bytes (cap {_MAX_WRITE_BYTES})"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"ok, wrote {len(content)} chars to {target.relative_to(root)}"
    except OSError as e:
        return f"⚠️ write failed: {e}"


async def _file_list(ctx: ToolContext, args: Dict[str, Any]) -> str:
    root = _sandbox_root(ctx)
    rel_prefix = str(args.get("prefix") or "").strip().lstrip("/")
    try:
        if rel_prefix:
            _, base = _resolve_safe(ctx, rel_prefix)
            if not base.exists():
                return f"(no such directory: {rel_prefix})"
            if base.is_file():
                return str(base.relative_to(root))
            base_dir = base
        else:
            base_dir = root
    except ValueError as e:
        return f"⚠️ {e}"

    if not base_dir.is_dir():
        return "(empty)"

    entries: List[str] = []
    for p in sorted(base_dir.rglob("*")):
        if p.is_file():
            entries.append(str(p.relative_to(root)))
            if len(entries) >= _MAX_LIST_ITEMS:
                entries.append(f"... [+more, truncated at {_MAX_LIST_ITEMS}]")
                break
    if not entries:
        return "(empty)"
    return "\n".join(entries)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="file_read",
            schema={
                "name": "file_read",
                "description": (
                    "Read a UTF-8 text file from the agent's sandbox (state/sandbox/). "
                    "Paths are relative; absolute paths or '..' are refused. "
                    f"Max {_MAX_READ_BYTES} bytes per read."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "path relative to the sandbox root"},
                    },
                    "required": ["path"],
                },
            },
            handler=_file_read,
        ),
        ToolEntry(
            name="file_write",
            schema={
                "name": "file_write",
                "description": (
                    "Write (or overwrite) a UTF-8 text file in the agent's sandbox "
                    "(state/sandbox/). Creates parent directories as needed. "
                    f"Max {_MAX_WRITE_BYTES} bytes per write."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path":    {"type": "string", "description": "path relative to the sandbox root"},
                        "content": {"type": "string", "description": "file content (UTF-8 text)"},
                    },
                    "required": ["path", "content"],
                },
            },
            handler=_file_write,
        ),
        ToolEntry(
            name="file_list",
            schema={
                "name": "file_list",
                "description": (
                    "List files in the agent's sandbox (state/sandbox/), recursively. "
                    "Pass an optional `prefix` to scope to a subdirectory."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prefix": {
                            "type": "string",
                            "description": "subdirectory to list (default: sandbox root)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_file_list,
        ),
    ]
