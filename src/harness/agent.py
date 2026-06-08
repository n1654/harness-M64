"""Agent loop — owns the LLM client, tools, memory; executes prompts.

Tool loop: repeatedly call the LLM with the same `tools` schema; if the model
emits `tool_calls`, run them, append results, loop. Stop when the model emits
content without tool calls, or after `HARNESS_MAX_ROUNDS` (default 8) rounds.

Not exposed to clients directly; everything goes through `CoreAPI`.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from harness.llm.base import LLMClient, Message, Usage

if TYPE_CHECKING:
    from harness.bus import EventBus
    from harness.memory.base import MemoryStore
    from harness.tools.registry import ToolRegistry

log = logging.getLogger("harness.agent")


class Agent:
    """Single-tenant agent. One per Core."""

    def __init__(
        self,
        *,
        bus: "EventBus",
        memory_dir: pathlib.Path,
        tools_dir: pathlib.Path,
        state_dir: pathlib.Path,
    ) -> None:
        self._bus = bus
        self._memory_dir = memory_dir
        self._tools_dir = tools_dir
        self._state_dir = state_dir
        self._running = False
        self._llm: Optional[LLMClient] = None
        self._memory: Optional["MemoryStore"] = None
        self._tools: Optional["ToolRegistry"] = None

    # ---- lifecycle ----

    async def start(self) -> None:
        from harness.llm.gigachat import GigaChatClient
        from harness.memory.files import FileMemoryStore
        from harness.tools.registry import ToolRegistry

        self._llm = GigaChatClient.from_env()
        self._memory = FileMemoryStore(self._memory_dir)
        self._tools = ToolRegistry(self._tools_dir)
        self._running = True
        log.info("agent started; tools=%s", self._tools.names())

    async def stop(self, *, emergency: bool = False) -> None:
        self._running = False
        if self._llm is not None and hasattr(self._llm, "close"):
            try:
                await self._llm.close()    # type: ignore[attr-defined]
            except Exception:    # noqa: BLE001
                log.warning("llm close failed", exc_info=True)
        log.info("agent stopped (emergency=%s)", emergency)

    # ---- introspection used by Core ----

    @property
    def tools(self) -> Optional["ToolRegistry"]:
        return self._tools

    @property
    def running(self) -> bool:
        return self._running

    # ---- single-prompt cycle (multi-round) ----

    async def handle(
        self,
        session_id: str,
        prompt: str,
        *,
        history: Optional[List[Any]] = None,
    ) -> str:
        """Run prompt -> [tool calls...] -> final response. Returns assistant content.

        `history` is a list of `ChatMessage`-shaped objects (role + content) -- prior
        user/assistant turns that give the model conversational memory. Tool-call
        history of *previous* prompts is NOT included; only the final assistant
        replies, mirroring what the user sees in the UI thread.
        """
        if not self._running or self._llm is None or self._memory is None or self._tools is None:
            raise RuntimeError("agent is not started")

        max_rounds = self._read_max_rounds()
        round_started = time.time()
        await self._bus.publish(
            "agent_round_start",
            session_id=session_id, prompt_len=len(prompt),
            history_len=len(history or []),
        )

        messages = self._build_messages(prompt, history=history)
        tool_schemas = self._tools.schemas()
        model = getattr(self._llm, "_default_model", "?")

        agg_usage = Usage()
        final_content: str = ""
        last_error: Optional[Exception] = None

        try:
            for round_idx in range(1, max_rounds + 1):
                # ---- LLM call ----
                llm_started = time.time()
                await self._bus.publish(
                    "llm_call_start",
                    session_id=session_id, model=model, round=round_idx,
                )
                try:
                    result = await self._llm.chat(messages=messages, tools=tool_schemas)
                except Exception as e:    # noqa: BLE001
                    lat = round(time.time() - llm_started, 3)
                    await self._bus.publish(
                        "llm_call_end",
                        session_id=session_id, model=model, round=round_idx,
                        ok=False, latency_sec=lat, error=str(e),
                    )
                    last_error = e
                    raise

                lat = round(time.time() - llm_started, 3)
                await self._bus.publish(
                    "llm_call_end",
                    session_id=session_id, model=model, round=round_idx,
                    ok=True, latency_sec=lat,
                )
                await self._bus.publish(
                    "llm_usage",
                    session_id=session_id, model=model, round=round_idx,
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    total_tokens=result.usage.total_tokens,
                )
                _accumulate_usage(agg_usage, result.usage)

                msg = result.message

                # ---- no tools -> done ----
                if not msg.tool_calls:
                    final_content = (msg.content or "").strip()
                    break

                # ---- execute tool calls ----
                # Append the assistant turn first so the model sees its own intent.
                messages.append(msg)
                for tc in msg.tool_calls:
                    fn = tc.get("function") or {}
                    name = str(fn.get("name") or "")
                    raw_args = fn.get("arguments")
                    args = _parse_args(raw_args)
                    tool_result = await self._invoke_tool(session_id, round_idx, name, args, tc.get("id"))
                    # Propagate any provider-specific state (e.g. GigaChat's
                    # functions_state_id) from the assistant turn onto the tool
                    # result so the LLM adapter can echo it back.
                    messages.append(Message(
                        role="tool",
                        tool_call_id=str(tc.get("id") or ""),
                        name=name,
                        content=tool_result,
                        provider_meta=dict(msg.provider_meta) if msg.provider_meta else None,
                    ))
            else:
                # MAX_ROUNDS exhausted without natural completion.
                final_content = f"⚠️ stopped after {max_rounds} rounds without final response"

        finally:
            # Always record what happened, even on error.
            self._memory.append_episode({
                "ts": time.time(),
                "session_id": session_id,
                "prompt": prompt,
                "response": final_content,
                "usage": {
                    "prompt_tokens": agg_usage.prompt_tokens,
                    "completion_tokens": agg_usage.completion_tokens,
                    "total_tokens": agg_usage.total_tokens,
                },
                "latency_sec": round(time.time() - round_started, 3),
                "error": repr(last_error) if last_error else None,
            })

        await self._bus.publish(
            "agent_round_end",
            session_id=session_id, ok=True,
            latency_sec=round(time.time() - round_started, 3),
            total_tokens=agg_usage.total_tokens,
        )
        return final_content

    # ---- helpers ----

    async def _invoke_tool(
        self, session_id: str, round_idx: int, name: str,
        args: Dict[str, Any], call_id: Optional[str],
    ) -> str:
        """Execute one tool call, emit telemetry, return its textual result."""
        assert self._tools is not None
        await self._bus.publish(
            "tool_call_start",
            session_id=session_id, round=round_idx, name=name, call_id=call_id,
            args_keys=sorted(args.keys()),
        )
        started = time.time()
        try:
            result_text = await self._tools.execute(name, args)
            ok = True
            err: Optional[str] = None
        except Exception as e:    # noqa: BLE001
            result_text = f"⚠️ tool exception: {type(e).__name__}: {e}"
            ok = False
            err = str(e)

        await self._bus.publish(
            "tool_call_end",
            session_id=session_id, round=round_idx, name=name, call_id=call_id,
            ok=ok, error=err, latency_sec=round(time.time() - started, 3),
        )
        return str(result_text)

    def _build_messages(
        self, prompt: str, *, history: Optional[List[Any]] = None,
    ) -> List[Message]:
        """Assemble the LLM message list: system layers + prior chat + new prompt."""
        assert self._memory is not None
        levels = self._memory.read_levels()
        parts: List[str] = []
        for layer in levels:
            content = layer.content.strip()
            if not content:
                continue
            parts.append(f"# level {layer.level}\n\n{content}")
        system_text = "\n\n---\n\n".join(parts)

        msgs: List[Message] = []
        if system_text:
            msgs.append(Message(role="system", content=system_text))
        for h in (history or []):
            role = getattr(h, "role", None) or (h.get("role") if isinstance(h, dict) else None)
            content = getattr(h, "content", None) or (h.get("content") if isinstance(h, dict) else None)
            if role in ("user", "assistant") and content:
                msgs.append(Message(role=role, content=content))
        msgs.append(Message(role="user", content=prompt))
        return msgs

    @staticmethod
    def _read_max_rounds() -> int:
        try:
            return max(1, int(os.environ.get("HARNESS_MAX_ROUNDS", "8")))
        except ValueError:
            return 8


def _parse_args(raw: Any) -> Dict[str, Any]:
    """Tool-call arguments arrive as a JSON string from the LLM; sometimes as dict."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else {}
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _accumulate_usage(total: Usage, add: Usage) -> None:
    total.prompt_tokens += add.prompt_tokens
    total.completion_tokens += add.completion_tokens
    total.total_tokens += add.total_tokens
