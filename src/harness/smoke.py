"""Smoke test for the GigaChat adapter.

Usage:
    docker compose exec -T harness python -m harness.smoke
    docker compose exec -T harness python -m harness.smoke "your prompt here"

The `-T` flag disables TTY allocation -- mandatory when stdin is not a terminal
(scripted / CI / heredoc invocations).

Exits 0 on success, 1 on any error. Prints the assistant content + usage.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from harness.llm.base import Message
from harness.llm.gigachat import GigaChatClient


_DEFAULT_PROMPT = "Say 'pong' in one word."


async def _amain(prompt: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        async with GigaChatClient.from_env() as gc:
            result = await gc.chat([Message(role="user", content=prompt)])
    except Exception as e:    # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("--- content ---")
    print(result.message.content or "(no content)")
    print("--- usage ---")
    print(f"  prompt_tokens     = {result.usage.prompt_tokens}")
    print(f"  completion_tokens = {result.usage.completion_tokens}")
    print(f"  total_tokens      = {result.usage.total_tokens}")
    return 0


def run() -> None:
    prompt = " ".join(sys.argv[1:]).strip() or _DEFAULT_PROMPT
    sys.exit(asyncio.run(_amain(prompt)))


if __name__ == "__main__":
    run()
