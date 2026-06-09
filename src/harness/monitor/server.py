"""Monitoring HTTP server.

Endpoints:
    GET /metrics           -- Prometheus exporter (text format)
    GET /process-map       -- HTML dashboard (counters, FSM, task history)
    GET /api/state         -- JSON: counters + current FSM state
    GET /api/history       -- JSON: recent tasks with steps

Process-map page is fully static HTML; updates happen client-side via small
JSON polls that target specific DOM leaves -- so scroll position is preserved
across refresh cycles.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from harness.monitor.history import TaskHistorySink
from harness.monitor.metrics import MetricsSink

if TYPE_CHECKING:
    from harness.core import CoreAPI

log = logging.getLogger("harness.monitor.server")


_PAGE_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Harness process map</title>
<style>
  body{font:14px/1.4 system-ui, sans-serif; padding:1rem; max-width:1100px; margin:0 auto; color:#222}
  h2{margin:.2rem 0}
  .grid{display:grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap:.5rem; margin:1rem 0}
  .card{background:#f7f7f7; padding:.5rem .75rem; border-radius:6px}
  .card .k{font-size:.7rem; color:#666; text-transform:uppercase; letter-spacing:.04em}
  .card .v{font-size:1.3rem; font-weight:600; font-variant-numeric: tabular-nums}
  .card.err .v{color:#a00}

  /* FSM strip */
  #fsm{display:flex; align-items:center; gap:.4rem; flex-wrap:wrap; margin:1rem 0; padding:.6rem; background:#f7f7f7; border-radius:6px}
  #fsm .node{padding:.3rem .7rem; border:1px solid #ccc; border-radius:.3rem; background:#fff; min-width:70px; text-align:center; font-size:.85rem; transition: background .15s, border-color .15s}
  #fsm .node.active{background:#fa5; border-color:#a30; font-weight:600}
  #fsm .arrow{color:#999}
  #fsm-extra{font-size:.85rem; color:#666; margin-left:.5rem; font-style:italic}

  /* Task history */
  #history{display:flex; flex-direction:column; gap:.5rem; margin-top:1rem}
  .task{background:#fff; border:1px solid #e0e0e0; border-radius:6px; padding:.6rem .75rem}
  .task.running{border-left:4px solid #fa5}
  .task.done   {border-left:4px solid #4a4}
  .task.failed {border-left:4px solid #c44}
  .task .head{display:flex; gap:.5rem; align-items:baseline; font-size:.85rem; color:#555; margin-bottom:.3rem}
  .task .head .ts{font-variant-numeric: tabular-nums}
  .task .head .status{margin-left:auto; font-weight:600; padding:0 .4rem; border-radius:.3rem; background:#eee}
  .task .head .restored{background:#ffe; color:#960; padding:0 .3rem; border-radius:.2rem; font-size:.75rem}
  .task .prompt{font-weight:500; margin:.2rem 0}
  .task .chain{display:flex; align-items:center; flex-wrap:wrap; gap:.3rem; margin-top:.3rem}
  .task .pill{padding:.15rem .55rem; border-radius:1rem; font-size:.78rem; font-variant-numeric: tabular-nums}
  .pill-user  {background:#eef}
  .pill-llm   {background:#efe}
  .pill-tool  {background:#fed}
  .pill-tool.err {background:#fcc}
  .pill-reply {background:#dde}
  .pill.running{outline:2px solid #fa5; outline-offset:1px}
  .task .arrow{color:#bbb; font-size:.8rem}
  .task .steplist{font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.78rem; color:#444; white-space:pre-wrap; margin-top:.3rem}
  .task .err-msg{color:#a00; margin-top:.2rem; font-size:.85rem}

  footer{margin-top:2rem; font-size:.75rem; color:#999}
</style>
</head><body>
<h2>Harness process map</h2>

<div class="grid">
  <div class="card"><div class="k">uptime (s)</div>          <div class="v" id="c-uptime">0</div></div>
  <div class="card"><div class="k">prompts</div>             <div class="v" id="c-prompts">0</div></div>
  <div class="card"><div class="k">rounds ok / fail</div>    <div class="v"><span id="c-rok">0</span> / <span id="c-rfail">0</span></div></div>
  <div class="card"><div class="k">llm calls</div>           <div class="v" id="c-llm">0</div></div>
  <div class="card"><div class="k">tool calls</div>          <div class="v" id="c-tool">0</div></div>
  <div class="card"><div class="k">tokens in / out</div>     <div class="v"><span id="c-tin">0</span> / <span id="c-tout">0</span></div></div>
  <div class="card"><div class="k">last llm latency (s)</div><div class="v" id="c-lat">0.00</div></div>
  <div class="card err" id="card-err" style="display:none"><div class="k">errors</div><div class="v" id="c-err">0</div></div>
</div>

<div id="fsm">
  <div class="node" data-state="idle">idle</div>
  <span class="arrow">→</span>
  <div class="node" data-state="prompted">prompted</div>
  <span class="arrow">→</span>
  <div class="node" data-state="llm">llm</div>
  <span class="arrow">↔</span>
  <div class="node" data-state="tool">tool</div>
  <span class="arrow">→</span>
  <div class="node" data-state="reply">reply</div>
  <span id="fsm-extra"></span>
</div>

<h3>Task history</h3>
<div id="history"><em style="color:#888">(no tasks yet — submit a prompt in the UI)</em></div>

<footer>
  Counters reset only by <code>reset metrics</code> or <code>reset all</code> in the control shell.
  Page polls /api/state every 1s and /api/history every 2s — scroll is preserved.
</footer>

<script>
const SECONDARY_CHAIN_LIMIT = 10;

// --- state poll: counters + FSM ---
async function pollState() {
  try {
    const r = await fetch("/api/state");
    const s = await r.json();
    setText("c-uptime", Math.round(s.uptime_sec || 0));
    const c = s.counters || {};
    setText("c-prompts", c.prompts);
    setText("c-rok",     c.rounds_ok);
    setText("c-rfail",   c.rounds_fail);
    setText("c-llm",     c.llm_calls);
    setText("c-tool",    c.tool_calls);
    setText("c-tin",     c.tokens_prompt);
    setText("c-tout",    c.tokens_completion);
    setText("c-lat",     (c.last_llm_latency_sec || 0).toFixed(2));
    const errs = (c.llm_errors|0) + (c.tool_errors|0) + (c.rounds_fail|0);
    document.getElementById("card-err").style.display = errs ? "" : "none";
    setText("c-err", errs);

    // FSM highlight
    document.querySelectorAll("#fsm .node").forEach(n => n.classList.remove("active"));
    const cur = s.state || "idle";
    const el = document.querySelector(`#fsm .node[data-state="${cur}"]`);
    if (el) el.classList.add("active");
    const extra = [];
    if (s.current_tool)  extra.push(`tool: ${s.current_tool}`);
    if (s.current_model && cur === "llm") extra.push(`model: ${s.current_model}`);
    document.getElementById("fsm-extra").textContent = extra.length ? "(" + extra.join("; ") + ")" : "";
  } catch (e) { console.warn("state poll failed", e); }
}

function setText(id, val){
  const el = document.getElementById(id);
  if (el) el.textContent = (val ?? 0).toString();
}

// --- history poll: per-task list ---
async function pollHistory() {
  try {
    const r = await fetch("/api/history");
    const tasks = await r.json();
    const root = document.getElementById("history");
    if (!tasks || tasks.length === 0) {
      if (!root.querySelector("em")) {
        root.innerHTML = '<em style="color:#888">(no tasks yet — submit a prompt in the UI)</em>';
      }
      return;
    }
    if (root.querySelector("em")) root.innerHTML = "";

    // Render newest-first. Update existing nodes in place; prepend new ones.
    const seen = new Set();
    for (const t of tasks) {
      seen.add(t.session_id);
      let card = document.getElementById("t-" + t.session_id);
      if (!card) {
        card = document.createElement("div");
        card.id = "t-" + t.session_id;
        card.className = "task";
        root.prepend(card);
      }
      renderTask(card, t);
    }
    // Optional: remove tasks no longer in the snapshot (e.g. after reset).
    [...root.children].forEach(child => {
      if (child.id && child.id.startsWith("t-") && !seen.has(child.id.slice(2))) {
        child.remove();
      }
    });
  } catch (e) { console.warn("history poll failed", e); }
}

function renderTask(card, t) {
  card.className = "task " + (t.status || "running");
  const tsStr = new Date(t.started_at * 1000).toLocaleTimeString();
  const dur = t.duration_sec != null ? `${t.duration_sec.toFixed(2)}s` : `${((Date.now()/1000 - t.started_at)).toFixed(1)}s (running)`;
  const restored = t.restored ? '<span class="restored">restored</span>' : '';
  const head = `
    <div class="head">
      <span class="ts">${tsStr}</span>
      <span>${dur}</span>
      <span>${t.total_tokens|0} tokens</span>
      ${restored}
      <span class="status">${t.status}</span>
    </div>
    <div class="prompt">${escapeHtml(t.prompt || "")}</div>
  `;

  // Build the chain
  let stepsHtml;
  const steps = t.steps || [];
  const stepCount = steps.length;
  if (stepCount <= SECONDARY_CHAIN_LIMIT) {
    const pills = [pill("user", "user")];
    for (const s of steps) {
      let cls = "tool", label = s.name;
      if (s.kind === "llm_call") { cls = "llm"; label = "llm"; }
      else if (s.kind === "tool_call") {
        cls = "tool" + (s.error ? " err" : "");
        label = s.name + (s.duration_sec != null ? ` (${(s.duration_sec*1000).toFixed(0)}ms)` : "");
      }
      const running = (s.ok == null && t.status === "running") ? " running" : "";
      pills.push(`<span class="arrow">→</span>`, `<span class="pill pill-${cls}${running}">${escapeHtml(label)}</span>`);
    }
    if (t.status === "done") {
      pills.push(`<span class="arrow">→</span>`, `<span class="pill pill-reply">reply</span>`);
    }
    stepsHtml = `<div class="chain">${pills.join("")}</div>`;
  } else {
    const lines = steps.map((s, i) => {
      const dur = s.duration_sec != null ? ` (${(s.duration_sec*1000).toFixed(0)}ms)` : "";
      const ok = s.ok === false ? " [ERROR]" : "";
      return `${String(i+1).padStart(3)}. ${s.kind === "llm_call" ? "llm" : s.name}${dur}${ok}`;
    });
    stepsHtml = `<div class="steplist">${stepCount} steps:\n${escapeHtml(lines.join("\n"))}</div>`;
  }

  const errMsg = t.error ? `<div class="err-msg">⚠️ ${escapeHtml(t.error)}</div>` : "";
  card.innerHTML = head + stepsHtml + errMsg;
}

function pill(cls, label) {
  return `<span class="pill pill-${cls}">${escapeHtml(label)}</span>`;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

pollState(); pollHistory();
setInterval(pollState,   1000);
setInterval(pollHistory, 2000);
</script>
</body></html>
"""


def build_app(core: "CoreAPI") -> FastAPI:
    app = FastAPI(title="Harness M64 Monitor", version="0.0.1")
    metrics = MetricsSink()
    history = TaskHistorySink()

    async def _on_event(evt):    # noqa: ANN001
        await metrics.consume(evt)
        await history.consume(evt)

    @app.on_event("startup")
    async def _wire() -> None:
        await core.bus.subscribe(_on_event)
        log.info("monitor wired to bus; tools known at startup: %s", core.list_tools())

    @app.get("/metrics", response_class=PlainTextResponse)
    async def prometheus_metrics():
        body = generate_latest(metrics.registry).decode("utf-8")
        return PlainTextResponse(body, media_type=CONTENT_TYPE_LATEST)

    @app.get("/process-map", response_class=HTMLResponse)
    async def process_map_page():
        return _PAGE_HTML

    @app.get("/api/state")
    async def api_state():
        snap = history.state_snapshot()
        snap["uptime_sec"] = core.get_status().uptime_sec
        return snap

    @app.get("/api/history")
    async def api_history():
        return history.history_snapshot()

    return app


async def run_monitor_server(core: "CoreAPI", *, host: str = "0.0.0.0", port: int = 9090) -> None:
    import uvicorn

    app = build_app(core)
    cfg = uvicorn.Config(app=app, host=host, port=port, log_level="info", access_log=True)
    server = uvicorn.Server(cfg)
    await server.serve()
