"""Minimal web UI: HTMX + Server-Sent Events.

Endpoints:
    GET  /                  -- one-page chat (HTMX + SSE extension inline)
    POST /api/prompt        -- form submit; returns HTML fragments to append
    GET  /api/stream/{sid}  -- SSE stream of response chunks
    GET  /api/sessions      -- recent sessions (JSON)
    GET  /api/status        -- harness status (JSON)

Replaceable: implement another UI surface against `CoreAPI` and drop this module.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, StreamingResponse

if TYPE_CHECKING:
    from harness.core import CoreAPI

log = logging.getLogger("harness.ui.web")


_INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>Harness M64</title>
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<script src="https://unpkg.com/htmx-ext-sse@2.2.2/sse.js"></script>
<style>
 body{font:14px/1.4 system-ui, sans-serif; max-width:780px; margin:2rem auto; padding:0 1rem}
 .msg{margin:.5rem 0; padding:.5rem .75rem; border-radius:6px}
 .user{background:#eef}
 .agent{background:#efe; white-space:pre-wrap; min-height:1.4em}
 .agent em{color:#888}
 form{display:flex; gap:.5rem; margin-top:1rem}
 input[type=text]{flex:1; padding:.5rem}
</style>
</head><body>
<h1>Harness M64</h1>
<div id="thread"></div>
<form hx-post="/api/prompt"
      hx-target="#thread"
      hx-swap="beforeend"
      hx-on::after-request="this.querySelector('input').value=''">
  <input type="text" name="text" autofocus placeholder="ask the agent..." required>
  <button>send</button>
</form>
</body></html>
"""


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _fragment(user_text: str, sid: str) -> str:
    """HTML returned to HTMX after form submit: user bubble + agent bubble with SSE."""
    # sse-swap defaults to innerHTML -- the "..." placeholder is replaced by the
    # first chunk. Multi-chunk incremental streaming will need a different swap
    # mode (beforeend) and an explicit clear-on-first-chunk pattern.
    return (
        f'<div class="msg user">{_esc(user_text)}</div>'
        f'<div class="msg agent" hx-ext="sse" '
        f'     sse-connect="/api/stream/{sid}" '
        f'     sse-swap="chunk" '
        f'     sse-close="done">'
        f'<em>...</em>'
        f'</div>'
    )


def build_app(core: "CoreAPI") -> FastAPI:
    app = FastAPI(title="Harness M64 UI", version="0.0.1")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _INDEX_HTML

    @app.get("/api/status")
    async def status():
        s = core.get_status()
        return {
            "uptime_sec": s.uptime_sec,
            "running": s.running,
            "sessions_total": s.sessions_total,
            "sessions_active": s.sessions_active,
            "tools_loaded": s.tools_loaded,
        }

    @app.post("/api/prompt", response_class=HTMLResponse)
    async def submit_prompt(text: str = Form(...)):
        sid = await core.submit_prompt(text)
        return _fragment(text, sid)

    @app.get("/api/stream/{session_id}")
    async def stream(session_id: str):
        async def gen():
            async for chunk in core.stream_response(session_id):
                # SSE rules: a data line cannot contain a raw newline; multi-line
                # payloads must spread across consecutive "data:" lines.
                safe = (chunk or "").replace("\r", "").replace("\n", "\ndata: ")
                yield f"event: chunk\ndata: {safe}\n\n"
            # Close marker -- matches sse-close="done" on the client.
            yield "event: done\ndata: \n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/sessions")
    async def sessions():
        return [
            {"id": s.id, "prompt": s.prompt, "status": s.status, "created_at": s.created_at}
            for s in core.list_sessions()
        ]

    return app


async def run_ui_server(core: "CoreAPI", *, host: str = "0.0.0.0", port: int = 8080) -> None:
    import uvicorn

    app = build_app(core)
    cfg = uvicorn.Config(app=app, host=host, port=port, log_level="info", access_log=True)
    server = uvicorn.Server(cfg)
    await server.serve()
