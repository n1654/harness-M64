# Operator-supplied tools

Drop a `*.py` file here. It will be auto-discovered at agent startup
(after the bundled tools in `src/harness/tools/`). On name collision, the
external tool **wins**.

For the per-tool reference of what's already built in, see
[docs/tools.md](../docs/tools.md). For running any tool standalone (without
the LLM / UI / monitoring stack), see `python -m harness.tools_cli`.

---

## Contract

A tool module **must** export `get_tools() -> list[ToolEntry]`. Everything
else is up to you.

### `ToolHandler` signature

```python
async def handler(ctx: ToolContext, args: dict[str, Any]) -> str: ...
```

- **Always async.** The agent loop awaits handlers; sync work goes inside `asyncio.to_thread` if needed.
- **First arg is `ctx`** — `ToolContext` (see below). Ignore if you don't need it.
- **Second arg is `args`** — whatever the LLM put in `function_call.arguments`. Validate keys you care about; tolerate missing/extra ones.
- **Return value is a `str`** — handed back to the LLM as the function result.
  - For success: any human/JSON text. If you want the model to parse it, return JSON via `json.dumps(...)`.
  - For failure: a string starting with `⚠️` (the agent treats it as a tool error in telemetry; the model usually picks up the cue and apologises / retries).
- **Don't `raise`** except for truly exceptional conditions — return `⚠️ ...` instead. The agent catches exceptions and converts to `⚠️ tool exception: ...`, but a clean string is better.

### `ToolContext`

What handlers get for free:

| Field | Type | Use for |
|---|---|---|
| `ctx.memory` | `MemoryStore` | `read_levels`, `read_scratchpad / write_scratchpad`, `read_knowledge / write_knowledge / list_knowledge`, `append_episode` |
| `ctx.state_dir` | `pathlib.Path` | Anything filesystem-backed your tool needs. Conventionally put each tool's data under `<state_dir>/<tool-name>/` |
| `ctx.bus` | `EventBus` | Emit custom events (`await ctx.bus.publish("my_kind", key=value)`). `tool_call_start/end` are already emitted by the agent loop around your handler — don't duplicate |
| `ctx.extras` | `dict[str, Any]` | Free-form per-deployment extras |

### `ToolEntry`

```python
ToolEntry(
    name="my_tool",            # must match schema["name"]
    schema={
        "name": "my_tool",
        "description": "...",  # The single most important field for LLM selection
        "parameters": {...},   # JSON Schema (OpenAI-compatible)
    },
    handler=_my_tool,
    timeout_sec=60,            # default 60; agent does NOT enforce yet, but field is honored
)
```

The schema is the OpenAI-style `function` object (no `{"type":"function","function":{...}}` wrapper — the registry adds that). Use a clean JSON Schema for `parameters`. Mention every required field in `required: [...]`.

**GigaChat-specific extensions** you can add to a tool's `schema` dict (they pass through transparently):

- `few_shot_examples: [{"request": "...", "params": {...}}, ...]` — visibly improves tool-selection accuracy.
- `return_parameters: {...}` — JSON schema describing what your tool returns; helps the model interpret structured output.

---

## Template — `tools/example.py`

Drop a copy here and edit. This compiles as-is and registers a tool named `example`.

```python
"""Example external tool — replace this with something useful."""

from __future__ import annotations

from typing import Any, Dict, List

from harness.tools.registry import ToolContext, ToolEntry


async def _example(ctx: ToolContext, args: Dict[str, Any]) -> str:
    # 1. Validate args. Return "⚠️ ..." on bad input; the model will see it.
    name = str(args.get("name") or "").strip()
    if not name:
        return "⚠️ argument 'name' is required and must be non-empty"

    # 2. Optionally pull from ctx.memory or read/write under ctx.state_dir.
    #    (See bundled knowledge / file tools for patterns.)
    greeting_count = ctx.extras.get("example_count", 0) + 1
    ctx.extras["example_count"] = greeting_count

    # 3. Return a string the LLM will consume as the function result.
    return f"Hello, {name}! (greeting #{greeting_count})"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="example",
            schema={
                "name": "example",
                "description": (
                    "Greet someone by name. Demo tool -- replace with something useful. "
                    "Be explicit in the description: this is the ONE field that determines "
                    "whether the LLM will choose your tool."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The name to greet (non-empty).",
                        },
                    },
                    "required": ["name"],
                },
                # Optional GigaChat extension -- few-shot examples teach the model
                # WHEN to pick this tool.
                "few_shot_examples": [
                    {"request": "Поздоровайся с Никитой", "params": {"name": "Никита"}},
                ],
            },
            handler=_example,
            timeout_sec=10,
        ),
    ]
```

### Quickly test your new tool

After saving (and bind-mounting `tools/` in `docker-compose.yml`, which is the default):

```bash
docker compose exec -T harness python -m harness.tools_cli --list
# → your tool should appear

docker compose exec -T harness python -m harness.tools_cli example '{"name":"Dmitry"}'
# → "Hello, Dmitry! (greeting #1)"
```

Restart the harness so the agent re-discovers tools:

```bash
docker compose restart harness
```

Then in the UI ask the agent: «Поприветствуй Дмитрия» — it should call `example`.

---

## Anti-patterns / things to avoid

- **Don't return huge blobs.** If a payload could be large (a downloaded file, a query result), truncate yourself and add `"[truncated]"`. Models choke on 100K+ chars; the agent also charges tokens for it.
- **Don't access the filesystem outside `state_dir`** unless you really mean to. Stick to `<state_dir>/<your-tool>/` for tool data; reserve `state/sandbox/` for the built-in `file_*` tools.
- **Don't make blocking network/disk calls without `to_thread`.** They'll stall the whole agent loop (single-process async).
- **Don't shadow built-in tool names** unless deliberately replacing them. External tools win on collision — easy footgun.
- **Don't put secrets in `schema.description`.** The schema reaches the LLM verbatim.
