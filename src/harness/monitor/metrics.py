"""Prometheus metrics — translates bus events into counters/histograms.

One `MetricsSink` instance per harness; subscribe its `consume` to the bus.
Endpoint `/metrics` serves `generate_latest(sink.registry)`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

if TYPE_CHECKING:
    from harness.bus import Event

log = logging.getLogger("harness.monitor.metrics")


class MetricsSink:
    def __init__(self) -> None:
        self._init_collectors()

    def reset(self) -> None:
        """Drop the registry and recreate all collectors (counters back to 0).

        Prometheus counters are normally monotonic; the operator explicitly
        opts into a reset via the control shell.
        """
        self._init_collectors()

    def _init_collectors(self) -> None:
        self.registry = CollectorRegistry()

        self.prompts = Counter(
            "harness_prompts_total", "Prompts submitted",
            registry=self.registry,
        )
        self.agent_rounds = Counter(
            "harness_agent_rounds_total", "Agent loop rounds",
            ["outcome"], registry=self.registry,
        )
        self.agent_round_latency = Histogram(
            "harness_agent_round_seconds", "Agent round latency",
            registry=self.registry,
        )

        self.llm_calls = Counter(
            "harness_llm_calls_total", "LLM completions",
            ["model", "outcome"], registry=self.registry,
        )
        self.llm_latency = Histogram(
            "harness_llm_latency_seconds", "LLM completion latency",
            ["model"], registry=self.registry,
        )
        self.llm_prompt_tokens = Counter(
            "harness_llm_prompt_tokens_total", "Prompt tokens",
            ["model"], registry=self.registry,
        )
        self.llm_completion_tokens = Counter(
            "harness_llm_completion_tokens_total", "Completion tokens",
            ["model"], registry=self.registry,
        )

        self.tool_calls = Counter(
            "harness_tool_calls_total", "Tool invocations",
            ["tool", "outcome"], registry=self.registry,
        )
        self.tool_latency = Histogram(
            "harness_tool_latency_seconds", "Tool latency",
            ["tool"], registry=self.registry,
        )

        self.active_sessions = Gauge(
            "harness_sessions_active", "Sessions currently running",
            registry=self.registry,
        )
        self.errors = Counter(
            "harness_errors_total", "Errors observed on the bus",
            ["source"], registry=self.registry,
        )

    async def consume(self, evt: "Event") -> None:
        """Translate one bus event into metric updates. Defensive: never raises."""
        kind = evt.kind
        d = evt.data
        try:
            if kind == "factory_reset":
                if (d.get("scope") or "all") in ("metrics", "all"):
                    self.reset()
                return
            if kind == "prompt_submitted":
                self.prompts.inc()
                self.active_sessions.inc()

            elif kind == "agent_round_end":
                outcome = "ok" if d.get("ok") else "fail"
                self.agent_rounds.labels(outcome=outcome).inc()
                if (lat := d.get("latency_sec")) is not None:
                    self.agent_round_latency.observe(float(lat))
                self.active_sessions.dec()
                if not d.get("ok"):
                    self.errors.labels(source="agent").inc()

            elif kind == "llm_call_end":
                model = str(d.get("model") or "?")
                outcome = "ok" if d.get("ok") else "fail"
                self.llm_calls.labels(model=model, outcome=outcome).inc()
                if (lat := d.get("latency_sec")) is not None:
                    self.llm_latency.labels(model=model).observe(float(lat))
                if not d.get("ok"):
                    self.errors.labels(source="llm").inc()

            elif kind == "llm_usage":
                model = str(d.get("model") or "?")
                if (pt := d.get("prompt_tokens")):
                    self.llm_prompt_tokens.labels(model=model).inc(int(pt))
                if (ct := d.get("completion_tokens")):
                    self.llm_completion_tokens.labels(model=model).inc(int(ct))

            elif kind == "tool_call_end":
                tool = str(d.get("name") or "?")
                outcome = "error" if d.get("error") else "ok"
                self.tool_calls.labels(tool=tool, outcome=outcome).inc()
                if (lat := d.get("latency_sec")) is not None:
                    self.tool_latency.labels(tool=tool).observe(float(lat))
                if d.get("error"):
                    self.errors.labels(source="tool").inc()

        except Exception:    # noqa: BLE001
            log.warning("metrics consume failed for kind=%s", kind, exc_info=True)
