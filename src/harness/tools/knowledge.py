"""Knowledge tools — agent-writable long-term facts.

Backed by `MemoryStore.read/write/list_knowledge`, which stores each topic as
a separate Markdown file under `memory/knowledge/<topic>.md`. Topics are
human-readable slugs; the agent picks them itself.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from harness.tools.registry import ToolContext, ToolEntry


_TOPIC_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _validate_topic(topic: str) -> str:
    topic = (topic or "").strip()
    if not _TOPIC_RE.match(topic):
        raise ValueError(
            f"invalid topic {topic!r}: must match [a-zA-Z0-9][a-zA-Z0-9_-]{{0,63}} "
            f"(no slashes, no spaces, no leading dash)"
        )
    return topic


async def _knowledge_read(ctx: ToolContext, args: Dict[str, Any]) -> str:
    try:
        topic = _validate_topic(str(args.get("topic") or ""))
    except ValueError as e:
        return f"⚠️ {e}"
    content = ctx.memory.read_knowledge(topic)
    if content is None:
        return f"(no knowledge entry for topic {topic!r})"
    return content


async def _knowledge_write(ctx: ToolContext, args: Dict[str, Any]) -> str:
    try:
        topic = _validate_topic(str(args.get("topic") or ""))
    except ValueError as e:
        return f"⚠️ {e}"
    content = str(args.get("content") or "")
    if not content.strip():
        return "⚠️ refusing to write empty knowledge entry"
    ctx.memory.write_knowledge(topic, content)
    return f"ok, wrote {len(content)} chars to knowledge/{topic}.md"


async def _knowledge_list(ctx: ToolContext, args: Dict[str, Any]) -> str:
    topics = ctx.memory.list_knowledge()
    if not topics:
        return "(no knowledge entries yet)"
    return "\n".join(topics)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="knowledge_read",
            schema={
                "name": "knowledge_read",
                "description": (
                    "Read a previously-saved knowledge entry by topic. Returns the entry's "
                    "Markdown content, or a 'not found' message if the topic doesn't exist."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "Topic slug (letters, digits, '-', '_'; up to 64 chars).",
                        },
                    },
                    "required": ["topic"],
                },
            },
            handler=_knowledge_read,
        ),
        ToolEntry(
            name="knowledge_write",
            schema={
                "name": "knowledge_write",
                "description": (
                    "Save or overwrite a knowledge entry under the given topic. Use this to "
                    "remember facts the user told you, lessons learned, or anything you'll "
                    "want to recall in future conversations."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "Topic slug (letters, digits, '-', '_'; up to 64 chars).",
                        },
                        "content": {
                            "type": "string",
                            "description": "Markdown content to store under this topic.",
                        },
                    },
                    "required": ["topic", "content"],
                },
            },
            handler=_knowledge_write,
        ),
        ToolEntry(
            name="knowledge_list",
            schema={
                "name": "knowledge_list",
                "description": (
                    "List all available knowledge topics (one per line). Call this when you "
                    "want to check whether you've remembered something on a subject."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_knowledge_list,
        ),
    ]
