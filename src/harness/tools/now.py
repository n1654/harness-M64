"""Tool: current UTC time. Useful both as a real utility and as a smoke target
for the tool loop (the LLM frequently picks this one for date/time questions)."""

from __future__ import annotations

import datetime
from typing import Any, Dict, List

from harness.tools.registry import ToolContext, ToolEntry


async def _now(ctx: ToolContext, args: Dict[str, Any]) -> str:
    fmt = str(args.get("format") or "iso").strip().lower()
    now = datetime.datetime.now(datetime.timezone.utc)
    if fmt == "epoch":
        return str(int(now.timestamp()))
    if fmt == "human":
        return now.strftime("%Y-%m-%d %H:%M:%S UTC")
    return now.isoformat()


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="now",
            schema={
                "name": "now",
                "description": (
                    "Return the current UTC time. Call this whenever the user asks "
                    "about the current date or time, or whenever you need a fresh "
                    "timestamp (the model itself has no clock)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "format": {
                            "type": "string",
                            "enum": ["iso", "epoch", "human"],
                            "description": (
                                "iso (default): ISO 8601, e.g. 2026-06-08T12:34:56+00:00; "
                                "epoch: Unix seconds; "
                                "human: '2026-06-08 12:34:56 UTC'."
                            ),
                        },
                    },
                    "required": [],
                },
                # GigaChat-specific extension -- improves tool selection accuracy.
                "few_shot_examples": [
                    {
                        "request": "Какое сейчас время?",
                        "params": {"format": "human"},
                    },
                    {
                        "request": "Дай таймстамп для лога",
                        "params": {"format": "epoch"},
                    },
                ],
            },
            handler=_now,
            timeout_sec=5,
        )
    ]
