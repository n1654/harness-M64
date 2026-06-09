"""Example bundled tool. Sanity check for the registry + an executable smoke target."""

from __future__ import annotations

from typing import Any, Dict, List

from harness.tools.registry import ToolContext, ToolEntry


async def _echo(ctx: ToolContext, args: Dict[str, Any]) -> str:
    return str(args.get("text", ""))


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="echo",
            schema={
                "name": "echo",
                "description": "Return the input text verbatim. Useful for smoke-testing the tool loop.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            handler=_echo,
            timeout_sec=5,
        )
    ]
