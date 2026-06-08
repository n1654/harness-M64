"""Minimal web UI: HTMX + Server-Sent Events.

Endpoints:
    GET  /                  -- one-page chat (HTML + a tiny inline HTMX setup)
    POST /api/prompt        -- submit; returns {session_id}
    GET  /api/stream/{sid}  -- SSE stream of response chunks
    GET  /api/sessions      -- recent sessions (JSON)
    GET  /api/status        -- harness status (JSON)

Replaceable: implement another UI surface against `CoreAPI` and drop this module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from harness.core import CoreAPI

log = logging.getLogger("harness.ui.web")


_INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>Harness M64</title>
<script src="https://unpkg.com/htmx.org@2.0.4" defer></script>
<style>
 body{font:14px/1.4 system-ui, sans-serif; max-width:780px; margin:2rem auto; padding:0 1rem}
 .msg{margin:.5rem 0; padding:.5rem .75rem; border-radius:6px}
 .user{background:#eef}
 .agent{background:#efe; white-space:pre-wrap}
 form{display:flex; gap:.5rem; margin-top:1rem}
 input[type=text]{flex:1; padding:.5rem}
</style>
</head><body>
<h1>Harness M64</h1>
<div id="thread"></div>
<form hx-post="/api/prompt" hx-target="#thread" hx-swap="beforeend">
  <input type="text" name="text" autofocus placeholder="ask the agent...">
  <button>send</button>
</form>
</body></html>
"""


class PromptIn(BaseModel):
    text: str


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

    @app.post("/api/prompt")
    async def submit_prompt(prompt: PromptIn):
        sid = await core.submit_prompt(prompt.text)
        return {"session_id": sid}

    @app.get("/api/stream/{session_id}")
    async def stream(session_id: str):
        async def gen():
            async for chunk in core.stream_response(session_id):
                yield f"data: {chunk}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/sessions")
    async def sessions():
        return [
            {"id": s.id, "prompt": s.prompt, "status": s.status, "created_at": s.created_at}
            for s in core.list_sessions()
        ]

    return app


async def run_ui_server(core: "CoreAPI", *, host: str = "0.0.0.0", port: int = 8080) -> None:
    """Launch the UI server. Returns when the server stops."""
    import uvicorn

    app = build_app(core)
    cfg = uvicorn.Config(app=app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(cfg)
    await server.serve()
