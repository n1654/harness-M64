"""Entry point: launch the three independent servers in one process.

Each server is a thin client of `harness.core.CoreAPI`. The core itself owns
the agent loop and runs concurrently with the servers.
"""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger("harness")


async def _amain() -> None:
    """Boot all three surfaces + the agent loop, gather forever."""
    from harness.core import CoreAPI
    from harness.control.shell import run_control_shell
    from harness.monitor.server import run_monitor_server
    from harness.ui.web import run_ui_server

    core = CoreAPI.from_env()
    await core.start()

    # Visibility: log every bus event so operator can see the flow in `docker compose logs`.
    bus_log = logging.getLogger("harness.bus")

    async def _log_event(evt) -> None:    # noqa: ANN001
        # Trim noisy fields (e.g. full raw HTTP responses) before logging.
        compact = {k: v for k, v in evt.data.items() if k not in ("raw", "prompt")}
        bus_log.info("%s %s", evt.kind, compact)

    await core.bus.subscribe(_log_event)

    ui_port = int(os.environ.get("HARNESS_UI_PORT", "8080"))
    mon_port = int(os.environ.get("HARNESS_MONITOR_PORT", "9090"))
    ctl_port = int(os.environ.get("HARNESS_CONTROL_PORT", "7000"))

    log.info("harness up: ui=:%d monitor=:%d control=:%d", ui_port, mon_port, ctl_port)

    await asyncio.gather(
        run_ui_server(core, port=ui_port),
        run_monitor_server(core, port=mon_port),
        run_control_shell(core, port=ctl_port),
        core.run_forever(),
    )


def run() -> None:
    """Console-script entry point (`harness` / `python -m harness`)."""
    logging.basicConfig(
        level=os.environ.get("HARNESS_LOG_LEVEL", "info").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_amain())


if __name__ == "__main__":
    run()
