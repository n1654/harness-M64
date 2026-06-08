"""Monitoring HTTP server.

Endpoints:
    GET /metrics              -- Prometheus text format
    GET /process-map          -- HTML dashboard with Mermaid graph + status panel
    GET /process-map/mermaid  -- raw Mermaid source (text/plain)
    GET /process-map/status   -- JSON snapshot for the status panel
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from harness.monitor.mermaid import MermaidGraph
from harness.monitor.metrics import MetricsSink

if TYPE_CHECKING:
    from harness.core import CoreAPI

log = logging.getLogger("harness.monitor.server")


_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Harness process map</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
 body{font:14px/1.4 system-ui, sans-serif; padding:1rem; max-width:1100px; margin:0 auto}
 h2{margin:.2rem 0}
 .grid{display:grid; grid-template-columns: repeat(5, 1fr); gap:.5rem; margin:1rem 0}
 .card{background:#f7f7f7; padding:.5rem .75rem; border-radius:6px}
 .card .k{font-size:.75rem; color:#666; text-transform:uppercase; letter-spacing:.04em}
 .card .v{font-size:1.4rem; font-weight:600}
 .mermaid{background:#fff; padding:1rem; border-radius:6px; min-height:140px}
</style>
</head><body>
<h2>Harness process map</h2>
<div class="grid">
  <div class="card"><div class="k">uptime (s)</div><div class="v" id="uptime">0</div></div>
  <div class="card"><div class="k">prompts</div><div class="v" id="prompts">0</div></div>
  <div class="card"><div class="k">sessions active</div><div class="v" id="active">0</div></div>
  <div class="card"><div class="k">llm calls</div><div class="v" id="llm_calls">0</div></div>
  <div class="card"><div class="k">last llm latency (s)</div><div class="v" id="llm_lat">0.00</div></div>
</div>
<div class="mermaid" id="graph">graph LR; loading[/" loading "/];</div>
<script>
  mermaid.initialize({startOnLoad: true, theme: "default"});
  async function tick(){
    const [m, s] = await Promise.all([
      fetch("/process-map/mermaid").then(r=>r.text()),
      fetch("/process-map/status").then(r=>r.json()),
    ]);
    document.getElementById("uptime").textContent    = Math.round(s.uptime_sec);
    document.getElementById("prompts").textContent   = s.prompts;
    document.getElementById("active").textContent    = s.sessions_active;
    document.getElementById("llm_calls").textContent = s.llm_calls;
    document.getElementById("llm_lat").textContent   = s.llm_last_latency_sec.toFixed(2);
    const el = document.getElementById("graph");
    el.removeAttribute("data-processed");
    el.textContent = m;
    await mermaid.run({nodes: [el]});
  }
  tick(); setInterval(tick, 1500);
</script>
</body></html>
"""


def build_app(core: "CoreAPI") -> FastAPI:
    app = FastAPI(title="Harness M64 Monitor", version="0.0.1")
    metrics = MetricsSink()
    graph = MermaidGraph()

    # Pre-populate the graph with known tools so they appear before any call.
    tool_names = core.list_tools()
    if tool_names:
        graph.register_tool_names(tool_names)

    async def _on_event(evt):    # noqa: ANN001
        await metrics.consume(evt)
        await graph.consume(evt)

    @app.on_event("startup")
    async def _wire() -> None:
        await core.bus.subscribe(_on_event)
        log.info("monitor wired to bus; tools known at startup: %s", tool_names)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def prometheus_metrics():
        body = generate_latest(metrics.registry).decode("utf-8")
        return PlainTextResponse(body, media_type=CONTENT_TYPE_LATEST)

    @app.get("/process-map", response_class=HTMLResponse)
    async def process_map_page():
        return _PAGE_HTML

    @app.get("/process-map/mermaid", response_class=PlainTextResponse)
    async def process_map_mermaid():
        # Refresh known tools each tick — picks up dynamically added external tools.
        graph.register_tool_names(core.list_tools())
        return graph.render()

    @app.get("/process-map/status")
    async def process_map_status():
        s = core.get_status()
        return {
            "uptime_sec": s.uptime_sec,
            "running": s.running,
            "sessions_total": s.sessions_total,
            "sessions_active": s.sessions_active,
            "tools_loaded": s.tools_loaded,
            "prompts": graph.agent.prompts,
            "agent_rounds_ok": graph.agent.rounds_ok,
            "agent_rounds_fail": graph.agent.rounds_fail,
            "llm_calls": graph.llm.calls,
            "llm_errors": graph.llm.errors,
            "llm_last_latency_sec": graph.llm.last_latency_sec,
            "llm_prompt_tokens": graph.llm.prompt_tokens,
            "llm_completion_tokens": graph.llm.completion_tokens,
        }

    return app


async def run_monitor_server(core: "CoreAPI", *, host: str = "0.0.0.0", port: int = 9090) -> None:
    import uvicorn

    app = build_app(core)
    cfg = uvicorn.Config(app=app, host=host, port=port, log_level="info", access_log=True)
    server = uvicorn.Server(cfg)
    await server.serve()
