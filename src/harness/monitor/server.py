"""Monitoring HTTP server.

Endpoints:
    GET /metrics      -- Prometheus text format (text/plain)
    GET /process-map  -- HTML page rendering a Mermaid graph that updates via polling
    GET /process-map/mermaid  -- raw Mermaid source (text/plain)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest

from harness.monitor.mermaid import MermaidGraph

if TYPE_CHECKING:
    from harness.core import CoreAPI

log = logging.getLogger("harness.monitor.server")


_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Harness process map</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>body{font:14px/1.4 system-ui, sans-serif; padding:1rem}</style>
</head><body>
<h2>Process map</h2>
<div class="mermaid" id="graph">graph LR; loading[/" loading "/];</div>
<script>
  mermaid.initialize({startOnLoad: true, theme: "default"});
  async function tick(){
    const r = await fetch("/process-map/mermaid");
    const text = await r.text();
    const el = document.getElementById("graph");
    el.removeAttribute("data-processed");
    el.textContent = text;
    await mermaid.run({nodes: [el]});
  }
  tick(); setInterval(tick, 2000);
</script>
</body></html>
"""


class _Metrics:
    """Prometheus collectors. Wire them from bus events in `attach()`."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.tool_calls = Counter(
            "harness_tool_calls_total", "Tool invocations", ["tool", "outcome"], registry=self.registry,
        )
        self.tool_latency = Histogram(
            "harness_tool_latency_seconds", "Tool latency", ["tool"], registry=self.registry,
        )
        self.llm_calls = Counter(
            "harness_llm_calls_total", "LLM completions", ["model", "outcome"], registry=self.registry,
        )
        self.llm_prompt_tokens = Counter(
            "harness_llm_prompt_tokens_total", "Prompt tokens", ["model"], registry=self.registry,
        )
        self.llm_completion_tokens = Counter(
            "harness_llm_completion_tokens_total", "Completion tokens", ["model"], registry=self.registry,
        )
        self.agent_rounds = Counter(
            "harness_agent_rounds_total", "Agent loop rounds", registry=self.registry,
        )


def build_app(core: "CoreAPI") -> FastAPI:
    app = FastAPI(title="Harness M64 Monitor", version="0.0.1")
    metrics = _Metrics()
    graph = MermaidGraph()

    # Subscribe to the event bus exactly once at module-app build time.
    async def _on_event(evt):    # noqa: ANN001
        # TODO: dispatch evt.kind into metrics + graph updates
        await graph.consume(evt)

    @app.on_event("startup")
    async def _wire():
        await core.bus.subscribe(_on_event)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def prometheus_metrics():
        body = generate_latest(metrics.registry).decode("utf-8")
        return PlainTextResponse(body, media_type=CONTENT_TYPE_LATEST)

    @app.get("/process-map", response_class=HTMLResponse)
    async def process_map_page():
        return _PAGE_HTML

    @app.get("/process-map/mermaid", response_class=PlainTextResponse)
    async def process_map_mermaid():
        return graph.render()

    return app


async def run_monitor_server(core: "CoreAPI", *, host: str = "0.0.0.0", port: int = 9090) -> None:
    import uvicorn

    app = build_app(core)
    cfg = uvicorn.Config(app=app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(cfg)
    await server.serve()
