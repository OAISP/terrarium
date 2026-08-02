"""Prometheus exposition for the orchestrator — a read-only fold over the
session registry. No new dependency: the text format is trivial and the handful
of gauges we expose come straight from one cheap SQLite query (not a re-scan of
every JSONL log on each scrape).
"""

from __future__ import annotations

from .registry import LIVE_STATUSES, SessionRegistry

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def render(registry: SessionRegistry) -> str:
    rows = registry.list()
    active = sum(1 for r in rows if r.get("status") in LIVE_STATUSES)
    spend = sum(float(r.get("total_cost_usd") or 0) for r in rows)

    lines: list[str] = []

    def gauge(name: str, value: object, help: str) -> None:
        lines.append(f"# HELP {name} {help}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")

    gauge("terrarium_sessions_active", active, "Sessions currently live (starting/running/idle).")
    gauge("terrarium_sessions_total", len(rows), "All sessions known to the registry.")
    gauge("terrarium_spend_usd_total", f"{spend:.6f}", "Cumulative session cost in USD.")
    return "\n".join(lines) + "\n"
