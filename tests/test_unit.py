#!/usr/bin/env python3
"""Fast, dependency-free unit tests (no docker, no API calls).

Run:  uv run python tests/test_unit.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

# Must be set before ANY test imports the worker: `conceal_env()` runs at worker import
# and re-execs the process to scrub /proc/environ, which would restart this suite. It
# belongs here, not inside whichever test happens to import the worker first — that made
# every later worker test depend on the hand-written run order.
os.environ["TERRA_NO_RESEAL"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run
from terracore import protocol as P
from terracore.events import EventStore
from terracore.harness import Harness
from terracore.personas import ASSISTANT_PROMPT, CUSTOM_PROMPT, MODES, build_system_prompt
from orchestrator import filebridge
from orchestrator.agents import AgentSpec, AgentStore
from orchestrator.config import Config
from orchestrator.runners import _CA_PEM as _CA_PEM_SENTINEL
from orchestrator.runners import DockerRunner, SessionConfig, hardened_flags


def test_events():
    p = Path(tempfile.mkdtemp()) / "s.jsonl"
    st = EventStore(p)
    st.append("session_start", model="x")
    st.record({"type": "assistant_text", "text": "hi"})
    st.record({"type": "result", "usage": {"input_tokens": 5}})
    assert [e["seq"] for e in st.read()] == [0, 1, 2]
    assert [e["seq"] for e in st.read(after=0)] == [1, 2]
    st2 = EventStore(p)
    st2.append("session_end")
    assert st2.read()[-1]["seq"] == 3
    # F11: a worker-supplied ts is discarded — ts is host-authoritative like seq, so a
    # compromised agent can't forge the timestamp the Logs view sorts/filters by.
    forged = st2.record({"type": "tool_use", "ts": "1999-01-01T00:00:00Z", "name": "Bash"})
    assert forged["ts"] != "1999-01-01T00:00:00Z" and forged["ts"].endswith("Z")
    from terracore.events import tail_events
    tail, capped = tail_events(p, 2)
    assert [e["seq"] for e in tail] == [3, 4] and capped is True
    with p.open("a") as fh:
        fh.write('{}\n{"seq":"bad"}\ntruncated-json\n')
    st3 = EventStore(p)
    assert st3.append("session_end")["seq"] == 5  # corrupt suffix cannot reset seq
    print("ok  events: append/record/replay/seq-persistence + ts is host-stamped (forge-proof)")


def test_untrusted_pipe_lines_are_bounded():
    import asyncio

    from orchestrator.runners import _bounded_lines

    async def collect():
        # readline()/async iteration would raise because this StreamReader has a
        # tiny separator limit. The fixed-chunk reader must recover at the next
        # newline and also preserve an unterminated final record.
        reader = asyncio.StreamReader(limit=16)
        reader.feed_data(b"x" * 100 + b"\nok\ntrailing")
        reader.feed_eof()
        return [item async for item in _bounded_lines(reader, 32)]

    assert asyncio.run(collect()) == [(None, 100), (b"ok", 0), (b"trailing", 0)]
    print("ok  runner pipes: oversized untrusted lines are bounded without wedging replay")


def test_session_summary():
    from orchestrator.manager import summarize_events

    # total_cost_usd is the CLI's CUMULATIVE session cost (grows monotonically,
    # one result per turn); usage is per-turn. Mirrors a real 2-turn session.
    events = [
        {"type": "user"},
        {"type": "tool_use"},
        {"type": "result", "total_cost_usd": 0.10,
         "usage": {"input_tokens": 3, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 1000}},
        {"type": "user"},
        {"type": "result", "total_cost_usd": 0.25,
         "usage": {"input_tokens": 3, "output_tokens": 80, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 50}},
    ]
    s = summarize_events(events)
    assert s["total_cost_usd"] == 0.25            # latest cumulative — NOT 0.35 (the buggy sum)
    assert s["user_turns"] == 2 and s["tool_calls"] == 1
    assert s["tokens"]["output"] == 130           # usage IS per-turn → summed
    assert s["tokens"]["cacheRead"] == 1000
    assert s["tokens"]["total"] == (3 + 50 + 0 + 1000) + (3 + 80 + 1000 + 50)
    # a trailing result with no cost field must not zero the running total
    s2 = summarize_events(events + [{"type": "result", "usage": {"output_tokens": 5}}])
    assert s2["total_cost_usd"] == 0.25
    assert s["context"] is None, "no context_usage events → context is None (back-compat)"
    print("ok  summary: cumulative cost (latest, not summed) + per-turn token totals")


def test_context_usage_in_summary():
    """Phase 1: the latest context_usage event surfaces in the summary so a supervisor can
    poll context-window % and decide when to checkpoint/compact (the CLI auto-compacts)."""
    from orchestrator.manager import summarize_events
    events = [
        {"type": "context_usage", "percentage": 40.0, "total_tokens": 80000, "max_tokens": 200000,
         "auto_compact": True, "compact_threshold": 184000},
        {"type": "result", "total_cost_usd": 0.1, "usage": {"input_tokens": 100}},
        {"type": "context_usage", "percentage": 55.5, "total_tokens": 111000, "max_tokens": 200000,
         "auto_compact": True, "compact_threshold": 184000},
    ]
    ctx = summarize_events(events)["context"]
    assert ctx["percentage"] == 55.5 and ctx["max_tokens"] == 200000, "latest context_usage wins"
    assert ctx["auto_compact"] is True
    print("ok  context_usage: latest surfaces in the session summary (Phase 1)")


def test_session_registry():
    from orchestrator.registry import LIVE_STATUSES, SessionRegistry

    d = Path(tempfile.mkdtemp())
    reg = SessionRegistry(d / "sessions.db")
    reg.upsert(session_id="s1", agent_id="a", status="running", runner="k8s",
               pod_name="terrarium-session-s1", total_cost_usd=0.0, harness_json='{"model":"x"}')
    assert reg.get("s1")["status"] == "running"
    reg.update("s1", total_cost_usd=0.5, last_seq=10, status=None)  # None must be ignored
    row = reg.get("s1")
    assert row["total_cost_usd"] == 0.5 and row["last_seq"] == 10 and row["status"] == "running"
    reg.upsert(session_id="s2", status="terminated", runner="k8s")
    assert {r["session_id"] for r in reg.list(LIVE_STATUSES)} == {"s1"}   # only live ones
    assert {r["session_id"] for r in reg.list()} == {"s1", "s2"}
    reg.remove("s2")
    assert reg.get("s2") is None
    reg.close()
    print("ok  registry: upsert · update(None-skip) · list(status filter) · remove")


def test_agent_spend_ledger():
    """#2.3: cumulative spend per agent across its sessions, windowed by created_ts."""
    from orchestrator.registry import SessionRegistry
    reg = SessionRegistry(Path(tempfile.mkdtemp()) / "sessions.db")
    reg.upsert(session_id="s1", agent_id="a", created_ts="2026-06-01T00:00:00Z", total_cost_usd=1.50)
    reg.upsert(session_id="s2", agent_id="a", created_ts="2026-06-27T00:00:00Z", total_cost_usd=0.10)
    reg.update("s2", total_cost_usd=0.25)   # cost grows per-turn → the ledger tracks it
    reg.upsert(session_id="s3", agent_id="b", created_ts="2026-06-27T00:00:00Z", total_cost_usd=9.99)
    assert reg.spend("a") == {"sessions": 2, "total_cost_usd": 1.75}        # only agent a, summed
    recent = reg.spend("a", since_iso="2026-06-15T00:00:00Z")
    assert recent == {"sessions": 1, "total_cost_usd": 0.25}, recent        # windowed by created_ts
    reg.remove("s1")   # DELETE the session — its cost must REMAIN in the cumulative ledger
    assert reg.spend("a") == {"sessions": 2, "total_cost_usd": 1.75}, "deleted session's spend persists"
    assert reg.get("s1") is None                                           # but the session row IS gone
    assert reg.spend("nobody") == {"sessions": 0, "total_cost_usd": 0.0}   # unknown agent → zero
    reg.close()
    print("ok  registry: DURABLE per-agent spend ledger (survives delete) + window (#2.3)")


def test_session_durability_views():
    # A restarted orchestrator must still surface sessions that live only on disk
    # (the registry + JSONL), not just the in-memory ones.
    from orchestrator.manager import SessionManager
    from terracore.events import EventStore

    rt, logs = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    cfg = Config(runtime_dir=rt, logs_dir=logs)
    mgr = SessionManager(cfg)
    store = EventStore(logs / "sX.jsonl")
    store.append("session_start", model="opus")
    store.record({"type": "user"})
    store.record({"type": "result", "total_cost_usd": 0.10, "usage": {"output_tokens": 50}})
    store.append("session_end")
    mgr.registry.upsert(session_id="sX", model="opus", status="terminated", runner="k8s", title="t")

    ids = {s["id"] for s in mgr.list()}              # list() merges live + registry-backed
    assert "sX" in ids
    s = mgr.summary_of("sX")                          # read-only summary from the log
    assert s and s["total_cost_usd"] == 0.10 and s["user_turns"] == 1 and s["model"] == "opus"
    assert mgr.summary_of("nope") is None             # unknown → None (API → 404)
    mgr.registry.close()
    print("ok  durability: registry-backed list + read-only summary survive restart")


def test_summary_fold_is_memoized():
    # The polled list path must not re-read + re-fold an unchanged log every request:
    # _fold_log memoizes by (mtime_ns, size) — cache HIT while unchanged, re-fold on append.
    from orchestrator.manager import SessionManager
    from terracore.events import EventStore

    rt, logs = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    mgr = SessionManager(Config(runtime_dir=rt, logs_dir=logs))
    store = EventStore(logs / "s1.jsonl")
    store.append("user", text="hi")
    c1, a1 = mgr._fold_log("s1")
    c2, a2 = mgr._fold_log("s1")
    assert c1 == c2 == 1
    assert a2 is a1, "unchanged log → same cached agg object (no re-fold)"
    store.append("assistant_text", text="yo")  # size changes → cache key invalidated
    c3, a3 = mgr._fold_log("s1")
    assert c3 == 2 and a3 is not a1, "append → re-fold"
    mgr.registry.close()
    print("ok  manager: session-summary fold memoized by log mtime+size (re-folds on append)")


def test_durability_fixes():
    # Regression guards for the MVP-1 review fixes (D1 / S2 / L3).
    import asyncio

    from orchestrator.manager import Session, SessionManager
    from orchestrator.runners import Runner
    from orchestrator.k8s_runner import K8sRunner
    from terracore.events import EventStore

    # D1: durable runners survive a restart (docker detached + k8s); the base
    # default is non-durable so unknown runners get a terminal write on shutdown.
    from orchestrator.runners import DockerRunner
    assert Runner.durable is False and K8sRunner.durable is True and DockerRunner.durable is True

    rt, logs = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    cfg = Config(runtime_dir=rt, logs_dir=logs)

    # S2: secret-bearing harness fields are stripped before they hit the registry.
    h = Harness(model="opus", env={"SECRET": "x"}, extra_options={"k": 1}, mcp_servers={"s": {}})
    s = Session("sZ", cfg, SessionConfig(harness=h), None)
    safe = json.loads(s._safe_harness_json())
    assert safe["model"] == "opus"
    assert not ({"env", "extra_options", "mcp_servers"} & safe.keys())

    # L3: delete unlinks the JSONL so a deleted session 404s (no log-fallback 200).
    mgr = SessionManager(cfg)
    st = EventStore(logs / "sD.jsonl"); st.append("session_start"); st.append("session_end")
    mgr.registry.upsert(session_id="sD", status="terminated", runner="k8s")
    assert asyncio.run(mgr.delete("sD")) is True
    assert mgr.summary_of("sD") is None and not (logs / "sD.jsonl").exists()
    mgr.registry.close()
    print("ok  durability fixes: durable flags (D1) · registry secret-strip (S2) · delete unlinks log (L3)")


def test_budget_hard_kill():
    import asyncio

    from orchestrator.manager import Session

    rt, logs = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    cfg = Config(runtime_dir=rt, logs_dir=logs, budget_hard_mult=1.25)
    s = Session("sB", cfg, SessionConfig(harness=Harness(model="opus", max_budget_usd=1.0)), None)

    stopped = {"n": 0}

    async def fake_stop():
        stopped["n"] += 1

    s.runner.stop = fake_stop  # type: ignore[assignment]

    async def step(ev):  # mirror _pump: bank then enforce
        s._bank(ev)
        await s._enforce_budget(ev)

    async def run():
        await step({"type": "result", "total_cost_usd": 1.1})   # under hard cap (1.25)
        assert not s._budget_killed and stopped["n"] == 0
        await step({"type": "result", "total_cost_usd": 1.30})  # over -> kill
        assert s._budget_killed and stopped["n"] == 1
        await step({"type": "result", "total_cost_usd": 9.0})   # idempotent
        assert stopped["n"] == 1

    asyncio.run(run())

    # Turn backstop: accepted result messages still provide a useful runaway
    # guard when a worker reports zero cost. They are not a trust boundary.
    cfg2 = Config(runtime_dir=Path(tempfile.mkdtemp()), logs_dir=Path(tempfile.mkdtemp()),
                  budget_max_turns=3)
    s2 = Session("sT", cfg2, SessionConfig(harness=Harness(model="opus", max_budget_usd=1.0)), None)
    killed2 = {"n": 0}

    async def fake_stop2():
        killed2["n"] += 1

    s2.runner.stop = fake_stop2  # type: ignore[assignment]

    async def run_turns():
        for _ in range(3):  # forged zero cost, within turn cap
            s2._bank({"type": "result", "total_cost_usd": 0.0})
            await s2._enforce_budget({"type": "result", "total_cost_usd": 0.0})
        assert not s2._budget_killed
        s2._bank({"type": "result", "total_cost_usd": 0.0})           # 4th > cap=3 -> kill
        await s2._enforce_budget({"type": "result", "total_cost_usd": 0.0})
        assert s2._budget_killed and killed2["n"] == 1

    asyncio.run(run_turns())
    assert "budget_exceeded" in [e["type"] for e in s.store.read()]

    # Runtime backstop on a FULLY SILENT hang: no events ever arrive, so the event-driven
    # path can't fire — enforce_runtime_backstop() (the watchdog's entry point) must still kill.
    import time as _time
    cfg3 = Config(runtime_dir=Path(tempfile.mkdtemp()), logs_dir=Path(tempfile.mkdtemp()),
                  budget_max_run_seconds=100)
    s3 = Session("sW", cfg3, SessionConfig(harness=Harness(model="opus")), None)
    killed3 = {"n": 0}
    async def fake_stop3():
        killed3["n"] += 1
    s3.runner.stop = fake_stop3  # type: ignore[assignment]

    async def run_silent():
        s3._turn_started = _time.monotonic()          # a turn just began
        assert await s3.enforce_runtime_backstop() is False and killed3["n"] == 0  # not yet over
        s3._turn_started = _time.monotonic() - 101     # …now wedged past the 100s cap
        assert await s3.enforce_runtime_backstop() is True and killed3["n"] == 1   # killed with no event
        assert await s3.enforce_runtime_backstop() is False, "idempotent once killed"
    asyncio.run(run_silent())
    assert "budget_exceeded" in [e["type"] for e in s3.store.read()]
    print("ok  budget: orchestrator-side hard-kill above SDK soft cap; runtime watchdog kills a silent hang")


def test_worker_lost_on_unexpected_stream_end():
    """#2.4: if the worker stream ends WITHOUT an intentional stop, the pump emits worker_lost
    (pod crash/OOM/evict) before session_end — distinct from a deliberate end."""
    import asyncio
    from orchestrator.manager import Session

    def make_session(sid):
        cfg = Config(runtime_dir=Path(tempfile.mkdtemp()), logs_dir=Path(tempfile.mkdtemp()))
        return Session(sid, cfg, SessionConfig(harness=Harness(model="opus")), None)

    async def ended_stream():  # ready → mid-turn → stream dies (worker gone)
        for ev in ({"type": "ready"}, {"type": "user", "text": "go"},
                   {"type": "status", "status": "running"}):
            yield ev

    s = make_session("sL")
    s.runner.events = ended_stream  # type: ignore[assignment]
    asyncio.run(s._pump())
    types = [e["type"] for e in s.store.read()]
    assert "worker_lost" in types, types
    lost = next(e for e in s.store.read() if e["type"] == "worker_lost")
    assert lost["mid_turn"] is True and lost["reason"] == "stream_ended"
    assert types.index("worker_lost") < types.index("session_end"), "worker_lost precedes session_end"

    s2 = make_session("sS")          # an INTENTIONAL stop → no worker_lost
    s2.runner.events = ended_stream  # type: ignore[assignment]
    s2._stopping = True
    asyncio.run(s2._pump())
    assert "worker_lost" not in [e["type"] for e in s2.store.read()]
    print("ok  supervision: worker_lost on unexpected stream end, not on deliberate stop (#2.4)")


def test_session_storage_limit_stops_producer():
    import asyncio

    from orchestrator.manager import Session

    root = Path(tempfile.mkdtemp())
    cfg = Config(
        runtime_dir=root, logs_dir=root,
        max_event_log_bytes=1, max_audit_log_bytes=0,
    )
    session = Session("sStorage", cfg, SessionConfig(), None)
    session.store.append("session_start")
    stopped = {"count": 0}

    async def stop():
        stopped["count"] += 1

    session.runner.stop = stop  # type: ignore[assignment]
    assert asyncio.run(session.enforce_storage_limits()) is True
    assert stopped["count"] == 1 and session._stopping is True
    assert session.store.read()[-1]["subtype"] == "storage_limit"
    print("ok  storage: oversized per-session evidence stops the producer without truncation")


def test_metrics_render():
    from orchestrator.metrics import render
    from orchestrator.registry import SessionRegistry

    reg = SessionRegistry(Path(tempfile.mkdtemp()) / "sessions.db")
    reg.upsert(session_id="a", status="running", total_cost_usd=0.5, runner="k8s")
    reg.upsert(session_id="b", status="terminated", total_cost_usd=0.3, runner="k8s")
    out = render(reg)
    assert "terrarium_sessions_active 1" in out
    assert "terrarium_sessions_total 2" in out
    assert "terrarium_spend_usd_total 0.800000" in out
    reg.close()
    print("ok  metrics: prometheus exposition folds the registry (active/total/spend)")


def test_templates():
    from terracore import models, templates
    from terracore.harness import HARNESS_FIELDS, Harness

    ids = {t["id"] for t in templates.list_templates()}
    assert {"research", "coder", "github-pr", "tldr"} <= ids
    t = templates.get("research")
    # Assert against the catalog constant, not a model literal: templates now pin via
    # terracore.models, so a new generation is one edit there instead of a literal per
    # template (which is how they fell a generation behind the console's picker).
    assert t and t.harness.model == models.SONNET and "WebSearch" in t.harness.allowed_tools
    # base (template) + explicit override — mirrors the create_agent merge
    base = json.loads(t.harness.to_json())
    overlay = {k: v for k, v in {"model": models.OPUS, "effort": "max"}.items() if k in HARNESS_FIELDS}
    merged = Harness.from_dict({**base, **overlay})
    assert merged.model == models.OPUS and merged.effort == "max" and "WebSearch" in merged.allowed_tools
    print("ok  templates: presets + base/override merge")


def test_cron():
    from datetime import datetime

    from orchestrator.schedules import cron_match, cron_valid

    dt = datetime(2026, 6, 21, 7, 30)
    cdow = (dt.weekday() + 1) % 7
    assert cron_match("30 7 * * *", dt)
    assert cron_match("*/15 * * * *", dt)                  # 30 % 15 == 0
    assert not cron_match("0 7 * * *", dt)                 # minute mismatch
    assert cron_match(f"30 7 * * {cdow}", dt)              # exact day-of-week
    assert not cron_match(f"30 7 * * {(cdow + 1) % 7}", dt)
    assert cron_match("0-59 0-23 21 6 *", dt)              # ranges + dom + month
    if cdow == 0:
        assert cron_match("30 7 * * 7", dt)                # 7 == Sunday
        assert cron_match("30 7 * * 1-7", dt)              # ranges preserve the Sunday alias
    assert cron_match("30 7 20 6 0", dt)                   # restricted dom/dow use cron OR
    assert cron_match("30 7 21 6 1", dt)
    assert not cron_match("30 7 20 6 1", dt)
    assert (not cron_valid("nope")) and cron_valid("0 0 * * *")
    # F31: range-aware validation rejects typos that previously passed (5 fields +
    # bool match) and would then silently never fire.
    assert cron_valid("0 7 * * *") and cron_valid("*/15 * * * *") and cron_valid("0,30 9-17 * * 1-5")
    assert cron_valid("* * * * 7")                         # 7 = Sunday alias
    for bad in ("99 99 * * *", "abc * * * *", "60 * * * *", "0 24 * * *",
                "* * 0 * *", "* * * 13 *", "0 7 * *", "*/0 * * * *"):
        assert not cron_valid(bad), f"should reject {bad!r}"
    print("ok  cron: 5-field matcher (steps, ranges, dow) + range-aware validation")


def test_schedule_store():
    from orchestrator.schedules import ScheduleStore

    p = Path(tempfile.mkdtemp()) / "schedules.json"
    st = ScheduleStore(p)
    s = st.create(name="nightly", agent_id="agt_x", prompt="go", cron="0 7 * * *")
    assert s.id.startswith("sch_") and s.enabled and st.get(s.id).cron == "0 7 * * *"
    st.update(s.id, enabled=False, cron="*/30 * * * *")
    assert st.get(s.id).enabled is False and st.get(s.id).cron == "*/30 * * * *"
    try:
        st.create(name="bad", agent_id="a", prompt="x", cron="not a cron")
        raise AssertionError("accepted bad cron")
    except ValueError:
        pass
    assert ScheduleStore(p).get(s.id) is not None          # persists across reopen
    assert st.delete(s.id) and st.get(s.id) is None
    print("ok  schedules: store crud + cron validation + persistence")


def test_session_create_rolls_back_failed_start():
    """A runner launch failure must release the in-memory reservation and invoke
    idempotent cleanup; otherwise it strands a ghost session/memory lock."""
    import asyncio as _aio
    import orchestrator.manager as manager_mod
    from orchestrator.manager import SessionManager, _new_id
    from orchestrator.runners import SessionConfig

    root = Path(tempfile.mkdtemp())
    mgr = SessionManager(Config(runtime_dir=root, logs_dir=root))
    cleaned: list[str] = []
    original = manager_mod.Session

    class FailedSession:
        def __init__(self, sid, config, sess, registry, notifier):
            self.id, self.sess, self.status = sid, sess, "starting"
            self.runner = self

        async def start(self):
            raise RuntimeError("launch failed")

        async def stop(self):
            cleaned.append(self.id)

    manager_mod.Session = FailedSession
    try:
        try:
            _aio.run(mgr.create(SessionConfig()))
            raise AssertionError("failed launch was accepted")
        except RuntimeError as exc:
            assert "launch failed" in str(exc)
        assert mgr.sessions == {} and len(cleaned) == 1
        ids = {_new_id() for _ in range(1000)}
        assert len(ids) == 1000
    finally:
        manager_mod.Session = original
        mgr.registry.close()
    print("ok  session create: failed launch cleans up reservation/resources; ids are collision-resistant")


def test_session_capacity_is_bounded():
    from types import SimpleNamespace

    from orchestrator.manager import CapacityError, SessionManager

    root = Path(tempfile.mkdtemp())
    mgr = SessionManager(Config(
        runtime_dir=root, logs_dir=root,
        max_live_sessions=2, max_live_sessions_per_agent=1,
    ))
    try:
        mgr.sessions["s1"] = SimpleNamespace(
            status="idle", sess=SimpleNamespace(agent_id="agt_a"),
        )
        mgr._check_capacity("agt_b")  # one global slot remains
        try:
            mgr._check_capacity("agt_a")
            raise AssertionError("accepted a second live session for one agent")
        except CapacityError:
            pass
        mgr.sessions["s2"] = SimpleNamespace(
            status="running", sess=SimpleNamespace(agent_id="agt_b"),
        )
        try:
            mgr._check_capacity(None)
            raise AssertionError("accepted a session above the global live cap")
        except CapacityError:
            pass
    finally:
        mgr.registry.close()
    print("ok  session capacity: global and per-agent admission bounds fail before launch")


def test_schedule_fire_idempotent():
    """#2.5: a cron tick fires AT MOST ONCE — a retry / restart within the same minute is
    dropped via the durable last_tick guard; a new tick fires again; manual fire always runs."""
    import asyncio as _aio
    from orchestrator.schedules import ScheduleStore, Scheduler
    st = ScheduleStore(Path(tempfile.mkdtemp()) / "schedules.json")
    s = st.create(name="nightly", agent_id="agt_x", prompt="go", cron="0 7 * * *")
    created = []

    class FakeAgents:
        def get(self, aid): return AgentSpec(id=aid, name="x", harness=Harness())

    class FakeSession:
        id = "sess_1"
        async def send_message(self, text): pass

    class FakeManager:
        async def create(self, sc): created.append(sc); return FakeSession()

    sched = Scheduler(store=st, manager=FakeManager(), agents=FakeAgents())

    async def go():
        return (await sched.fire(s.id, tick="202606270700"),   # fires
                await sched.fire(s.id, tick="202606270700"),   # SAME tick (retry/restart) → dropped
                await sched.fire(s.id, tick="202606270701"),   # next tick → fires
                await sched.fire(s.id))                         # manual (tick=None) → fires
    a, b, c, manual = _aio.run(go())
    assert a == "sess_1" and b is None and c == "sess_1" and manual == "sess_1", (a, b, c, manual)
    assert len(created) == 3, "same tick fires once; new tick + manual fire again"
    assert st.get(s.id).last_tick == "202606270701"
    print("ok  schedules: cron tick is at-most-once (durable dedupe, work_item #2.5)")


def test_notifier():
    from orchestrator.notify import Notifier

    n = Notifier("http://hook", ["session_end", "budget_exceeded"])
    assert n.active
    assert n.build_payload("s1", {"type": "assistant_text", "text": "hi"}) is None  # not subscribed
    p = n.build_payload("s1", {"type": "budget_exceeded", "cost_usd": 2.0, "cap_usd": 1.0, "ts": "t", "seq": 5})
    assert p and p["session_id"] == "s1" and p["cost_usd"] == 2.0 and p["type"] == "budget_exceeded"
    assert not Notifier(None, ["x"]).active
    print("ok  notifier: subscribed-event payloads only (inactive when no URL)")


def test_sdk_surface():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk"))
    from terrarium import cli
    from terrarium.client import TerrariumClient

    c = TerrariumClient("http://x")  # async-only client; constructing does no I/O
    assert hasattr(c, "schedules") and hasattr(c, "templates") and hasattr(c, "fleet")
    assert hasattr(c.schedules, "run") and hasattr(c.schedules, "create")
    import asyncio
    asyncio.run(c.aclose())
    pr = cli._build_parser()
    ns = pr.parse_args(["schedules", "add", "do it", "--agent", "agt_x", "--cron", "0 7 * * *"])
    assert ns.cmd == "schedules" and ns.op == "add" and ns.agent == "agt_x"
    ns2 = pr.parse_args(["run", "hello", "--agent", "agt_x", "--budget", "2"])
    assert ns2.cmd == "run" and ns2.budget == 2.0
    print("ok  sdk/cli: schedules/templates/fleet resources + terra-cli parser")


def test_scoped_tokens():
    from orchestrator.tokens import TokenStore, hash_token

    p = Path(tempfile.mkdtemp()) / "tokens.json"
    st = TokenStore(p)
    rec, raw = st.create("ci-runner", ["run"])
    assert raw.startswith("terra_") and rec.scopes == ["run"]
    pr = st.verify(raw)
    assert pr and pr.name == "ci-runner" and pr.has("run") and not pr.has("admin")  # can run, NOT admin
    assert pr.has("read")  # privilege ladder: run implies read (so run tokens can hit read-gated GETs)
    prd = st.verify(st.create("viewer", ["read"])[1])
    assert prd.has("read") and not prd.has("run") and not prd.has("admin")  # read is the floor
    assert st.verify("terra_nope") is None
    pa = st.verify(st.create("ops", ["admin"])[1])
    assert pa.has("admin") and pa.has("run") and pa.has("read")                     # admin implies all
    rec2, raw2 = st.create("dash", ["read"])
    text = p.read_text()
    assert raw2 not in text and hash_token(raw2) in text                            # hashed at rest
    assert any(t["id"] == rec.id for t in TokenStore(p).list())                     # persists across reopen
    assert st.delete(rec.id) and st.verify(raw) is None                             # delete revokes
    print("ok  tokens: scoped create/verify · admin implies all · hashed at rest · delete revokes")


def test_auth_scopes_enforced():
    try:
        from fastapi.testclient import TestClient
    except Exception:
        print("ok  auth: (skipped — fastapi TestClient unavailable)")
        return
    from orchestrator import api
    cfg = Config(runtime_dir=Path(tempfile.mkdtemp()), logs_dir=Path(tempfile.mkdtemp()), auth_token="ROOT")
    with TestClient(api.create_app(cfg)) as c:
        run_tok = c.post("/v1/tokens", json={"name": "ci", "scopes": ["run"]},
                         headers={"Authorization": "Bearer ROOT"}).json()["token"]
        h = {"Authorization": f"Bearer {run_tok}"}
        assert c.get("/v1/agents", headers=h).status_code == 200          # any valid token can read/run
        me = c.get("/v1/me", headers=h).json()
        assert me["name"] == "ci" and me["can"] == {"read": True, "run": True, "admin": False}
        assert c.post("/v1/tokens", json={"name": "x"}, headers=h).status_code == 403   # but not mint tokens
        assert c.delete("/v1/credentials", headers=h).status_code == 403  # ...nor touch credentials
        assert c.get("/v1/agents").status_code == 401                     # no token → 401
        assert c.get("/v1/tokens", headers={"Authorization": "Bearer ROOT"}).status_code == 200  # root admin ok
        assert c.get("/v1/me", headers={"Authorization": "Bearer ROOT"}).json()["can"]["admin"] is True

        # read token: GETs ok, mutations forbidden (least-privilege is now real)
        read_tok = c.post("/v1/tokens", json={"name": "v", "scopes": ["read"]},
                          headers={"Authorization": "Bearer ROOT"}).json()["token"]
        rh = {"Authorization": f"Bearer {read_tok}"}
        assert c.get("/v1/sessions", headers=rh).status_code == 200       # read can read
        assert c.post("/v1/agents", json={"name": "a", "harness": {"model": "opus"}},
                      headers=rh).status_code == 403                      # read CANNOT create
        # The admin-only host-path copy_in endpoint it used to check here is gone (nothing
        # called it, and /files/upload covers the same need on every runner without handing a
        # sandbox an arbitrary host path). Download is read-scoped, so a read token reaches it
        # — 404 for a session that doesn't exist, never 403.
        assert c.get("/v1/sessions/nope/files/x.txt", headers=rh).status_code == 404

        # Boundary references cannot be deleted into a broader fallback posture.
        root = {"Authorization": "Bearer ROOT"}
        prof = c.post("/v1/egress/profiles", json={"name": "locked"}, headers=root).json()
        env = c.post("/v1/environments", json={
            "name": "prod", "egress_profile": prof["id"],
        }, headers=root).json()
        agent = c.post("/v1/agents", json={
            "name": "bound", "environments": [env["id"]],
        }, headers=root).json()
        assert c.delete(f"/v1/environments/{env['id']}", headers=root).status_code == 409
        assert c.delete(f"/v1/egress/profiles/{prof['id']}", headers=root).status_code == 409
        assert c.patch(f"/v1/agents/{agent['id']}", json={"environments": []}, headers=root).status_code == 200
        assert c.delete(f"/v1/environments/{env['id']}", headers=root).status_code == 200
        assert c.delete(f"/v1/egress/profiles/{prof['id']}", headers=root).status_code == 200
    print("ok  auth: scoped token runs but can't admin; missing token 401; root admin ok")


def test_permission_mode_validation():
    from pydantic import ValidationError
    from orchestrator.api import HarnessRequest, ReconfigRequest
    # valid modes pass
    for m in ("default", "acceptEdits", "plan", "bypassPermissions"):
        assert HarnessRequest(permission_mode=m).permission_mode == m
        assert ReconfigRequest(permission_mode=m).permission_mode == m
    # a typo is a loud validation error, not a silent mis-apply in the worker
    for bad in ("acceptedits", "bypass", "allow", ""):
        try:
            HarnessRequest(permission_mode=bad)
            assert False, f"expected ValidationError for {bad!r}"
        except ValidationError:
            pass
    print("ok  api: permission_mode is a closed enum (bad value → 422, never reaches the worker)")


def test_stores_are_owner_only():
    """Every durable store writes 0600.

    This was per-store boilerplate and four of the seven omitted it — including
    agents.json, which persists harness.env (Session._safe_harness_json strips exactly
    that field from the registry because it carries secrets), and tokens.json, which
    holds API-token hashes. The mode now lives in JsonStore, so it can't be forgotten
    by the next store that gets added.
    """
    import stat

    from orchestrator.agents import AgentStore
    from orchestrator.egress import EgressPolicyStore
    from orchestrator.egress_profiles import EgressProfileStore
    from orchestrator.environments import EnvironmentStore
    from orchestrator.schedules import ScheduleStore
    from orchestrator.secret_store import SecretStore
    from orchestrator.secrets import UserSecretStore
    from orchestrator.tokens import TokenStore

    d = Path(tempfile.mkdtemp())
    vault = SecretStore(d / "vault.json", kek="k" * 32)
    vault.put("v", "x")
    stores = {
        "agents": AgentStore(d / "agents.json"),
        "schedules": ScheduleStore(d / "schedules.json"),
        "tokens": TokenStore(d / "tokens.json"),
        "profiles": EgressProfileStore(d / "profiles.json"),
        "environments": EnvironmentStore(d / "environments.json"),
        "policy": EgressPolicyStore(d / "policy.json", seed_allow=("example.com",)),
        "usersecrets": UserSecretStore(d / "index.json", vault),
        "vault": vault,
    }
    # force a write on the ones that only persist on mutation
    stores["agents"].create("a")
    stores["schedules"].create(name="s", agent_id="a", prompt="p", cron="0 * * * *")
    stores["tokens"].create("t", ["read"])
    stores["profiles"].create(name="p")
    stores["environments"].create(name="e")
    stores["usersecrets"].put("s", scopes=["example.com"], header="Authorization", value="v")
    for bad in (
        {"header": "X-Good\r\nX-Evil", "value": "v"},
        {"header": "X-Good", "value": "v\r\nX-Evil: yes"},
    ):
        try:
            stores["usersecrets"].put("bad", scopes=["example.com"], **bad)
            raise AssertionError("accepted an injectable HTTP header")
        except ValueError:
            pass

    for label, st in stores.items():
        assert st.path.exists(), f"{label} never wrote"
        mode = stat.S_IMODE(st.path.stat().st_mode)
        assert mode == 0o600, f"{label} is {oct(mode)}, not 0600"
    print("ok  stores: every durable JSON store is written 0600 (uniform via JsonStore)")


def test_json_store_recovers_or_fails_loudly():
    from orchestrator.store import JsonStore, StoreCorruptionError

    root = Path(tempfile.mkdtemp())
    store = JsonStore(root / "state.json")
    store._write({"generation": 1})
    store._write({"generation": 2})
    store.path.write_text("{broken")
    assert store._read({}) == {"generation": 1}

    lonely = JsonStore(root / "lonely.json")
    lonely.path.write_text("{broken")
    try:
        lonely._read({})
        raise AssertionError("corrupt control-plane state was silently treated as empty")
    except StoreCorruptionError as exc:
        assert "lonely.json" in str(exc)
    print("ok  stores: previous generation recovers corruption; no backup fails loudly")


def test_secret_store():
    from orchestrator.secret_store import SecretStore

    d = Path(tempfile.mkdtemp())
    st = SecretStore(d / "secrets.enc", kek="test-kek-passphrase")
    st.put("anthropic", "sk-ant-REALTOKEN-secret-value")
    assert st.get("anthropic") == "sk-ant-REALTOKEN-secret-value"
    assert st.list() == ["anthropic"]
    raw = (d / "secrets.enc").read_text()
    assert "REALTOKEN" not in raw and "sk-ant" not in raw           # opaque at rest
    assert SecretStore(d / "secrets.enc", kek="WRONG").get("anthropic") is None   # wrong KEK fails closed
    assert SecretStore(d / "secrets.enc", kek="test-kek-passphrase").get("anthropic") == "sk-ant-REALTOKEN-secret-value"  # persists
    assert st.delete("anthropic") and st.get("anthropic") is None
    print("ok  secret store: AES-GCM envelope · opaque at rest · wrong-KEK fails · persists · delete")


def test_file_mounted_secrets():
    """Secrets mounted as FILES must be read, not just secrets passed as env vars.

    An env var is visible in `docker inspect`, so a deployment that cares puts the admin
    bearer and the KEK in files instead (`/run/secrets/NAME` — the convention Swarm, k8s and
    shunt's `mode = "file"` share). Reading only the environment meant those deployments
    delivered the secrets correctly and the orchestrator then refused to start, reporting the
    token as unset — which reads as a delivery failure rather than a consumption gap.
    """
    import importlib
    import orchestrator.config as C

    d = Path(tempfile.mkdtemp())
    (d / "TERRA_TOKEN").write_text("tok-from-file\n")   # trailing newline: editors add them
    (d / "TERRA_KEK").write_text("kek-from-file")
    saved = C.SECRETS_DIR
    try:
        C.SECRETS_DIR = d
        assert C._secret("TERRA_TOKEN") == "tok-from-file", "trailing newline not stripped"
        assert C._secret("TERRA_KEK") == "kek-from-file"
        assert C._secret("NOT_MOUNTED") is None
        assert C._secret("NOT_MOUNTED", "fallback") == "fallback"

        # The environment wins when both exist: an explicit override for one run must not be
        # silently outranked by a file someone forgot was mounted.
        import os as _os
        _os.environ["TERRA_TOKEN"] = "tok-from-env"
        try:
            assert C._secret("TERRA_TOKEN") == "tok-from-env"
        finally:
            _os.environ.pop("TERRA_TOKEN", None)
    finally:
        C.SECRETS_DIR = saved
        importlib.reload(C)
    print("ok  config: secrets are read from /run/secrets when not in the environment")


def test_auth_boot_guard():
    from orchestrator import api
    mk = lambda: Path(tempfile.mkdtemp())
    try:
        api.create_app(Config(host="0.0.0.0", auth_token=None, runtime_dir=mk(), logs_dir=mk()))
        assert False, "should refuse non-loopback bind with no token"
    except RuntimeError as e:
        assert "TERRA_TOKEN" in str(e)
    # explicit opt-in, loopback, and token-set all start fine
    api.create_app(Config(host="0.0.0.0", auth_token=None, allow_no_auth=True, runtime_dir=mk(), logs_dir=mk()))
    api.create_app(Config(host="127.0.0.1", auth_token=None, runtime_dir=mk(), logs_dir=mk()))
    api.create_app(Config(host="0.0.0.0", auth_token="x", runtime_dir=mk(), logs_dir=mk()))
    print("ok  auth boot guard: refuse open non-loopback; allow with token / opt-in / loopback")


def test_k8s_warden_pod_spec():
    from orchestrator.k8s_runner import build_pod_manifest
    m = build_pod_manifest(name="s1", image="img", harness_json="{}", memory_pvc="pvc",
                           api_key="sk-real-secret", warden_port=8888,
                           warden_cred_secret="wcred", warden_policy_cm="wpol")
    spec = m["spec"]
    inits = spec.get("initContainers", [])
    assert len(inits) == 1 and inits[0]["name"] == "warden" and inits[0]["restartPolicy"] == "Always"  # native sidecar
    assert inits[0]["command"] == ["/opt/runtime/zstunnel"]
    assert inits[0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}  # Warden needs no caps
    assert inits[0]["securityContext"]["runAsUser"] == 1002        # distinct uid for shared-netns firewall
    warden_env = {e["name"]: e.get("value") for e in inits[0]["env"]}
    assert warden_env["WARDEN_LISTEN"] == "127.0.0.1:8888"         # loopback only (no 0.0.0.0)
    assert len(warden_env.get("WARDEN_RECEIPT_KEY", "")) == 32     # tamper-evident audit enabled in k8s
    wenv = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
    assert wenv["HTTPS_PROXY"] == "http://127.0.0.1:8888"          # loopback (shared netns)
    assert wenv["WARDEN_UID"] == "1002"                           # worker firewall allows Warden's egress by uid
    assert wenv["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/proxy-ca/session-ca.pem"   # generic CA path (not /warden-ca)
    assert wenv["ANTHROPIC_API_KEY"].startswith("sk-ant-api03-")   # realistic decoy in the worker
    assert wenv["ANTHROPIC_API_KEY"] != "sk-ant-warden-dummy"
    assert wenv["TERRA_EGRESS_DEFAULT_DROP"] == "1"
    assert all(e.get("value") != "sk-real-secret" for e in spec["containers"][0]["env"])  # real key NOT in worker
    vols = {v["name"] for v in spec["volumes"]}
    assert {"warden-ca", "wcred", "wpolicy"} <= vols
    print("ok  k8s warden pod: native sidecar · loopback proxy · decoy key (real absent) · shared CA/cred/policy")


def test_k8s_warden_subscription_decoy_shape():
    """F23 (functional): with a SUBSCRIPTION (no api_key) real cred, the k8s worker
    must get an OAuth-shaped decoy (~/.claude stub via TERRA_DECOY_OAUTH), NOT
    ANTHROPIC_API_KEY — otherwise the CLI omits the anthropic-beta oauth header and
    the injected subscription Bearer token is rejected upstream."""
    from orchestrator.config import decoy_oauth_stub
    from orchestrator.k8s_runner import build_pod_manifest
    from terracore.conceal import write_decoy_creds

    m = build_pod_manifest(name="s1", image="img", harness_json="{}", memory_pvc="pvc",
                           api_key=None, warden_port=8888,
                           warden_cred_secret="wcred", warden_policy_cm="wpol")
    wenv = {e["name"]: e.get("value") for e in m["spec"]["containers"][0]["env"]}
    assert "ANTHROPIC_API_KEY" not in wenv, "subscription mode must NOT use an api-key-shaped decoy"
    stub = json.loads(wenv["TERRA_DECOY_OAUTH"])
    assert "claudeAiOauth" in stub and stub["claudeAiOauth"]["accessToken"].startswith("sk-ant-oat01-")

    # the stub's expiry is far-future but NOT the old year-2286 sentinel
    import time as _t
    exp = decoy_oauth_stub()["claudeAiOauth"]["expiresAt"]
    assert exp != 9999999999999 and exp > _t.time() * 1000 + 300 * 24 * 3600 * 1000

    # the worker materializes the stub into ~/.claude (heap env, post-conceal)
    import os
    import tempfile
    home = tempfile.mkdtemp()
    saved = {"HOME": os.environ.get("HOME"), "TERRA_DECOY_OAUTH": os.environ.get("TERRA_DECOY_OAUTH")}
    try:
        os.environ["HOME"] = home
        os.environ["TERRA_DECOY_OAUTH"] = wenv["TERRA_DECOY_OAUTH"]
        dest = write_decoy_creds()
        assert dest and Path(dest).exists()
        assert json.loads(Path(dest).read_text())["claudeAiOauth"]["accessToken"].startswith("sk-ant-oat01-")
        assert (Path(dest).stat().st_mode & 0o777) == 0o600
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok  k8s warden pod: subscription → OAuth-shaped decoy (CLI emits oauth beta header) (F23)")


def test_build_command_warden():
    cfg = Config(api_key="sk-real-secret", runtime_dir=Path(tempfile.mkdtemp()))
    cfg.warden = True
    r = DockerRunner(session_id="w1", config=cfg, sess=SessionConfig())
    r._warden_endpoint = ("172.30.0.7", 8888)
    r._warden_ca_pem = b"-----BEGIN CERTIFICATE-----\n"
    s = " ".join(r.build_command())
    assert "HTTPS_PROXY=http://172.30.0.7:8888" in s
    assert "NODE_EXTRA_CA_CERTS=/etc/ssl/proxy-ca/session-ca.pem" in s   # generic CA path (not /warden-ca)
    assert "TERRA_EGRESS_DEFAULT_DROP=1" in s
    assert "ANTHROPIC_API_KEY=sk-ant-api03-" in s          # realistic DECOY key in the sandbox
    assert "sk-ant-warden-dummy" not in s                  # the obvious dummy is gone
    assert "sk-real-secret" not in s                       # the REAL key is NOT in the sandbox
    # The CA is seeded with `docker cp`, not bind-mounted, so it is not in the command.
    # Its integrity now rests on ownership rather than a `:ro` mount — see the seeding test.
    assert "-v" not in s.split("--name")[0] or "/etc/ssl/proxy-ca:ro" not in s
    # The CA rides a per-session VOLUME, mounted :ro. A docker cp into the container itself
    # cannot work: the sandbox rootfs is --read-only, which is the whole point of it.
    assert f"{r._seed_vol('ca')}:/etc/ssl/proxy-ca:ro" in s
    assert (_CA_PEM_SENTINEL, r._seed_vol("ca")) in r._seed
    assert r._warden_cred() == {"type": "apikey", "value": "sk-real-secret"}  # Warden gets the real one
    # the sandbox runs on a PER-SESSION network (not the shared one), so no co-located
    # container can route through this session's credential-injecting Warden
    assert f"--network {r.network}" in s
    assert r.network == "terrarium-net-w1" and r.network != cfg.network
    print("ok  build_command: warden mode (decoy key + generic CA path; real key absent; HTTPS_PROXY+default-drop)")


def test_egress_policy():
    from orchestrator.egress import EgressPolicyStore, read_audit

    d = Path(tempfile.mkdtemp())
    dests = lambda pol, action: {r["dest"] for r in pol["rules"] if r["action"] == action}  # noqa: E731
    st = EgressPolicyStore(d / "policy.json", seed_allow=("api.github.com", "*.bad", "API.GITHUB.COM"))
    p = st.get()
    assert dests(p, "allow") == {"api.github.com"} and p["mode"] == "enforce"  # wildcard + dup dropped, lowercased
    assert "api.anthropic.com" in p["always_allow"]
    st.set(mode="monitor", rules=[{"action": "allow", "dest": "PyPI.org"}, {"action": "deny", "dest": "telemetry.bad.com"}])
    p = st.get()
    assert p["mode"] == "monitor" and dests(p, "allow") == {"pypi.org"} and dests(p, "deny") == {"telemetry.bad.com"}
    try:
        st.set(mode="bogus"); assert False
    except ValueError:
        pass
    # the policy.json written above is the exact file each per-session Warden consumes;
    # the DECISION logic itself lives in (and is tested by) the Rust Warden.
    st.set(mode="enforce", kill=True)
    on_disk = json.loads((d / "policy.json").read_text())
    assert on_disk["mode"] == "enforce" and on_disk["kill"] is True and "rules" in on_disk

    # IP / CIDR destinations with per-rule ports (the internal-network mechanism) survive;
    # a bad IP/CIDR dest is dropped. allow_metadata is an explicit off-by-default flag.
    st.set(rules=[
        {"action": "allow", "dest": "10.0.0.0/8", "ports": [443, 5432]},
        {"action": "allow", "dest": "db.internal"},
        {"action": "allow", "dest": "::1"},
        {"action": "allow", "dest": "bad/33"},          # malformed CIDR → dropped
    ], allow_metadata=True)
    p = st.get()
    assert dests(p, "allow") == {"10.0.0.0/8", "db.internal", "::1"} and p["allow_metadata"] is True
    cidr = next(r for r in p["rules"] if r["dest"] == "10.0.0.0/8")
    assert cidr["ports"] == [443, 5432]
    assert json.loads((d / "policy.json").read_text())["allow_metadata"] is True  # reaches Warden's file
    st.set(allow_metadata=False)
    assert st.get()["allow_metadata"] is False
    assert read_audit(d / "nope.jsonl") == []
    (d / "audit.jsonl").write_text('{"decision":"allow","host":"a"}\nbad line\n{"decision":"deny","host":"b"}\n')
    a = read_audit(d / "audit.jsonl", limit=10)
    assert len(a) == 2 and a[-1]["host"] == "b"                             # tolerates junk, tails newest
    # tail-read stays O(limit) on a big file (DoS guard): last 5 of 5000, not a full read
    big = d / "big.jsonl"
    with big.open("w") as f:
        for i in range(5000):
            f.write(json.dumps({"decision": "deny", "host": f"h{i}.com"}) + "\n")
    tail = read_audit(big, limit=5)
    assert [r["host"] for r in tail] == [f"h{i}.com" for i in range(4995, 5000)]
    print("ok  egress policy store: defaults · clean_hosts (wildcard/dup/case) · validation · persist · kill · tail-read")


def test_personas():
    assert build_system_prompt("minimal") is None
    assert build_system_prompt("claude_code")["preset"] == "claude_code"
    assert isinstance(build_system_prompt("assistant"), str)
    assert build_system_prompt("custom", "XYZ") == "XYZ"
    assert set(MODES) >= {"minimal", "claude_code", "custom", "assistant"}
    # F26 (concealment): the default personas must not volunteer the boundary —
    # no "sandbox"/"terrarium" tell that primes an adversarial agent to probe.
    for prompt in (ASSISTANT_PROMPT, CUSTOM_PROMPT, build_system_prompt("assistant")):
        low = prompt.lower()
        assert "sandbox" not in low and "terrarium" not in low, f"persona leaks a tell: {prompt!r}"
    print("ok  personas: 4 modes + no concealment tell in the default prompts")


def test_cost_banks_across_rewind_reset():
    from orchestrator.manager import summarize_events

    # cost is cumulative within a CLI segment but resets when a rewind reconnects —
    # the total must bank the completed segment, not show the latest fragment.
    events = [
        {"type": "user", "seq": 1},
        {"type": "result", "seq": 2, "total_cost_usd": 0.10, "usage": {"output_tokens": 5}},
        {"type": "result", "seq": 3, "total_cost_usd": 0.17, "usage": {"output_tokens": 3}},
        {"type": "user", "seq": 4},
        {"type": "result", "seq": 5, "total_cost_usd": 0.02, "usage": {"output_tokens": 4}},  # rewind reset
        {"type": "result", "seq": 6, "total_cost_usd": 0.05, "usage": {"output_tokens": 2}},
    ]
    s = summarize_events(events)
    assert abs(s["total_cost_usd"] - 0.22) < 1e-9    # banked 0.17 + current 0.05 (NOT the 0.05 fragment)
    assert s["tokens"]["output"] == 14               # tokens summed regardless of cost segments
    # a monotonic (cumulative) session passes through unchanged — no double-counting
    s2 = summarize_events([{"type": "result", "total_cost_usd": 0.10}, {"type": "result", "total_cost_usd": 0.25}])
    assert abs(s2["total_cost_usd"] - 0.25) < 1e-9
    print("ok  cost: banks completed segments across a rewind reset (0.17+0.05); cumulative passes through")


def test_memory_isolation_on_concurrent():
    from orchestrator.manager import SessionManager

    mgr = SessionManager(Config(runtime_dir=Path(tempfile.mkdtemp()), runner="k8s"))
    mgr.registry.upsert(session_id="s-old", status="running", memory_volume="agent-x")
    assert mgr._memory_busy("agent-x") is True and mgr._memory_busy("free") is False
    sid = "20260622-141255-422"
    vol, isolated = mgr._resolve_memory("agent-x", sid)              # contended → isolate this session
    assert isolated is True and vol != "agent-x" and vol.startswith("agent-x-")
    vol2, iso2 = mgr._resolve_memory("free", sid)                    # uncontended → share as before
    assert iso2 is False and vol2 == "free"

    # F15: an IN-FLIGHT session (in self.sessions, registry row not yet written) must
    # also count as busy — else two concurrent creates both bind the RWO volume.
    from orchestrator.manager import Session
    from orchestrator.runners import SessionConfig as _SC
    inflight = Session("s-inflight", mgr.config, _SC(memory_volume="agent-y"), mgr.registry, mgr.notifier)
    mgr.sessions["s-inflight"] = inflight                            # synchronous reserve (pre-registry)
    assert mgr._memory_busy("agent-y") is True                      # seen before any registry row exists
    _, iso3 = mgr._resolve_memory("agent-y", "20260622-141300-999")
    assert iso3 is True                                             # second concurrent create isolates
    mgr.registry.close()
    print("ok  memory: concurrent same-agent session auto-isolates (registry + in-flight TOCTOU) (F15)")


def test_file_upload():
    import asyncio
    from orchestrator.filebridge import sanitize_name
    from orchestrator.runners import LocalRunner

    assert sanitize_name("my data.csv") == "my_data.csv"        # spaces → underscores
    assert sanitize_name("../../etc/passwd") == "passwd"        # path stripped (no traversal)
    assert sanitize_name("..") == "upload.bin"                  # degenerate → safe default
    r = LocalRunner(session_id="up1", config=Config(runtime_dir=Path(tempfile.mkdtemp())), sess=SessionConfig())
    name = asyncio.run(r.copy_in_bytes("../escape me.txt", b"payload"))
    assert name == "escape_me.txt" and ".." not in name and "/" not in name
    assert (r.workspace / name).read_bytes() == b"payload"      # content landed in the workspace
    print("ok  file upload: copy_in_bytes → workspace; sanitize_name blocks traversal, keeps names usable")


def test_file_download():
    """Download is a first-class, runner-independent operation now — and the guards hold.

    It was previously a docker-only endpoint with no SDK method and no console button, so
    an agent's output artifacts couldn't be retrieved at all on k8s. The host_path copy_in
    endpoint it shipped alongside is gone: nothing called it, and it read an arbitrary HOST
    path into a sandbox, which /files/upload does safely for every runner.
    """
    import asyncio
    import inspect

    from orchestrator.filebridge import MAX_DOWNLOAD_BYTES
    from orchestrator.k8s_runner import K8sRunner
    from orchestrator.runners import DockerRunner, LocalRunner, Runner

    # every runner implements it — the point of the change
    for cls in (DockerRunner, LocalRunner, K8sRunner):
        assert "copy_out_bytes" in cls.__dict__, f"{cls.__name__} cannot return a file"
    assert hasattr(Runner, "copy_out_bytes")

    ws = Path(tempfile.mkdtemp())
    r = LocalRunner(session_id="dl", config=Config(runtime_dir=ws), sess=SessionConfig())
    r.workspace.mkdir(parents=True, exist_ok=True)
    (r.workspace / "report.md").write_bytes(b"# findings")
    assert asyncio.run(r.copy_out_bytes("report.md")) == b"# findings"

    # traversal is rejected by the name rule, before anything touches the filesystem
    for bad in ("../../etc/passwd", "a/b", "..", "x;y"):
        try:
            asyncio.run(r.copy_out_bytes(bad))
            raise AssertionError(f"accepted unsafe download name {bad!r}")
        except ValueError:
            pass

    # a symlink is refused even though its NAME is safe and it resolves to a real file —
    # the sandbox chooses the link target, so following it would escape the workspace.
    outside = ws / "secret.txt"
    outside.write_bytes(b"not yours")
    (r.workspace / "link.txt").symlink_to(outside)
    try:
        asyncio.run(r.copy_out_bytes("link.txt"))
        raise AssertionError("followed a symlink out of the workspace")
    except ValueError:
        pass

    # size is capped: the agent picks the size, so an unbounded read is a control-plane OOM
    (r.workspace / "big.bin").write_bytes(b"x" * (MAX_DOWNLOAD_BYTES + 1))
    try:
        asyncio.run(r.copy_out_bytes("big.bin"))
        raise AssertionError("returned a file over the cap")
    except ValueError:
        pass

    # the host-path copy_in endpoint and its model are gone, not merely unrouted
    import orchestrator.api as api
    import orchestrator.filebridge as fb
    assert not hasattr(fb, "copy_in"), "filebridge.copy_in (arbitrary host path) is back"
    assert not hasattr(api, "FileInRequest")
    paths = {r.path for r in api.create_app(Config(runtime_dir=ws)).routes if hasattr(r, "path")}
    assert "/v1/sessions/{sid}/files" not in paths
    assert "/v1/sessions/{sid}/files/{name}" in paths

    # the SDK can actually reach it (the gap that made the endpoint dead in the first place)
    from terrarium.client import SessionsResource
    assert "download_file" in SessionsResource.__dict__
    assert "dest" in inspect.signature(SessionsResource.download_file).parameters
    print("ok  file download: every runner, name+symlink+size guarded, host-path copy_in removed")


def test_protocol():
    assert P.query_cmd("a") == {"cmd": "query", "text": "a"}
    assert P.interrupt_cmd()["cmd"] == "interrupt"
    assert P.shutdown_cmd()["cmd"] == "shutdown"
    assert P.rewind_cmd("uuid-1", "conversation") == {"cmd": "rewind", "message_id": "uuid-1", "mode": "conversation"}
    assert P.rewind_cmd("u")["mode"] == "files"  # default mode
    assert P.EV_REWIND_POINT == "rewind_point" and P.EV_REWOUND == "rewound"
    # AskUserQuestion: answer command + the question/answered events the worker emits
    assert P.answer_cmd("q1", {"Risk?": "Moderate"}) == {"cmd": "answer", "question_id": "q1", "answers": {"Risk?": "Moderate"}}
    assert P.decision_cmd("p1", "always") == {"cmd": "decision", "request_id": "p1", "decision": "always"}
    assert P.EV_QUESTION == "question" and P.EV_ANSWERED == "answered"
    assert P.EV_PERMISSION == "permission" and P.EV_DECIDED == "decided"
    # all interactive events must pass the worker-event trust boundary un-quarantined (else the UI never sees them)
    for t in (P.EV_QUESTION, P.EV_ANSWERED, P.EV_PERMISSION, P.EV_DECIDED):
        assert P.validate_worker_event({"type": t, "request_id": "x"})["type"] == t
    print("ok  protocol: command builders (rewind + answer + decision) + question/permission events")


def test_harness():
    h = Harness(model="opus", allowed_tools=["Read", "Bash"], thinking={"type": "adaptive"},
                max_turns=5, max_budget_usd=2.0, skills=True, interactive=True, mcp_servers={"x": {"type": "url"}})
    h2 = Harness.from_json(h.to_json())
    assert h2.model == "opus" and h2.allowed_tools == ["Read", "Bash"]
    assert h2.thinking == {"type": "adaptive"} and h2.max_turns == 5 and h2.skills is True
    assert h2.interactive is True and Harness().interactive is False  # off by default (unattended-safe)
    assert Harness.from_json(Harness(approval="edits").to_json()).approval == "edits" and Harness().approval == "off"
    assert Harness.from_json(Harness(approval=["Bash"]).to_json()).approval == ["Bash"]  # list form round-trips
    assert h2.mcp_servers == {"x": {"type": "url"}}
    assert Harness.from_dict({"model": "haiku", "bogus": 1}).model == "haiku"  # tolerant
    print("ok  harness: round-trip + tolerant parse")


def test_memory_mode():
    """memory_mode decides whether the sandbox mounts the per-agent RWO PVC — the single biggest
    lever on k8s launch latency (measured: 1.6s pod start without it vs 11.4s with, on a ~23s
    launch). "volume" keeps today's durable mount; "synced"/"none" swap it for an emptyDir."""
    from orchestrator.k8s_runner import build_pod_manifest

    def mem_volume(mode):
        m = build_pod_manifest(name="s1", image="img", harness_json="{}", memory_pvc="pvc-x",
                               memory_mode=mode)
        vols = m["spec"]["volumes"]
        return next(v for v in vols if v["name"] == "memory")

    # new agents default to the fast mode; pre-existing ones are pinned to "volume" by a
    # migration (test_memory_mode_migration) so nobody's memory silently moves stores
    assert Harness().memory_mode == "synced"
    assert mem_volume("volume")["persistentVolumeClaim"]["claimName"] == "pvc-x"
    # the fast modes mount NO pvc — this is what removes the ~11s Longhorn attach
    for fast in ("synced", "none"):
        v = mem_volume(fast)
        assert "persistentVolumeClaim" not in v, f"{fast} must not mount the PVC"
        assert v["emptyDir"]["sizeLimit"] == "256Mi", f"{fast} should get a bounded emptyDir"
    # /memory is still mounted in every mode — only its BACKING changes, so the agent
    # (and the persona prompt that tells it to write notes there) never sees a missing path
    for mode in ("volume", "synced", "none"):
        m = build_pod_manifest(name="s1", image="img", harness_json="{}", memory_pvc="p",
                               memory_mode=mode)
        worker = next(c for c in m["spec"]["containers"] if c["name"] == "worker")
        assert any(mt["mountPath"] == "/memory" for mt in worker["volumeMounts"]), mode
    # the worker's wait is driven by an explicit runner signal, present only in synced
    for mode, want in (("volume", False), ("synced", True), ("none", False)):
        m = build_pod_manifest(name="s1", image="img", harness_json="{}", memory_pvc="p",
                               memory_mode=mode)
        worker_c = next(c for c in m["spec"]["containers"] if c["name"] == "worker")
        has = any(e["name"] == "TERRA_MEMORY_RESTORE" for e in worker_c["env"])
        assert has is want, f"{mode} should {'' if want else 'not '}promise a restore"
    # round-trips through the harness + the API surface
    assert Harness.from_json(Harness(memory_mode="synced").to_json()).memory_mode == "synced"
    print("ok  memory_mode: volume mounts the PVC; synced/none skip it (the ~11s k8s attach)")


def test_memory_mode_migration():
    """New agents default to memory_mode="synced", but "volume" and "synced" read DIFFERENT stores
    (PVC vs orchestrator snapshot). Without pinning, flipping the default would make every existing
    agent boot with an empty /memory while its real memory sat untouched in an unmounted volume."""
    from orchestrator.migrations import migrate_pin_memory_mode

    d = Path(tempfile.mkdtemp())
    path = d / "agents.json"
    path.write_text(json.dumps({
        "agt_old":     {"name": "old",     "harness": {"model": "x"}},                        # pre-field
        "agt_explicit": {"name": "explicit","harness": {"model": "x", "memory_mode": "none"}},  # chosen
    }))

    assert migrate_pin_memory_mode(path) == 1, "only the agent lacking the key is pinned"
    after = json.loads(path.read_text())
    assert after["agt_old"]["harness"]["memory_mode"] == "volume", "legacy agent keeps its PVC memory"
    assert after["agt_explicit"]["harness"]["memory_mode"] == "none", "an explicit choice is never overwritten"

    # idempotent: a second boot changes nothing
    assert migrate_pin_memory_mode(path) == 0
    assert json.loads(path.read_text()) == after
    # missing / malformed store must not block boot
    assert migrate_pin_memory_mode(d / "nope.json") == 0
    (d / "bad.json").write_text("{{{")
    assert migrate_pin_memory_mode(d / "bad.json") == 0
    print("ok  memory migration: legacy agents pinned to volume; explicit choices preserved")



def test_approval_gate_hook():
    """`approval` must install a PreToolUse hook — can_use_tool alone cannot gate.

    Any bare tool name in allowed_tools auto-approves that tool BEFORE can_use_tool is
    consulted, so an approval policy that rides only on the callback silently never
    fires. The hook runs ahead of that auto-approval and returns "ask", which routes the
    call back into can_use_tool where the operator-prompt logic lives.
    """
    import sandbox.worker as W

    async def _cb(*a, **k):  # stand-in can_use_tool
        return None

    async def _hook(*a, **k):
        return {}

    # Unattended: no gate, whatever `approval` says (the OS sandbox is the boundary).
    h = Harness(interactive=False, approval="all")
    opts = W._build_options(h, can_use_tool=_cb, pre_tool_hook=_hook)
    assert not getattr(opts, "hooks", None), "unattended session must not install a gate"

    # Interactive + approval off: nothing to gate.
    h = Harness(interactive=True, approval="off")
    opts = W._build_options(h, can_use_tool=_cb, pre_tool_hook=_hook)
    assert not getattr(opts, "hooks", None), "approval='off' must not install a gate"

    # Interactive + a real policy: the hook is installed for EVERY tool (matcher=None),
    # since the hook body decides which tools need the operator.
    for approval in ("all", "edits", ["Bash"]):
        h = Harness(interactive=True, approval=approval)
        opts = W._build_options(h, can_use_tool=_cb, pre_tool_hook=_hook)
        hooks = getattr(opts, "hooks", None) or {}
        assert "PreToolUse" in hooks, f"approval={approval!r} must install a PreToolUse hook"
        matchers = hooks["PreToolUse"]
        assert matchers and matchers[0].matcher is None, "gate must apply to every tool"

    print("ok  approval gate installs a PreToolUse hook (and only when interactive)")



def test_resume_cursor():
    """The resume cursor must land on the last TURN BOUNDARY, never the log's tail.

    Issue #6: attach() seeded _last_seq = -1, so a reattached session replayed history and
    re-ran completed client_tool_call handlers (real side effects in the consumer's process).
    The cursor is what makes attach resumable; these are the properties it must hold.
    """
    from orchestrator.manager import summarize_events

    # A completed turn: the client tool call is INSIDE the turn, `result` closes it.
    events = [
        {"seq": 0, "type": "ready"},
        {"seq": 1, "type": "user"},
        {"seq": 2, "type": "client_tool_call", "name": "lookup"},
        {"seq": 3, "type": "assistant_text"},
        {"seq": 4, "type": "result"},
        {"seq": 5, "type": "status", "status": "idle"},
    ]
    cur = summarize_events(events)["resume_cursor"]
    assert cur == 5, cur
    # The whole point: resuming here skips the completed tool call, so its handler
    # is never re-executed.
    assert not [e for e in events if e["seq"] > cur and e["type"] == "client_tool_call"]

    # Mid-turn: cursor lags to the last boundary, so a PENDING tool call still replays
    # (it genuinely needs an answer). Lagging is the safe direction.
    mid = events[:4] + [
        {"seq": 4, "type": "result"},
        {"seq": 5, "type": "status", "status": "running"},
        {"seq": 6, "type": "client_tool_call", "name": "lookup"},
    ]
    cur_mid = summarize_events(mid)["resume_cursor"]
    assert cur_mid == 5, cur_mid
    assert [e for e in mid if e["seq"] > cur_mid and e["type"] == "client_tool_call"]

    # A session that never reached a boundary yields -1 → full drain (old behavior),
    # which is how a still-starting session keeps working.
    assert summarize_events([{"seq": 0, "type": "user"}])["resume_cursor"] == -1
    assert summarize_events([])["resume_cursor"] == -1

    print("ok  resume_cursor: last turn boundary; completed client-tool calls not replayed")


def test_resume_cursor_survives_restart():
    """Criterion 3: the cursor comes from the log/registry, not in-memory session state.

    A session the manager has never held (orchestrator restarted, session reaped) must still
    report a usable cursor from its JSONL, or reattach silently regresses to full replay.
    """
    import json as _j
    from orchestrator.manager import SessionManager
    from orchestrator.config import Config

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "logs").mkdir()
        sid = "s-restart"
        with open(root / "logs" / f"{sid}.jsonl", "w") as fh:
            for ev in [
                {"seq": 0, "type": "ready"},
                {"seq": 1, "type": "user"},
                {"seq": 2, "type": "client_tool_call", "name": "lookup"},
                {"seq": 3, "type": "result"},
            ]:
                fh.write(_j.dumps(ev) + "\n")

        cfg = Config(runtime_dir=root, logs_dir=root / "logs")
        mgr = SessionManager(cfg)
        summary = mgr.summary_of(sid)          # log-only path: nothing live in memory
        assert summary is not None, "log-only session must still resolve"
        assert summary["resume_cursor"] == 3, summary["resume_cursor"]

    print("ok  resume_cursor: recovered from the log after an orchestrator restart")



def test_spend_ledger_survives_session_deletion():
    """Usage must report what was actually spent, not what is still listed.

    The console's Usage view used to fold the live session list, so deleting a session
    retroactively erased its cost from the fleet total — you could lower reported spend by
    tidying up. The spend ledger exists precisely to outlive the session row (see the DDL),
    and these are the queries the /v1/usage window is built on."""
    from orchestrator.registry import SessionRegistry

    with tempfile.TemporaryDirectory() as td:
        reg = SessionRegistry(Path(td) / "sessions.db")
        rows = [("s1", "agt_a", "2026-07-20T10:00:00Z", 1.50),
                ("s2", "agt_a", "2026-07-20T18:00:00Z", 0.25),
                ("s3", "agt_b", "2026-07-22T09:00:00Z", 4.00),
                ("s4", None,    "2026-07-28T09:00:00Z", 0.10)]  # inline harness → no agent_id
        for sid, agent, ts, cost in rows:
            reg.upsert(session_id=sid, agent_id=agent, created_ts=ts,
                       status="terminated", total_cost_usd=cost)

        # Daily series: one bucket per UTC day, same-day sessions summed, oldest first.
        assert reg.spend_series("2026-07-01T00:00:00Z") == [
            {"day": "2026-07-20", "sessions": 2, "total_cost_usd": 1.75},
            {"day": "2026-07-22", "sessions": 1, "total_cost_usd": 4.0},
            {"day": "2026-07-28", "sessions": 1, "total_cost_usd": 0.1},
        ]
        # The window is a real filter, not decoration.
        assert [d["day"] for d in reg.spend_series("2026-07-21T00:00:00Z")] == ["2026-07-22", "2026-07-28"]
        # Biggest spender first; the inline (agent_id=None) bucket is kept so the per-agent
        # rows still sum to the fleet total instead of quietly dropping inline spend.
        by_agent = reg.spend_by_agent()
        assert [a["agent_id"] for a in by_agent] == ["agt_b", "agt_a", None]
        assert round(sum(a["total_cost_usd"] for a in by_agent), 6) == 5.85

        # THE POINT: delete the session row; the ledger keeps its cost.
        reg.remove("s3")
        assert reg.get("s3") is None
        assert sum(d["total_cost_usd"] for d in reg.spend_series("2026-07-01T00:00:00Z")) == 5.85
        assert reg.spend("agt_b")["total_cost_usd"] == 4.0
        reg.close()

    print("ok  usage: spend ledger survives session deletion (window + per-agent)")


def test_model_catalog_is_single_source():
    """One catalog, consumed by everything.

    The list lived in four places that drifted: the console's agent form, the console's alias
    list (used by the live model switcher), templates.py, and Config.default_model. Symptom: a
    session on claude-opus-5 was offered only sonnet|opus|haiku to switch to. This asserts the
    catalog is authoritative — the default resolves through it, every template pins a model it
    lists, and the console fetches it rather than shipping its own copy."""
    from pathlib import Path as _P

    from terracore import models, templates
    from orchestrator.config import Config

    cat = models.catalog()
    ids = [m["id"] for m in cat]
    assert len(ids) == len(set(ids)), "duplicate model ids"
    assert models.DEFAULT_MODEL in ids
    assert Config().default_model in ids            # the deployment default is a listed model
    for m in cat:                                   # wire shape the console types against
        assert set(m) == {"id", "label", "alias", "note"}
    # Aliases must be bare names (the CLI resolves them); concrete ids must be pinned.
    for m in cat:
        assert m["alias"] == (not m["id"].startswith("claude-")), m["id"]
    assert models.known(models.OPUS) and models.known(models.SONNET) and models.known(models.HAIKU)
    assert models.label("claude-opus-5") == "Opus 5"
    assert models.label("some-future-model") == "some-future-model"   # unknown → readable, not blank

    # Every built-in template pins a model the catalog actually offers (they were a generation
    # behind, so a one-click agent launched on an older model than the form could even select).
    for t in templates.list_templates():
        assert t["harness"]["model"] in ids, f'template {t["id"]} pins unlisted {t["harness"]["model"]}'

    # The console must not reintroduce a hardcoded list. Checked on CODE, with comments
    # stripped — a comment explaining the drift this replaced is not itself a hardcoded id.
    import re as _re

    def _strip_comments(src: str) -> str:
        src = _re.sub(r"/\*.*?\*/", "", src, flags=_re.S)
        return _re.sub(r"(?m)^\s*//.*$|\s//.*$", "", src)

    web = _P(__file__).resolve().parent.parent / "web"
    if web.is_dir():
        for f in ("components/AgentForm.tsx", "components/SessionsView.tsx",
                  "components/SessionView.tsx", "lib/harness.ts"):
            code = _strip_comments((web / f).read_text())
            # Model families only — "claude-api" is a SKILL name, not a model.
            hits = _re.findall(
                r"""['"`](claude-(?:opus|sonnet|haiku|fable)[a-z0-9.-]*|opus|sonnet|haiku)['"`]""", code)
            assert not hits, f"{f} hardcodes model id(s) {hits} — read the catalog via useModels()"
        assert "useModels" in (web / "lib/queries.ts").read_text()
    print("ok  models: one catalog drives the default, templates, and every console picker")


def test_dropped_stream_reattaches_a_live_sandbox():
    """A dropped event stream must not terminate a sandbox that is still running.

    For a durable runner the stream is a CLIENT of the sandbox — a `docker attach`
    subprocess — not the sandbox itself, and it dies for reasons the agent knows nothing
    about: a Docker daemon restart, a host suspend, an idle connection closed. Treating the
    first EOF as death is what marked healthy sessions `terminated` after a day or two of
    idling, with the container still up and the agent still fine.
    """
    import asyncio as _a
    from orchestrator.config import Config
    from orchestrator.manager import Session
    from orchestrator.runners import SessionConfig

    class FlakyRunner:
        """Ends its stream once, stays 'running', then behaves."""
        durable = True

        def __init__(self, states):
            self.states = list(states)
            self.reattaches = 0
            self.batches = [
                [{"type": "ready"}],   # first attach
                [{"type": "status", "status": "idle"}],  # after reattach
                [],                    # then nothing
            ]

        async def events(self):
            for ev in (self.batches.pop(0) if self.batches else []):
                yield ev

        async def probe_state(self): return self.states.pop(0) if self.states else "gone"
        async def reattach(self): self.reattaches += 1
        async def snapshot_memory(self): return None
        async def drain_audit(self): return None

    def run(states):
        cfg = Config(runtime_dir=Path(tempfile.mkdtemp()), logs_dir=Path(tempfile.mkdtemp()))
        sess = Session("s-flaky", cfg, SessionConfig())
        sess.runner = FlakyRunner(states)
        _a.run(sess._pump())
        types = [e["type"] for e in sess.store.read()]
        return sess, types

    # sandbox still running -> reattach, no terminal record until it is really gone
    sess, types = run(["running", "gone"])
    assert sess.runner.reattaches == 1, "a live sandbox was not reattached"
    assert "reattached" in types, types
    assert types.count("session_end") == 1 and types[-1] == "session_end", types

    # sandbox actually gone -> terminate immediately, exactly one worker_lost
    sess, types = run(["gone"])
    assert sess.runner.reattaches == 0
    assert types.count("worker_lost") == 1 and types[-1] == "session_end", types

    # a deliberate stop still closes the session out, and never as a worker death
    cfg = Config(runtime_dir=Path(tempfile.mkdtemp()), logs_dir=Path(tempfile.mkdtemp()))
    sess = Session("s-stop", cfg, SessionConfig())
    sess.runner = FlakyRunner(["running"])
    sess._stopping = True
    _a.run(sess._pump())
    types = [e["type"] for e in sess.store.read()]
    assert "worker_lost" not in types and types[-1] == "session_end", types
    assert sess.runner.reattaches == 0, "a deliberate stop must not reattach"
    print("ok  sessions: a dropped stream reattaches a live sandbox; only a dead one terminates")


def test_abnormal_ends_are_reported():
    """A killed run must be distinguishable from a finished one, in the SUMMARY.

    `worker_lost` (the sandbox died) and `budget_exceeded` (a backstop hard-stopped the run)
    are the two outcomes an operator scans a fleet for, and both used to fold into the same
    grey "terminated" row — visible only as an unstyled debug line at the foot of a transcript
    nobody had a reason to open.
    """
    from orchestrator.manager import summarize_events

    def fold(*types):
        return summarize_events([{"seq": i, "ts": "2026-07-31T00:00:0%dZ" % i, "type": t}
                                 for i, t in enumerate(types)])

    assert fold("session_start", "user", "result", "session_end")["terminal"] is None
    assert fold("session_start", "user", "budget_exceeded", "session_end")["terminal"] == "budget"
    assert fold("session_start", "user", "worker_lost", "session_end")["terminal"] == "lost"
    # A run that blew its budget AND then lost its sandbox reports the sandbox death: it is
    # the later fact and the one that changes what the operator can do next.
    assert fold("budget_exceeded", "worker_lost")["terminal"] == "lost"

    # and it survives the trip through both summary builders (live + read-only)
    import tempfile, json as _j
    from pathlib import Path as _P
    from orchestrator.config import Config
    from orchestrator.manager import SessionManager
    with tempfile.TemporaryDirectory() as td:
        root = _P(td); (root / "logs").mkdir()
        with open(root / "logs" / "s-killed.jsonl", "w") as fh:
            for i, t in enumerate(["session_start", "user", "budget_exceeded", "session_end"]):
                fh.write(_j.dumps({"seq": i, "ts": "2026-07-31T00:00:0%dZ" % i, "type": t}) + "\n")
        mgr = SessionManager(Config(runtime_dir=root, logs_dir=root / "logs"))
        mgr.registry.upsert(session_id="s-killed", created_ts="2026-07-31T00:00:00Z", status="terminated")
        assert mgr.summary_of("s-killed")["terminal"] == "budget"
        assert mgr.list_page(limit=10)["sessions"][0]["terminal"] == "budget"
    print("ok  sessions: a budget kill / lost sandbox is reported in the summary, not just the log")


def test_session_list_pages():
    """The list is paged, and the counts stay fleet-wide while the rows page.

    Sessions are durable and accumulate for the life of the deployment, so an unbounded
    listing grew without limit in payload and render cost — on a 5s poll. The properties
    that have to hold: paging returns each session exactly once in the same newest-first
    order the unpaged list used, `total`/`running` describe the FLEET (a live badge must
    not shrink as you page), and a cursor naming a since-deleted row still resumes in the
    right place rather than restarting.
    """
    import json as _j
    from orchestrator.config import Config
    from orchestrator.manager import SessionManager

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "logs").mkdir()
        ids = [f"s-{i:02d}" for i in range(12)]
        cfg = Config(runtime_dir=root, logs_dir=root / "logs")
        mgr = SessionManager(cfg)
        for i, sid in enumerate(ids):
            ts = f"2026-07-{i + 1:02d}T10:00:00Z"
            with open(root / "logs" / f"{sid}.jsonl", "w") as fh:
                fh.write(_j.dumps({"seq": 0, "ts": ts, "type": "session_start"}) + "\n")
            # two of them are still alive, and they are NOT on the first page by age
            mgr.registry.upsert(session_id=sid, created_ts=ts,
                                status="running" if i in (0, 1) else "terminated")

        expected = [s["id"] for s in mgr.list()]
        assert expected == sorted(ids, reverse=True)

        walked, cursor, pages = [], None, 0
        while True:
            page = mgr.list_page(limit=5, before=cursor)
            # counts describe the fleet on EVERY page, not the page
            assert page["total"] == 12 and page["running"] == 2, page
            walked += [s["id"] for s in page["sessions"]]
            pages += 1
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert pages == 3 and walked == expected, walked

        # A cursor whose row has since been deleted still resumes at the same position:
        # the composite key is compared, not looked up.
        first = mgr.list_page(limit=5)
        stale = first["next_cursor"]
        mgr.registry.remove(first["sessions"][-1]["id"])
        (root / "logs" / f"{first['sessions'][-1]['id']}.jsonl").unlink()
        assert [s["id"] for s in mgr.list_page(limit=5, before=stale)["sessions"]] == expected[5:10]

        # usage_totals folds the window, not a page — the reason the console stopped
        # summing the session list when the list became paged.
        assert mgr.usage_totals("2026-07-01T00:00:00Z")["tokens"]["total"] == 0
    print("ok  sessions: paged newest-first, fleet-wide counts, cursor survives a deleted row")


def test_session_list_is_time_ordered():
    """The list carries created_ts and comes back newest-first.

    Both were missing: the summary had NO time field at all (so the console could show neither
    an age nor a sort), and list() emitted live sessions in dict order followed by terminated
    ones by created_ts DESC — an order that changed across a restart. A log-only session (row
    reaped) must still report a time, taken from its first event."""
    import json as _j
    from orchestrator.config import Config
    from orchestrator.manager import SessionManager

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "logs").mkdir()
        stamps = {"s-old": "2026-07-01T10:00:00Z", "s-mid": "2026-07-20T10:00:00Z",
                  "s-new": "2026-07-28T10:00:00Z"}
        for sid, ts in stamps.items():
            with open(root / "logs" / f"{sid}.jsonl", "w") as fh:
                fh.write(_j.dumps({"seq": 0, "ts": ts, "type": "session_start"}) + "\n")
                fh.write(_j.dumps({"seq": 1, "ts": ts, "type": "ready"}) + "\n")

        cfg = Config(runtime_dir=root, logs_dir=root / "logs")
        mgr = SessionManager(cfg)
        # Registry rows deliberately inserted OUT of chronological order, so a correct result
        # can only come from sorting — not from insertion order.
        for sid in ("s-mid", "s-new", "s-old"):
            mgr.registry.upsert(session_id=sid, created_ts=stamps[sid], status="terminated")
        assert [s["id"] for s in mgr.list()] == ["s-new", "s-mid", "s-old"]
        assert mgr.summary_of("s-new")["created_ts"] == stamps["s-new"]

        # A session with no registry row at all still reports a time, from the log.
        mgr.registry.remove("s-mid")
        mgr._fold_cache.clear()
        assert mgr.summary_of("s-mid")["created_ts"] == stamps["s-mid"]

        # Equal timestamps must still order deterministically (total order, id as tiebreak) —
        # otherwise the list reshuffles between polls and rows jump under the cursor.
        for sid in ("t-a", "t-b", "t-c"):
            with open(root / "logs" / f"{sid}.jsonl", "w") as fh:
                fh.write(_j.dumps({"seq": 0, "ts": stamps["s-old"], "type": "ready"}) + "\n")
            mgr.registry.upsert(session_id=sid, created_ts=stamps["s-old"], status="terminated")
        tied = [s["id"] for s in mgr.list() if s["id"].startswith("t-")]
        assert tied == ["t-c", "t-b", "t-a"], tied
        assert tied == [s["id"] for s in mgr.list() if s["id"].startswith("t-")]  # stable

    print("ok  sessions: created_ts exposed; list newest-first with a total order")


def test_reattach_keeps_original_created_ts():
    """A restart must not re-date a surviving session. reattach() takes created_ts from the
    registry row (else the log's first event), so an hours-old run doesn't read as "just now"
    and jump to the top of the list."""
    import asyncio
    import json as _j
    from orchestrator.config import Config
    from orchestrator.manager import Session
    from orchestrator.runners import SessionConfig

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "logs").mkdir()
        started = "2026-07-01T09:00:00Z"
        with open(root / "logs" / "s-r.jsonl", "w") as fh:
            fh.write(_j.dumps({"seq": 0, "ts": started, "type": "session_start"}) + "\n")
        cfg = Config(runtime_dir=root, logs_dir=root / "logs", runner="local")
        sess = Session("s-r", cfg, SessionConfig())
        assert sess.created_ts != started              # a fresh Session stamps "now"

        async def fake_reattach():
            return None

        async def no_events():                          # the pump ends immediately
            return
            yield {}                                    # pragma: no cover — makes it a generator

        def stub(s):
            # Stub BOTH: reattach() spawns the event pump, and LocalRunner.events() would hit
            # an uninitialised queue and log an AttributeError from inside the task.
            s.runner.reattach = fake_reattach           # type: ignore[method-assign]
            s.runner.events = no_events                 # type: ignore[method-assign]

        stub(sess)
        asyncio.run(sess.reattach(status="idle", created_ts=started))
        assert sess.created_ts == started

        # No registry row to hand over → fall back to the log's first event, not "now".
        sess2 = Session("s-r", cfg, SessionConfig())
        stub(sess2)
        asyncio.run(sess2.reattach(status="idle"))
        assert sess2.created_ts == started

    print("ok  reattach: keeps the original created_ts (a restart doesn't re-date sessions)")


def test_wait_running_waits_for_worker_container():
    """_wait_running must gate on the WORKER container, not just pod phase.

    Warden is a native sidecar (initContainer + restartPolicy: Always), so the pod reports
    Running as soon as the sidecar starts -- while `worker` may still be waiting. Both exec
    and attach target `worker`, so returning on phase alone raced the synced-memory restore:
    the touch of the sentinel exec'd into a container that did not exist yet, so the worker
    sat on its gate until the 20s timeout ("memory restore timed out").
    """
    from types import SimpleNamespace as NS
    from orchestrator.k8s_runner import K8sRunner

    def pod(phase, worker_state):
        cs = None if worker_state is None else [
            NS(name="worker", state=NS(running=NS(started_at="t") if worker_state == "running" else None))
        ]
        return NS(status=NS(phase=phase, container_statuses=cs))

    # Pod Running because the SIDECAR is up, but worker not created yet -> not ready.
    assert K8sRunner._worker_started(pod("Running", None)) is False
    # Worker present but still waiting (state.running is None) -> not ready.
    assert K8sRunner._worker_started(pod("Running", "waiting")) is False
    # Worker actually running -> ready.
    assert K8sRunner._worker_started(pod("Running", "running")) is True

    # And the loop must not return on phase alone: feed it a sidecar-only pod first,
    # then a ready one, and assert it kept polling instead of returning early.
    seen = {"n": 0}
    def read(_name, _ns):
        seen["n"] += 1
        return pod("Running", None) if seen["n"] == 1 else pod("Running", "running")

    r = K8sRunner.__new__(K8sRunner)
    r._core = NS(read_namespaced_pod=read)
    r.pod_name, r.namespace = "p", "ns"
    r._wait_running(timeout=5)
    assert seen["n"] >= 2, f"returned on pod phase alone after {seen['n']} read(s)"

    print("ok  k8s: _wait_running gates on the worker container, not pod phase")



def test_memory_snapshot_never_clobbers():
    """An empty /memory must never overwrite a snapshot that has content.

    The old guard was `if not blob: return`, but an empty directory still tars to a valid
    ~100-byte archive, so it sailed straight past. Chained with a failed restore (which
    leaves the pod's /memory empty through no fault of the agent), the turn-end snapshot
    would silently destroy everything the agent had remembered.
    """
    import io as _io, tarfile
    from orchestrator.k8s_runner import K8sRunner

    def tar(names):
        buf = _io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            ti = tarfile.TarInfo("./"); ti.type = tarfile.DIRTYPE; t.addfile(ti)
            for n in names:
                ti = tarfile.TarInfo(n); ti.size = 4
                t.addfile(ti, _io.BytesIO(b"data"))
        return buf.getvalue()

    empty, full = tar([]), tar(["./notes.md"])
    assert len(empty) > 0, "an empty /memory still produces a non-empty archive"
    assert not K8sRunner._tar_has_content(empty), "bare ./ is not content"
    assert K8sRunner._tar_has_content(full), "a real file is content"
    # A corrupt archive must count as content: never justify an overwrite with a blob
    # we could not read.
    assert K8sRunner._tar_has_content(b"not a tar at all")

    print("ok  k8s: an empty /memory cannot overwrite a snapshot that has content")



def test_exec_capture_honours_container():
    """_exec_capture must run in the container it was ASKED for.

    It used to accept `container` and then hardcode "warden". That silently pointed every
    /memory operation at the wrong container -- and warden has no /memory mount, yet the
    image still ships an empty /memory directory, so the calls succeeded against the wrong
    filesystem instead of failing loudly:
      - the restore sentinel was touched where the worker could never see it, so every
        synced session burned its full 20s memory gate and reported a false timeout;
      - the turn-end snapshot tarred warden's empty directory, so snapshots were always
        empty archives and no agent memory was ever actually captured.
    """
    import sys as _sys, types
    from orchestrator.k8s_runner import K8sRunner

    seen = {}
    fake = types.ModuleType("kubernetes.stream")
    fake.stream = lambda *a, **kw: seen.update(kw) or "out"
    saved = _sys.modules.get("kubernetes.stream")
    _sys.modules["kubernetes.stream"] = fake
    try:
        r = K8sRunner.__new__(K8sRunner)
        r._core = type("C", (), {"connect_get_namespaced_pod_exec": staticmethod(lambda *a, **k: None)})()
        r.pod_name, r.namespace = "p", "ns"

        assert r._exec_capture(["touch", "/memory/x"], container="worker") == "out"
        assert seen["container"] == "worker", f"asked for worker, ran in {seen['container']!r}"

        r._exec_capture(["ls"], container="warden")
        assert seen["container"] == "warden", seen["container"]
    finally:
        if saved is None: _sys.modules.pop("kubernetes.stream", None)
        else: _sys.modules["kubernetes.stream"] = saved

    print("ok  k8s: _exec_capture runs in the requested container (sentinel + snapshot)")



def test_purge_deletes_memory_snapshots():
    """purge_memory must remove synced snapshots, not just the PVC.

    In memory_mode="synced" the agent's memory is a tarball on the orchestrator's volume, so
    purging only the PVC left the real memory on disk, outliving the agent it belonged to.
    Isolated per-session clones (`<base>-<session>`) count too -- they are the same memory.
    """
    from orchestrator.config import Config
    from orchestrator.k8s_runner import delete_memory_snapshots, snapshot_dir

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = Config(runtime_dir=root, logs_dir=root / "logs")
        d = snapshot_dir(cfg); d.mkdir(parents=True, exist_ok=True)
        (d / "terrarium-mem-agt-aaa.tar.gz").write_bytes(b"memory")
        (d / "terrarium-mem-agt-aaa-20260101-1.tar.gz").write_bytes(b"isolated clone")
        (d / "terrarium-mem-agt-bbb.tar.gz").write_bytes(b"a DIFFERENT agent")

        removed = delete_memory_snapshots(cfg, "terrarium-mem-agt-aaa")
        assert removed == 2, removed
        left = sorted(p.name for p in d.glob("*.tar.gz"))
        assert left == ["terrarium-mem-agt-bbb.tar.gz"], left

        # Purging a scope with no snapshots is a no-op, not an error.
        assert delete_memory_snapshots(cfg, "terrarium-mem-agt-zzz") == 0

    print("ok  purge_memory: synced snapshots (and isolated clones) are deleted too")


def test_memory_mode_worker_gate():
    """The worker must wait for a "synced" restore — but ONLY when a restore is actually coming.

    Gated on TERRA_MEMORY_RESTORE (set solely by the k8s runner, which is the only one that
    restores) rather than on harness.memory_mode: Docker keeps a real volume mount for "synced", so
    gating on config would hang every Docker session for the full timeout waiting on a sentinel
    nobody writes. Regression guard — flipping the default to "synced" did exactly that."""
    import os as _os
    import time as _time
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sandbox"))
    try:
        import worker
    except Exception as e:  # noqa: BLE001 — needs claude_agent_sdk; skip quietly like the rewind tests
        print(f"ok  memory gate (skipped: {e})")
        return

    _os.environ.pop("TERRA_MEMORY_RESTORE", None)
    t0 = _time.monotonic()
    worker._await_memory(timeout=5)
    assert _time.monotonic() - t0 < 0.2, "no restore promised → must not block (docker/volume/none)"

    # restore promised but sentinel never lands → fail open at the deadline, never hang
    _os.environ["TERRA_MEMORY_RESTORE"] = "1"
    try:
        t0 = _time.monotonic()
        worker._await_memory(timeout=0.3)
        assert 0.25 < _time.monotonic() - t0 < 2.0, "should fail open at the timeout"

        # sentinel present → returns immediately
        d = Path(tempfile.mkdtemp())
        sentinel = d / ".terra-memory-restored"
        sentinel.write_text("")
        orig = worker.MEMORY_SENTINEL
        worker.MEMORY_SENTINEL = str(sentinel)
        try:
            t0 = _time.monotonic()
            worker._await_memory(timeout=5)
            assert _time.monotonic() - t0 < 0.2, "sentinel present → no wait"
        finally:
            worker.MEMORY_SENTINEL = orig
    finally:
        _os.environ.pop("TERRA_MEMORY_RESTORE", None)
    print("ok  memory gate: waits only when a restore is promised, fails open, releases on sentinel")


def test_harness_trim_and_agents():
    """The harness knobs that TRIM the default Claude harness: skills as a filter
    ([] = hide everything, incl. the CLI's built-in skills) and programmatic subagents."""
    # skills: all four shapes round-trip; [] stays an explicit empty list (≠ False)
    assert Harness.from_json(Harness(skills=[]).to_json()).skills == []
    assert Harness.from_json(Harness(skills=["code-review"]).to_json()).skills == ["code-review"]
    assert Harness.from_json(Harness(skills="all").to_json()).skills == "all"
    assert Harness().skills == "all"  # default enables built-in skills (deep-research, …) out of the box
    # agents round-trip
    spec = {"triager": {"description": "Triage bugs", "prompt": "You triage.", "max_turns": 3}}
    assert Harness.from_json(Harness(agents=spec).to_json()).agents == spec

    # worker mapping: skills filter + AgentDefinition conversion (snake_case → camelCase);
    # needs claude_agent_sdk — skip quietly if absent (same policy as the worker rewind tests).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sandbox"))
    try:
        import worker
    except Exception as e:  # noqa: BLE001
        print(f"ok  harness: trim knobs round-trip (worker mapping skipped: {e})")
        return
    opts = worker._build_options(Harness(skills=[], agents=spec))
    assert opts.skills == [], "skills=[] must reach the SDK (hides built-in skills)"
    # bare implies no filesystem settings either — otherwise the SDK's skills-discovery
    # default (["user","project"]) would drag settings back into a stripped agent
    assert opts.setting_sources == [], "skills=[] must pin setting_sources empty"
    assert worker._build_options(Harness(skills=[], setting_sources=["project"])).setting_sources == ["project"]
    ad = opts.agents["triager"]
    assert ad.description == "Triage bugs" and ad.prompt == "You triage." and ad.maxTurns == 3
    # bool skills stays the legacy path — never sent as an SDK filter
    assert worker._build_options(Harness(skills=True)).skills is None
    assert worker._build_options(Harness(skills=False)).skills is None
    # builtin_tools remains the availability trim for tools/subagents (e.g. no Task = no subagents)
    assert worker._build_options(Harness(builtin_tools=["Read", "Grep"])).tools == ["Read", "Grep"]
    try:
        worker._build_options(Harness(extra_options={"cwd": "/host"}))
        raise AssertionError("extra_options overrode a Terrarium-managed field")
    except ValueError as exc:
        assert "cwd" in str(exc)
    print("ok  harness: skills filter + programmatic agents reach the SDK options")


def test_tool_catalog_is_single_source():
    """The tool + skill catalog is served, not duplicated.

    It lived in web/components/AgentForm.tsx, and its default set was a verbatim second copy
    of the worker's DEFAULT_BUILTINS — in another language, where a CLI upgrade could put the
    two out of step silently. Same failure the model catalog was centralized to stop.
    """
    import re

    from terracore.toolset import ALL_TOOLS, DEFAULT_BUILTINS, TOOL_PRESETS, catalog

    # the worker's auto-approve default IS the catalog's, not a copy of it
    import sandbox.worker as W
    assert W.DEFAULT_BUILTINS is DEFAULT_BUILTINS

    wire = catalog()
    assert {"groups", "presets", "defaults", "skills"} == set(wire)
    flat = [t for g in wire["groups"] for t in g["tools"]]
    assert len(flat) == len(set(flat)), "a tool is listed in two groups"
    # every preset and every default names a tool the catalog actually offers, or the form
    # would render a selection the availability picker cannot show
    for name, tools in TOOL_PRESETS.items():
        assert set(tools) <= set(ALL_TOOLS), f"preset {name} names an unlisted tool"
    assert set(DEFAULT_BUILTINS) <= set(ALL_TOOLS)

    # the console must not have grown its own copy back
    form = (Path(__file__).resolve().parent.parent / "web" / "components" / "AgentForm.tsx").read_text()
    for gone in ("const TOOL_GROUPS: {", "const KNOWN_SKILLS = [", "const COMMON_TOOLS = ["):
        assert gone not in form, f"AgentForm.tsx re-hardcoded {gone!r} — read useTools() instead"
    assert "useTools()" in form
    # and it must not have re-listed the tools inline either
    assert not re.search(r'"WebFetch"\s*,?\s*\]', form), "AgentForm.tsx inlines a tool list again"
    print("ok  tools: one catalog, served to the console; worker default is the same object")


def test_console_csrf_guard_handles_tls_termination():
    """The console's CSRF guard must compare against the origin the BROWSER used.

    A reverse proxy terminates TLS and forwards over plain HTTP, so Next.js sees
    `http://host` while the browser's Origin header says `https://host`. Comparing those
    directly rejects every mutation on a correctly-configured HTTPS deployment — login
    included — with an error that reads like an attack rather than a proxy artifact.

    Parsed from source: this lives in the console, which has no Python to import, and the
    property is worth pinning because the obvious "simplification" is to drop the forwarded
    headers and compare req.url again.
    """
    src = (Path(__file__).resolve().parent.parent / "web" / "lib" / "orchestrator.ts").read_text()
    guard = src[src.index("function browserOrigin"):src.index("export function crossSiteMutation")]
    assert "x-forwarded-proto" in guard, "the guard ignores the forwarded scheme again"
    assert "x-forwarded-host" in guard, "the guard ignores the forwarded host again"
    # and the unspoofable signal must still be checked
    check = src[src.index("export function crossSiteMutation"):]
    assert 'sec-fetch-site' in check and '"cross-site"' in check, \
        "Sec-Fetch-Site is the one signal page JS cannot forge — it must stay"
    print("ok  console: CSRF guard honours X-Forwarded-* and still checks Sec-Fetch-Site")


def test_harness_surfaces_are_one_schema():
    """The harness field set is declared in four places; assert they stay one schema.

    Harness (the dataclass the worker reads) → HarnessRequest (the API body) →
    UpdateAgentRequest (the agent PATCH) → web/lib/types.ts (what the console can edit).
    The SDK is covered separately by test_sdk_harness_parity_with_api.

    Each drift here has a distinct symptom: a field missing from HarnessRequest is a 422
    on a valid request; missing from the PATCH is silently accepted and discarded (which
    is what happened to `effort`); missing from the TS type is a knob the console can't
    reach at all (which is what had happened to fallback_model, max_thinking_tokens and
    betas).
    """
    import re

    from terracore.harness import HARNESS_FIELDS
    from orchestrator.api import SESSION_SCOPED_HARNESS_FIELDS, HarnessRequest, UpdateAgentRequest

    api = set(HarnessRequest.model_fields)
    assert api == HARNESS_FIELDS, f"API body vs Harness differ: {api ^ HARNESS_FIELDS}"

    patch = set(UpdateAgentRequest.model_fields)
    expected = (api - set(SESSION_SCOPED_HARNESS_FIELDS)) | {"name", "memory_scope"}
    assert patch == expected, f"agent PATCH vs harness differ: {patch ^ expected}"
    # Every patchable field must be optional, or exclude_unset can't distinguish
    # "omitted" from an explicit null (= clear it).
    assert not [n for n, f in UpdateAgentRequest.model_fields.items() if f.is_required()]

    # The console's mirror of the same schema. Parsed from source rather than generated,
    # because the guard has to fail in THIS suite — the console has no Python to import.
    ts = (Path(__file__).resolve().parent.parent / "web" / "lib" / "types.ts").read_text()
    block = re.search(r"export type Harness = \{(.*?)\n\};", ts, re.S)
    assert block, "web/lib/types.ts no longer declares `export type Harness = {...}`"
    ts_fields = set(re.findall(r"^\s{2}(\w+)\??:", block.group(1), re.M))
    # client_tools is session-scoped (handlers run in the SDK client's process), so the
    # console — which drives agents — has nothing to do with it.
    want = HARNESS_FIELDS - set(SESSION_SCOPED_HARNESS_FIELDS)
    assert ts_fields == want, (
        f"web/lib/types.ts Harness is out of sync with the Python harness: "
        f"missing {sorted(want - ts_fields)}, extra {sorted(ts_fields - want)}")
    print("ok  harness: Harness / API body / agent PATCH / console type are one schema")


def test_sdk_harness_parity_with_api():
    """The SDK must let users configure EVERYTHING the API/console accept — no drift. Guards
    against a harness field being added server-side but forgotten in the SDK surface."""
    from orchestrator.api import HarnessRequest
    from terrarium.options import _HARNESS_FIELDS, TerrariumOptions
    api = set(HarnessRequest.model_fields)
    sdk = set(_HARNESS_FIELDS)
    # client_tools is sent via TerrariumOptions(tools=[...]); egress is a deprecated no-op.
    missing = api - sdk - {"client_tools", "egress"}
    assert not missing, f"SDK missing harness fields the API/console accept: {sorted(missing)}"
    # the availability allowlist + the aligned extras serialize through.
    h = TerrariumOptions(builtin_tools=["Read", "Grep"], fallback_model="claude-haiku-4-5",
                         max_thinking_tokens=4000, betas=["x"]).to_harness()
    assert h["builtin_tools"] == ["Read", "Grep"] and h["fallback_model"] == "claude-haiku-4-5"
    assert h["max_thinking_tokens"] == 4000 and h["betas"] == ["x"]
    assert "disallowed_tools" not in TerrariumOptions.__dataclass_fields__, "disallowed_tools removed"

    # Claude-SDK drop-in shapes: AgentDefinition (same field names as claude_agent_sdk's)
    # and the {"type":"preset"} system_prompt both serialize to the harness.
    from terrarium.options import AgentDefinition
    h = TerrariumOptions(
        system_prompt={"type": "preset", "preset": "claude_code"},
        agents={"triager": AgentDefinition(description="Triage", prompt="You triage.", maxTurns=3)},
        skills=[],
    ).to_harness()
    assert h["system_mode"] == "claude_code" and "custom_prompt" not in h
    assert h["agents"] == {"triager": {"description": "Triage", "prompt": "You triage.", "maxTurns": 3}}
    assert h["skills"] == []
    try:
        TerrariumOptions(system_prompt={"type": "preset", "preset": "claude_code", "append": "x"}).to_harness()
        assert False, "preset append must be refused, not silently dropped"
    except ValueError:
        pass
    print("ok  sdk: harness configurability matches the API/console surface (no drift)")


def test_hardened_flags():
    f = " ".join(hardened_flags("net", "runsc"))
    for must in ["--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
                 "--pids-limit=512", "--runtime runsc", "--network net"]:
        assert must in f, must
    assert "--privileged" not in f and "--network=host" not in f and "--pid=host" not in f
    print("ok  hardened_flags: secure, no escape flags")


def test_build_command_harness_blob():
    # api-key mode under mandatory Warden: the whole harness travels as JSON, the real
    # key is absent (only the decoy), and no creds dir is mounted into the sandbox.
    cfg = Config(api_key="sk-test", runtime_dir=Path(tempfile.mkdtemp()))
    r = DockerRunner(session_id="t1", config=cfg, sess=SessionConfig(harness=Harness(model="haiku")))
    r._warden_endpoint = ("172.30.0.5", 8888)
    s = " ".join(r.build_command())
    assert "sk-test" not in s                                 # real key never in the sandbox
    assert "/home/agent/.claude" not in s                     # no creds dir mount
    assert "terrarium-ws-t1:/workspace" in s
    assert "TERRA_HARNESS=" in s and '"model": "haiku"' in s  # whole harness travels as JSON
    print("ok  build_command: harness blob travels; real key absent (decoy only)")


def test_build_command_per_agent_memory():
    spec = AgentSpec(id="agt_xyz", name="X")
    sc = SessionConfig.from_agent(spec)
    cfg = Config(api_key="sk-test", runtime_dir=Path(tempfile.mkdtemp()))
    r = DockerRunner(session_id="s9", config=cfg, sess=sc)
    r._warden_endpoint = ("172.30.0.5", 8888)
    s = " ".join(r.build_command())
    assert "terrarium-mem-agt_xyz:/memory" in s
    assert "terrarium-memory:/memory" not in s
    print("ok  build_command: mounts per-agent memory volume")


def test_filebridge_names():
    for bad in ["../x", "a/b", ".", "..", "x;y", "/etc/passwd"]:
        try:
            filebridge._safe_name(bad)
            raise AssertionError(f"accepted unsafe name {bad!r}")
        except ValueError:
            pass
    assert filebridge._safe_name("good_file.txt") == "good_file.txt"
    print("ok  filebridge: rejects traversal / unsafe names")


def test_image_block_anthropic_to_mcp():
    """issue #5: a client tool's Anthropic image block must convert to the MCP tool-result shape
    ({data, mimeType}) so create_sdk_mcp_server builds valid ImageContent (else the agent errors)."""
    from terracore.tools import _to_mcp_block
    anthropic = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}
    assert _to_mcp_block(anthropic) == {"type": "image", "data": "QUJD", "mimeType": "image/jpeg"}
    assert _to_mcp_block({"type": "text", "text": "hi"}) == {"type": "text", "text": "hi"}  # text untouched
    mcp = {"type": "image", "data": "QUJD", "mimeType": "image/png"}
    assert _to_mcp_block(mcp) == mcp  # already-MCP image passes through
    print("ok  tools: Anthropic image block → MCP tool-result shape (work_item #5)")


def test_agent_store():
    path = Path(tempfile.mkdtemp()) / "agents.json"
    store = AgentStore(path)
    a = store.create(name="Researcher", harness=Harness(model="opus", allowed_tools=["Read"]))
    assert a.id.startswith("agt_") and a.memory_scope == a.id
    assert a.harness.model == "opus" and a.harness.allowed_tools == ["Read"]
    assert a.memory_volume() == f"terrarium-mem-{a.id}"
    b = store.create(name="Teammate", memory_scope="team-x")
    assert b.memory_volume() == "terrarium-mem-team-x"  # shared-memory agent
    upd = store.update(a.id, harness_updates={"model": "sonnet", "thinking": {"type": "adaptive"}})
    assert upd.harness.model == "sonnet" and upd.harness.thinking == {"type": "adaptive"} and upd.version == 2
    # persistence incl. nested harness, across reopen
    store2 = AgentStore(path)
    got = store2.get(a.id)
    assert got.harness.model == "sonnet" and got.harness.allowed_tools == ["Read"]
    assert {x.name for x in store2.list()} == {"Researcher", "Teammate"}
    assert store2.delete(a.id) and store2.get(a.id) is None
    print("ok  agent_store: crud + versioning + persistence (nested harness) + memory scope")


def test_session_from_agent():
    spec = AgentSpec(id="agt_abc", name="X", harness=Harness(model="haiku", system_mode="claude_code"))
    sc = SessionConfig.from_agent(spec, title="t")
    assert sc.agent_id == "agt_abc" and sc.model == "haiku" and sc.system_mode == "claude_code"
    assert sc.memory_volume == "terrarium-mem-agt_abc"
    print("ok  session_from_agent: resolves harness + per-agent memory volume")


def test_agent_bound_session_overlays_client_tools():
    """work_item #1: an agent-bound session must forward request-only client_tools onto the
    worker harness — they can't live on the stored agent (handlers run in the SDK client)."""
    from orchestrator.api import apply_session_overlay, SESSION_SCOPED_HARNESS_FIELDS, CreateSessionRequest
    assert SESSION_SCOPED_HARNESS_FIELDS == ("client_tools",)
    spec = AgentSpec(id="agt_x", name="X", harness=Harness(model="opus", allowed_tools=["Read"]))
    tools = [{"name": "whoami", "description": "who am i", "input_schema": {}}]
    sc = apply_session_overlay(SessionConfig.from_agent(spec), CreateSessionRequest(agent_id="agt_x", client_tools=tools))
    assert sc.harness.client_tools == tools, "client_tools forwarded onto the agent harness"
    assert sc.harness.model == "opus" and sc.harness.allowed_tools == ["Read"], "agent config preserved"
    # a request without client_tools leaves the snapshot untouched (no accidental wipe)
    sc2 = apply_session_overlay(SessionConfig.from_agent(spec), CreateSessionRequest(agent_id="agt_x"))
    assert sc2.harness.client_tools is None
    print("ok  agent_bound_session: overlays session-scoped client_tools (work_item #1)")


def test_k8s_pod_manifest():
    from orchestrator.k8s_runner import build_pod_manifest, build_pvc_manifest, dns_name

    assert dns_name("terrarium-mem-agt_C9X") == "terrarium-mem-agt-c9x"  # DNS-1123

    # api-key mode: Warden is mandatory, so the worker gets only an inert DECOY key —
    # the real key never enters the worker container, and there is no creds mount.
    pod = build_pod_manifest(
        name="terrarium-session-abc", image="img:1",
        harness_json=Harness(model="haiku").to_json(), memory_pvc="terrarium-mem-x", api_key="sk-real",
        warden_cred_secret="wcred", warden_policy_cm="wpol",
    )
    spec = pod["spec"]
    c = spec["containers"][0]
    # hardening mirrors the docker flags
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert spec["securityContext"]["fsGroup"] == 1001              # volumes writable by agent
    assert "CHOWN" in c["securityContext"]["capabilities"]["add"]  # entrypoint owns the creds
    assert spec["automountServiceAccountToken"] is False           # sandbox gets NO k8s token
    assert spec["dnsConfig"]["nameservers"] == ["1.1.1.1", "1.0.0.1"]  # bypass cluster DNS
    assert spec["restartPolicy"] == "Never"
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["workspace"]["emptyDir"]["sizeLimit"] == "2Gi"
    assert volumes["warden-audit"]["emptyDir"]["sizeLimit"] == "256Mi"
    assert c["resources"]["limits"]["ephemeral-storage"] == "4Gi"
    envs = {e["name"]: e.get("value") for e in c["env"]}
    assert "sk-real" not in json.dumps(pod)                        # real key NEVER in the manifest
    assert envs["ANTHROPIC_API_KEY"].startswith("sk-ant-api03-")   # only the inert decoy
    assert all(m["name"] != "creds" for m in c["volumeMounts"])     # never a worker-side creds mount
    assert any(v.get("persistentVolumeClaim", {}).get("claimName") == "terrarium-mem-x" for v in spec["volumes"])

    # subscription mode: OAuth-shaped decoy stub, no api-key, no worker creds mount.
    pod2 = build_pod_manifest(name="n", image="i", harness_json="{}", memory_pvc="m",
                              api_key=None, warden_cred_secret="wcred")
    c2 = pod2["spec"]["containers"][0]
    assert all(m["name"] != "creds" for m in c2["volumeMounts"])
    e2 = {e["name"] for e in c2["env"]}
    assert "TERRA_DECOY_OAUTH" in e2 and "ANTHROPIC_API_KEY" not in e2

    pvc = build_pvc_manifest("terrarium-mem-x", "2Gi", "longhorn")
    assert pvc["spec"]["resources"]["requests"]["storage"] == "2Gi"
    assert pvc["spec"]["storageClassName"] == "longhorn"
    print("ok  k8s: pod/pvc manifest (hardening · no sa-token · real cred never in worker · decoy only)")


def test_credential_manager():
    import asyncio
    from orchestrator.credentials import CredentialManager, _extract_oauth, _now_ms

    assert _extract_oauth({"claudeAiOauth": {"accessToken": "a"}}) == {"accessToken": "a"}
    assert _extract_oauth({"accessToken": "b"}) == {"accessToken": "b"}
    assert _extract_oauth({"nope": 1}) is None

    d = Path(tempfile.mkdtemp())
    seed, store = d / "seed.json", d / "credentials.json"
    seed.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "fresh", "refreshToken": "rt-1", "expiresAt": _now_ms() + 3600_000}}))
    mgr = CredentialManager(seed_path=seed, store_path=store)

    calls = {"n": 0}
    mgr._refresh_call = lambda rt: (calls.__setitem__("n", calls["n"] + 1)
                                    or {"access_token": "new-access", "refresh_token": "rt-2", "expires_in": 3600})

    assert asyncio.run(mgr.ensure_fresh()) is True
    assert calls["n"] == 0 and store.exists()                    # fresh → no refresh, but seeded
    assert json.loads(store.read_text())["claudeAiOauth"]["accessToken"] == "fresh"

    assert asyncio.run(mgr.ensure_fresh(force=True)) is True      # force → refresh + rotate
    saved = json.loads(store.read_text())["claudeAiOauth"]
    assert calls["n"] == 1 and saved["accessToken"] == "new-access" and saved["refreshToken"] == "rt-2"
    assert saved["expiresAt"] > _now_ms()

    # set via API + status (no token leaked in status)
    st = asyncio.run(mgr.set_credentials({"claudeAiOauth": {
        "accessToken": "x", "refreshToken": "y", "expiresAt": _now_ms() + 7200_000, "subscriptionType": "pro"}}))
    assert st["present"] and st["valid"] and st["subscription_type"] == "pro" and st["expires_in_s"] > 3600
    assert "accessToken" not in st and "refreshToken" not in st
    try:
        asyncio.run(mgr.set_credentials({"nope": 1})); raise AssertionError("accepted invalid creds")
    except ValueError:
        pass
    asyncio.run(mgr.clear())
    assert mgr.status()["present"] is False
    assert mgr.current_creds() is None  # mounted seed cannot undo explicit revocation
    assert asyncio.run(mgr.set_credentials({"claudeAiOauth": {"accessToken":"x","refreshToken":"y","expiresAt":_now_ms()+7200_000}}))["present"]
    print("ok  credentials: seed · refresh/rotate · set-via-api · status (no token leak) · clear")


def test_credential_manager_encrypted():
    import asyncio
    from orchestrator.credentials import CredentialManager, _now_ms

    d = Path(tempfile.mkdtemp())
    store = d / "credentials.json"
    # a pre-existing PLAINTEXT store (from a prior deploy) must be migrated + removed
    store.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "PLAINTEXT-TOKEN", "refreshToken": "rt", "expiresAt": _now_ms() + 3600_000}}))
    mgr = CredentialManager(seed_path=None, store_path=store, kek="unit-test-kek")
    sealed = d / "credentials.sealed.json"
    assert sealed.exists() and not store.exists()                      # migrated → cleartext gone
    assert "PLAINTEXT-TOKEN" not in sealed.read_text()                 # at rest = ciphertext only
    assert mgr.current_creds()["claudeAiOauth"]["accessToken"] == "PLAINTEXT-TOKEN"  # decrypts in RAM

    # right KEK reads it back; wrong KEK fails closed (no plaintext recoverable)
    assert CredentialManager(seed_path=None, store_path=store, kek="unit-test-kek").current_creds() is not None
    assert CredentialManager(seed_path=None, store_path=store, kek="WRONG").current_creds() is None

    # set + rotate stay sealed at rest
    asyncio.run(mgr.set_credentials({"claudeAiOauth": {
        "accessToken": "ROTATED", "refreshToken": "r2", "expiresAt": _now_ms() + 7200_000}}))
    assert "ROTATED" not in sealed.read_text()
    assert mgr.current_creds()["claudeAiOauth"]["accessToken"] == "ROTATED"
    asyncio.run(mgr.clear())
    assert mgr.current_creds() is None
    print("ok  credentials: sealed at rest (KEK) · plaintext migrated+purged · decrypt-in-RAM · wrong-KEK fails closed")


def test_effective_kek_fallback():
    from orchestrator.config import Config

    assert Config(warden_kek="explicit", auth_token="tok").effective_kek == "explicit"  # explicit wins
    assert Config(warden_kek=None, auth_token="tok").effective_kek == "tok"              # inherits token
    assert Config(warden_kek=None, auth_token=None).effective_kek is None                # neither → plaintext
    print("ok  kek: effective_kek = TERRA_KEK else TERRA_TOKEN (encrypt-by-default, no separate key)")


def test_credential_refresh_backoff():
    from orchestrator.credentials import CredentialManager

    m = CredentialManager(seed_path=None, store_path=Path(tempfile.mkdtemp()) / "c.json")
    assert m._next_retry_at == 0 and m._fail_count == 0
    m._note_refresh_failure()
    t1 = m._next_retry_at
    assert t1 > 0 and m._fail_count == 1
    m._note_refresh_failure()
    assert m._next_retry_at >= t1 and m._fail_count == 2   # backoff grows — don't hammer a 429
    print("ok  creds: refresh backoff grows on failure (stops hammering a rate-limited token)")


def test_managed_creds_provider():
    from orchestrator.config import Config, managed_creds

    d = Path(tempfile.mkdtemp())
    f = d / "creds.json"
    f.write_text(json.dumps({"claudeAiOauth": {"accessToken": "FROM-FILE"}}))
    cfg = Config(runtime_dir=d, creds_path=f)
    assert managed_creds(cfg)["claudeAiOauth"]["accessToken"] == "FROM-FILE"     # no provider → file
    cfg.creds_provider = lambda: {"claudeAiOauth": {"accessToken": "FROM-RAM"}}
    assert managed_creds(cfg)["claudeAiOauth"]["accessToken"] == "FROM-RAM"      # provider preferred
    print("ok  managed_creds: in-memory provider preferred; on-disk fallback when unmanaged")


def test_warden_mandatory():
    """There is no opt-out flag to test any more — mediation is structural. Assert the
    structure instead: no config knob can produce an unmediated sandbox, and the manifest
    builder cannot emit a Pod without the sidecar."""
    import inspect

    from orchestrator.config import Config
    from orchestrator.k8s_runner import build_pod_manifest

    assert not hasattr(Config(), "warden"), "a warden on/off flag is back — mediation must be structural"
    assert "warden" not in inspect.signature(build_pod_manifest).parameters, \
        "build_pod_manifest must not be able to build a Pod without the Warden sidecar"
    assert any(c["name"] == "warden" for c in build_pod_manifest(
        name="n", image="i", harness_json="{}", memory_pvc="m")["spec"]["initContainers"])

    # build_command always routes the agent through Warden with a DECOY credential —
    # the real api key never reaches the sandbox env. (The sidecar sets the endpoint in
    # start(); simulate it so build_command takes its normal mediated path.)
    cfg = Config(api_key="sk-real-secret", runtime_dir=Path(tempfile.mkdtemp()))
    r = DockerRunner(session_id="esc", config=cfg, sess=SessionConfig())
    r._warden_endpoint = ("10.244.0.7", 8888)
    s = " ".join(r.build_command())
    assert "sk-real-secret" not in s                       # real cred NEVER in the sandbox
    assert "ANTHROPIC_API_KEY=sk-ant-api03-" in s          # only the inert decoy
    assert "HTTPS_PROXY=http://10.244.0.7:8888" in s       # forced through Warden
    print("ok  warden: mandatory for every session; real cred never enters the sandbox")


def test_docker_per_session_audit():
    """Each Docker session gets its OWN audit file (independent, individually-verifiable
    HMAC chains) instead of one shared file that interleaves concurrent sessions; and it
    lives under the durable egress_dir, so it survives stop() for post-mortem verify."""
    import asyncio
    from orchestrator.warden import WardenController

    cfg = Config(runtime_dir=Path(tempfile.mkdtemp()))
    w_a, w_b = WardenController(cfg, "ses_a", None), WardenController(cfg, "ses_b", None)
    assert w_a.audit_file == cfg.egress_dir / "audit" / "ses_a.jsonl"
    assert w_a.audit_file != w_b.audit_file                       # not one shared file
    # and the audit dir is NOT under the session dir that stop() reaps → it persists
    assert (cfg.runtime_dir / "sessions" / "ses_a") not in w_a.audit_file.parents

    (cfg.egress_dir / "audit").mkdir(parents=True, exist_ok=True)
    w_a.audit_file.write_text('{"kind":"egress","decision":"allow","host":"a.example"}\n')
    w_b.audit_file.write_text('{"kind":"egress","decision":"deny","host":"b.example"}\n')
    r = DockerRunner(session_id="ses_a", config=cfg, sess=SessionConfig())
    rows = asyncio.run(r.read_egress_audit(10))
    assert [x["host"] for x in rows] == ["a.example"]            # only THIS session's chain
    print("ok  docker: per-session audit file (isolated chains; persists past stop)")


def test_docker_warden_state_is_hot_reloadable_after_reattach():
    import asyncio
    from orchestrator.warden import WardenController

    cfg = Config(runtime_dir=Path(tempfile.mkdtemp()))
    ctl = WardenController(cfg, "ses_hot", None)

    # update_* now pushes into the running container with `docker cp`, which bumps mtime and
    # preserves the inode — so Warden's mtime hot-reload still fires, and there is no plaintext
    # credential left on the orchestrator's disk between updates.
    sent: list[tuple[str, str]] = []

    async def fake_docker(*args):
        if args[0] == "cp":
            sent.append((Path(args[1]).name, args[2].split(":", 1)[1]))
        return 0, ""

    ctl._docker = fake_docker
    asyncio.run(ctl.update_cred(None))
    asyncio.run(ctl.update_secrets(None))
    asyncio.run(ctl.update_policy('{"mode":"enforce","rules":[]}'))
    assert sent == [("cred.json", ctl.C_CRED),
                    ("secrets.json", ctl.C_SECRETS),
                    ("policy.json", ctl.C_POLICY)]
    # the staging dir is gone, so no plaintext credential survives the call
    assert not list(Path(tempfile.gettempdir()).glob("wstage-*"))

    runner = DockerRunner(session_id="ses_hot", config=cfg, sess=SessionConfig())

    async def no_attach():
        return None

    runner._attach = no_attach
    asyncio.run(runner.reattach())
    assert runner._warden is not None
    assert runner._warden.C_CRED == ctl.C_CRED
    assert runner._warden.audit_file == ctl.audit_file   # drain target survives reattach
    print("ok  docker: Warden state hot-reloads via cp and survives reattach")


def test_k8s_audit_drain_persists_chain():
    """The k8s Warden audit is an emptyDir inside its own container, so it used to die with
    the Pod: verify-egress 409'd ("audit not retained") for exactly the finished sessions
    worth reviewing, and every console poll paid a pod-exec per session. The drain mirrors
    it byte-exactly onto the orchestrator's volume, INCREMENTALLY, so the HMAC chain stays
    verifiable and re-draining never duplicates or drops a line."""
    import asyncio
    from orchestrator.egress import session_audit_path
    from orchestrator.k8s_runner import AUDIT_IN_POD, K8sRunner

    cfg = Config(runtime_dir=Path(tempfile.mkdtemp()))
    r = K8sRunner(session_id="ses_k", config=cfg, sess=SessionConfig())
    r._core = object()  # non-None so drain_audit() doesn't bail before the exec

    # Stand in for the pod: serve `stat -c %s` and `tail -c +N | base64` off a local buffer,
    # exactly as the real exec channel does (base64 so raw bytes survive the WS text decode).
    pod_file = {"data": b""}

    def fake_exec(command, container="warden"):
        assert container == "warden"                 # NEVER the worker: the agent must not see it
        if command[0] == "stat":
            return f"{len(pod_file['data'])}\n"
        assert command[0] == "sh" and AUDIT_IN_POD in command[2]
        start = int(command[2].split("tail -c +")[1].split()[0]) - 1
        return base64.b64encode(pod_file["data"][start:]).decode()

    r._exec_capture = fake_exec
    path = session_audit_path(cfg, "ses_k")

    line1 = b'{"kind":"egress","seq":0,"decision":"allow","host":"a.example","receipt":"aa"}\n'
    line2 = b'{"kind":"egress","seq":1,"decision":"deny","host":"b.example","receipt":"bb"}\n'

    pod_file["data"] = line1
    asyncio.run(r.drain_audit())
    assert path.read_bytes() == line1

    # A second drain with nothing new must be a no-op (not a duplicate append).
    asyncio.run(r.drain_audit())
    assert path.read_bytes() == line1

    # Only the DELTA is appended, so the chain reads back in order, exactly once.
    pod_file["data"] = line1 + line2
    asyncio.run(r.drain_audit())
    assert path.read_bytes() == line1 + line2
    rows = asyncio.run(r.read_egress_audit(10))       # inherited: reads the drained file
    assert [x["host"] for x in rows] == ["a.example", "b.example"]
    assert [x["seq"] for x in rows] == [0, 1]         # seq contiguous → verify_chain sees no gap

    # A truncated/replaced file is corruption, not a rotation boundary. Keep the
    # offset so later bytes cannot be appended as an unanchored suffix.
    pod_file["data"] = b""
    asyncio.run(r.drain_audit())
    assert path.read_bytes() == line1 + line2
    assert r._audit_offset == len(line1 + line2)

    # A reattached session (orchestrator restarted mid-session) seeds its offset from the
    # file, so it resumes where the drain left off rather than duplicating the whole chain.
    r2 = K8sRunner(session_id="ses_k", config=cfg, sess=SessionConfig())
    assert r2._audit_offset == len(line1 + line2)
    print("ok  k8s: audit drained byte-exactly to the orchestrator (chain outlives the Pod)")


def test_audit_reads_are_runner_independent():
    """Every audit reader resolves the SAME per-session path, on every runner — the k8s
    exec-fanout/409 split is gone. Asserted structurally (not by grepping prose): each
    runner must inherit the base read, and only the k8s runner may override the drain."""
    import asyncio
    from orchestrator.egress import session_audit_path
    from orchestrator.k8s_runner import K8sRunner
    from orchestrator.runners import DockerRunner, LocalRunner, Runner

    cfg = Config(runtime_dir=Path(tempfile.mkdtemp()))
    assert session_audit_path(cfg, "s1") == cfg.egress_dir / "audit" / "s1.jsonl"
    # Reading is owned by the base class; draining is the ONLY runner-specific half.
    assert "read_egress_audit" in Runner.__dict__ and "drain_audit" in Runner.__dict__
    for cls in (DockerRunner, LocalRunner, K8sRunner):
        assert "read_egress_audit" not in cls.__dict__, f"{cls.__name__} must inherit the audit read"
    # Both containerised runners now drain: neither Warden writes anywhere the orchestrator
    # can read directly. Docker used to be exempt only because it shared a bind mount, which
    # is the coupling that broke every named-volume deployment.
    assert "drain_audit" in K8sRunner.__dict__
    assert "drain_audit" in DockerRunner.__dict__
    assert "drain_audit" not in LocalRunner.__dict__    # runs in-process; the path IS ours

    # All three runners read the same file for the same session id, live or terminated.
    (cfg.egress_dir / "audit").mkdir(parents=True, exist_ok=True)
    session_audit_path(cfg, "s1").write_text('{"kind":"egress","decision":"allow","host":"x.example"}\n')
    for cls in (DockerRunner, LocalRunner, K8sRunner):
        r = cls(session_id="s1", config=cfg, sess=SessionConfig())
        assert [x["host"] for x in asyncio.run(r.read_egress_audit(10))] == ["x.example"]
    print("ok  egress audit reads are runner-independent (no k8s exec fanout)")


def test_k8s_warden_gates_worker_creds():
    import inspect
    from orchestrator.k8s_runner import K8sRunner, build_pod_manifest

    # subscription (no api_key): the real cred goes ONLY to the warden-cred Secret.
    cfg = Config(runtime_dir=Path(tempfile.mkdtemp()))
    r = K8sRunner(session_id="kw", config=cfg, sess=SessionConfig())
    assert r.warden_cred_secret and r.warden_policy_cm  # real cred goes only to Warden
    # The worker-side creds-Secret path was removed entirely — cred-out-of-sandbox is
    # structural, not conditional: build_pod_manifest cannot mount a worker creds Secret.
    assert "creds_secret" not in inspect.signature(build_pod_manifest).parameters
    # even with an api key, the worker only ever gets a DECOY, never the real cred.
    r2 = K8sRunner(session_id="kw2", config=Config(api_key="sk-x", runtime_dir=cfg.runtime_dir),
                   sess=SessionConfig())
    assert r2.warden_cred_secret
    print("ok  k8s: the real credential only ever reaches Warden, never the worker")


def test_k8s_orphan_reaps_warden_creds() -> None:
    """The default hardened mode stores the REAL token in the warden-cred Secret
    (not terrarium-creds). Verify both stop() and the restart orphan-reaper delete
    warden Secrets + policy CMs, while keeping resources for reattached sids."""
    import sys
    import types
    from orchestrator.k8s_runner import cleanup_orphans, dns_name, K8sRunner

    def _meta(name):
        return types.SimpleNamespace(metadata=types.SimpleNamespace(name=name))

    deleted = {"secrets": [], "cms": [], "pvcs": []}

    def _pvc(name, labels):
        return types.SimpleNamespace(metadata=types.SimpleNamespace(name=name, labels=labels))

    class _Core:
        def list_namespaced_pod(self, ns, label_selector=None):
            return types.SimpleNamespace(items=[])

        def list_namespaced_secret(self, ns, label_selector=None):
            if "warden" in (label_selector or ""):
                return types.SimpleNamespace(
                    items=[_meta(dns_name("warden-cred-keep")), _meta(dns_name("warden-cred-orphan"))])
            return types.SimpleNamespace(items=[])

        def list_namespaced_config_map(self, ns, label_selector=None):
            return types.SimpleNamespace(
                items=[_meta(dns_name("warden-policy-keep")), _meta(dns_name("warden-policy-orphan"))])

        def list_namespaced_persistent_volume_claim(self, ns, label_selector=None):
            # Only labeled ISOLATED clones are ever listed (the selector filters);
            # base volumes never carry the label, so they never appear here.
            assert "terrarium-isolated=true" in (label_selector or "")
            return types.SimpleNamespace(items=[
                _pvc("terrarium-mem-agt-x-731", {"terrarium-isolated": "true", "terrarium-session": dns_name("keep")}),
                _pvc("terrarium-mem-agt-x-942", {"terrarium-isolated": "true", "terrarium-session": dns_name("dead")}),
            ])

        def delete_namespaced_pod(self, name, ns, **kw):
            pass

        def delete_namespaced_secret(self, name, ns, **kw):
            deleted["secrets"].append(name)

        def delete_namespaced_config_map(self, name, ns, **kw):
            deleted["cms"].append(name)

        def delete_namespaced_persistent_volume_claim(self, name, ns, **kw):
            deleted["pvcs"].append(name)

    fake = types.ModuleType("kubernetes")
    fake.client = types.SimpleNamespace(
        CoreV1Api=lambda: _Core(),
        ApiException=type("ApiException", (Exception,), {}),
    )

    def _boom():
        raise Exception("not in cluster")

    fake.config = types.SimpleNamespace(load_incluster_config=_boom, load_kube_config=lambda: None)

    saved = sys.modules.get("kubernetes")
    sys.modules["kubernetes"] = fake
    try:
        cleanup_orphans(Config(runtime_dir=Path(tempfile.mkdtemp())), keep_sids={"keep"})
        assert dns_name("warden-cred-orphan") in deleted["secrets"]
        assert dns_name("warden-cred-keep") not in deleted["secrets"]
        assert dns_name("warden-policy-orphan") in deleted["cms"]
        assert dns_name("warden-policy-keep") not in deleted["cms"]
        # isolated memory clones: dead session's reaped, reattached session's kept
        assert deleted["pvcs"] == ["terrarium-mem-agt-x-942"]

        # stop() path: _delete_warden_resources removes this session's pair
        deleted["secrets"].clear(); deleted["cms"].clear()
        r = K8sRunner(session_id="z", config=Config(runtime_dir=Path(tempfile.mkdtemp())),
                      sess=SessionConfig())
        r._core = _Core()
        r._delete_warden_resources()
        assert r.warden_cred_secret in deleted["secrets"] and r.warden_policy_cm in deleted["cms"]

        # stop() path: an ISOLATED session's memory clone is deleted; a base volume never is
        deleted["pvcs"].clear()
        from dataclasses import replace as _rep
        iso = K8sRunner(session_id="z-9", config=Config(runtime_dir=Path(tempfile.mkdtemp())),
                        sess=_rep(SessionConfig(), memory_volume="terrarium-mem-agt-x-9", memory_isolated=True))
        iso._core = _Core()
        iso._delete_isolated_pvc()
        assert deleted["pvcs"] == [dns_name("terrarium-mem-agt-x-9")]
        deleted["pvcs"].clear()
        r._delete_isolated_pvc()  # not isolated → no delete
        assert deleted["pvcs"] == []
        # isolated clones are labeled for the reaper at create time
        from orchestrator.k8s_runner import build_pvc_manifest
        m = build_pvc_manifest("terrarium-mem-agt-x-9", "1Gi", None,
                               labels={"terrarium-isolated": "true", "terrarium-base": "terrarium-mem-agt-x"})
        assert m["metadata"]["labels"]["terrarium-isolated"] == "true"
        assert m["metadata"]["labels"]["app"] == "terrarium-memory"
    finally:
        if saved is not None:
            sys.modules["kubernetes"] = saved
        else:
            sys.modules.pop("kubernetes", None)
    print("ok  k8s: orphan reaper + stop() delete warden cred Secrets + policy CMs (keep reattached sids)")


def test_sse_backlog_bounded() -> None:
    """A stalled SSE subscriber must not buffer the whole session in RAM: on
    overflow its backlog is dropped and replaced with a resync sentinel."""
    import asyncio
    from orchestrator.manager import Session, _STREAM_OVERFLOW

    cfg = Config(runtime_dir=Path(tempfile.mkdtemp()), logs_dir=Path(tempfile.mkdtemp()))
    s = Session("sQ", cfg, SessionConfig(harness=Harness(model="opus")), None)
    q: asyncio.Queue = asyncio.Queue(maxsize=3)
    s._subs.add(q)
    for i in range(50):  # far more than maxsize, never drained
        s._broadcast({"type": "x", "seq": i})
    assert q.qsize() <= 3, "queue must stay bounded"
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    # the consumer reads FIFO and returns on the first sentinel → resync from the log
    assert _STREAM_OVERFLOW in drained, "overflow must hand the consumer a resync sentinel"
    assert drained.index(_STREAM_OVERFLOW) == 0, "sentinel is consumed before any post-overflow event"
    print("ok  sse: a stalled subscriber is bounded + resync'd (no orchestrator OOM)")


def test_read_only_stream_synthesizes_session_end() -> None:
    """F10: replaying a terminated session whose log lacks session_end must append a
    synthetic terminal event so clients stop reconnecting; a clean log is untouched."""
    import asyncio

    from orchestrator.manager import SessionManager

    rt, logs = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    cfg = Config(runtime_dir=rt, logs_dir=logs)
    mgr = SessionManager(cfg)

    async def collect(sid, replay_limit=None):
        return [ev async for ev in mgr.read_only_stream(sid, replay_limit=replay_limit)]

    # (a) log WITHOUT session_end → one synthetic appended
    st = EventStore(logs / "noend.jsonl")
    st.append("session_start"); st.record({"type": "assistant_text", "text": "hi"})
    out = asyncio.run(collect("noend"))
    assert out[-1]["type"] == "session_end" and out[-1].get("synthetic") is True
    assert out[-1]["seq"] == out[-2]["seq"] + 1, "synthetic seq must advance past the last real event"
    assert sum(e["type"] == "session_end" for e in out) == 1

    # (b) log WITH a real session_end → NOT duplicated
    st2 = EventStore(logs / "clean.jsonl")
    st2.append("session_start"); st2.append("session_end")
    out2 = asyncio.run(collect("clean"))
    assert sum(e["type"] == "session_end" for e in out2) == 1 and out2[-1].get("synthetic") is None

    # (c) console-style bounded replay avoids loading genesis and explicitly
    # signals that older history is available through the export endpoint.
    st3 = EventStore(logs / "long.jsonl")
    st3.append("session_start")
    for i in range(8):
        st3.record({"type": "assistant_text", "text": str(i)})
    st3.append("session_end")
    out3 = asyncio.run(collect("long", replay_limit=3))
    assert out3[0]["type"] == "_history_truncated"
    assert [e["seq"] for e in out3[1:]] == [7, 8, 9]
    assert out3[-1]["type"] == "session_end"
    mgr.registry.close()
    print("ok  sse: read_only_stream emits a synthetic session_end only when the log lacks one (F10)")


def test_worker_event_validation() -> None:
    from terracore.protocol import validate_worker_event, MAX_EVENT_BYTES

    # legit event passes through unchanged
    ok = validate_worker_event({"type": "assistant_text", "text": "hi"})
    assert ok == {"type": "assistant_text", "text": "hi"}
    # orchestrator-only types the worker must not forge are stripped of agency AND
    # flagged distinctly (a deliberate control-plane forgery, not benign junk)
    for forged in ("session_end", "budget_exceeded", "session_start"):
        q = validate_worker_event({"type": forged, "cost_usd": 0})
        assert q["type"] == "system" and q["subtype"] == "forged_control_event"
        assert q["claimed_type"] == forged
    # unknown (non-control) type → quarantined
    assert validate_worker_event({"type": "totally_made_up"})["subtype"] == "quarantined_event"
    # negative / NaN / bad cost is clamped ≥ 0; non-dict usage dropped
    r = validate_worker_event({"type": "result", "total_cost_usd": -5, "usage": "bad"})
    assert r["total_cost_usd"] == 0.0 and r["usage"] == {}
    assert validate_worker_event({"type": "result", "total_cost_usd": float("nan")})["total_cost_usd"] == 0.0
    # non-dict input never raises
    assert validate_worker_event("evil")["subtype"] == "malformed_event"
    # oversized payload is bounded, not persisted verbatim
    big = validate_worker_event({"type": "tool_result", "data": "x" * (MAX_EVENT_BYTES + 1000)})
    assert big["subtype"] == "oversized_event" and "data" not in big
    print("ok  protocol: worker events sanitized (quarantine forged types · clamp cost · bound size)")


def test_fold_cost_seeds_reattach() -> None:
    from orchestrator.manager import fold_cost

    # one segment, monotonically rising → seg=latest, banked=0
    assert fold_cost([{"type": "result", "total_cost_usd": c} for c in (0.1, 0.3, 0.5)]) == (0.0, 0.5)
    # a rewind drop banks the prior segment; total survives a restart
    evs = [{"type": "result", "total_cost_usd": c} for c in (0.1, 0.3, 0.05, 0.2)]
    banked, seg = fold_cost(evs)
    assert abs((banked + seg) - 0.5) < 1e-9 and banked == 0.3 and seg == 0.2
    # non-result events ignored
    assert fold_cost([{"type": "user"}, {"type": "assistant_text"}]) == (0.0, 0.0)
    print("ok  cost: fold_cost reconstructs banked+seg from the log (restart-durable budget)")


def test_conceal_env_scrubs_proc_environ():
    """F6: the worker re-exec must remove TERRA_*/WARDEN_* from its own
    /proc/<pid>/environ (a same-uid agent can read it), WHILE keeping the values
    usable via os.environ and leaving the functional proxy var in place."""
    import os
    import subprocess
    import tempfile

    repo = str(Path(__file__).resolve().parent.parent)
    driver = Path(tempfile.mkdtemp()) / "seal_driver.py"
    driver.write_text(
        "import sys; sys.path.insert(0, %r)\n" % repo
        + "from terracore.conceal import conceal_env\n"
        + "conceal_env()\n"
        + "import os\n"
        + "blob = open(f'/proc/{os.getpid()}/environ','rb').read()\n"
        + "print('LEAK', (b'SEAL_TEST_SECRET' in blob) or (b'WARDEN_UID' in blob))\n"
        + "print('USABLE', os.environ.get('TERRA_HARNESS'))\n"
        + "print('PROXY', os.environ.get('HTTPS_PROXY'))\n"
    )
    env = {**os.environ, "TERRA_HARNESS": '{"secret":"SEAL_TEST_SECRET"}',
           "WARDEN_UID": "1002", "HTTPS_PROXY": "http://127.0.0.1:8888"}
    # This suite sets TERRA_NO_RESEAL so importing the worker doesn't re-exec the runner;
    # the re-exec is precisely what THIS test asserts, so drop it for the child.
    env.pop("TERRA_NO_RESEAL", None)
    out = subprocess.run([sys.executable, str(driver)], env=env, capture_output=True, text=True, timeout=30)
    lines = dict(ln.split(" ", 1) for ln in out.stdout.splitlines() if " " in ln)
    assert lines.get("LEAK") == "False", f"tells still in /proc/environ: {out.stdout}\n{out.stderr}"
    assert "SEAL_TEST_SECRET" in (lines.get("USABLE") or ""), "harness not recovered for the worker"
    assert lines.get("PROXY") == "http://127.0.0.1:8888", "functional proxy var must be kept (corp-proxy frame)"
    print("ok  conceal: re-exec scrubs TERRA_/WARDEN_ from /proc/environ, keeps harness usable (F6)")


def test_ca_bundle_is_combined_not_single_cert():
    """F8/F9: the trust store handed to OpenSSL/curl/git is real-roots + the
    session proxy CA (a realistic corp-TLS-inspection store), NOT a lone
    self-signed cert; Node keeps appending only the session CA."""
    import os
    import tempfile

    import terracore.conceal as cz

    tmp = Path(tempfile.mkdtemp())
    sysca = tmp / "system.crt"
    sysca.write_text("-----BEGIN CERTIFICATE-----\nSYSTEMROOT\n-----END CERTIFICATE-----\n")
    sessca = tmp / "session-ca.pem"
    sessca.write_text("-----BEGIN CERTIFICATE-----\nSESSIONCA\n-----END CERTIFICATE-----\n")
    bundle = tmp / "bundle.pem"

    saved_env = {k: os.environ.get(k) for k in
                 ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO")}
    saved_mod = (cz.SYSTEM_CA_CANDIDATES, cz.CA_BUNDLE_PATH)
    try:
        cz.SYSTEM_CA_CANDIDATES = (str(sysca),)
        cz.CA_BUNDLE_PATH = str(bundle)
        for v in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
            os.environ.pop(v, None)
        os.environ["NODE_EXTRA_CA_CERTS"] = str(sessca)

        out = cz.prepare_ca_bundle(wait_s=1.0)
        assert out == str(bundle)
        text = bundle.read_text()
        assert "SYSTEMROOT" in text and "SESSIONCA" in text, "bundle must contain BOTH real roots and the proxy CA"
        for v in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
            assert os.environ[v] == str(bundle), f"{v} must point at the combined bundle (fixes curl/git too)"
        assert os.environ["NODE_EXTRA_CA_CERTS"] == str(sessca), "Node appends; keep it pointed at the session CA only"

        # no-MITM (api-key/direct): nothing set → no bundle, system store untouched
        for v in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
            os.environ.pop(v, None)
        assert cz.prepare_ca_bundle(wait_s=0.1) is None
    finally:
        cz.SYSTEM_CA_CANDIDATES, cz.CA_BUNDLE_PATH = saved_mod
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok  conceal: trust store is real-roots + proxy CA (no single-cert tell; curl/git/pip TLS works) (F8/F9)")


def test_egress_receipt_verifier():
    """F2: the persisted per-session key + Python verifier recompute Warden's HMAC
    chain (golden vector pins cross-language match with warden/src/audit.rs) and
    catch tampering / dropped lines."""
    from orchestrator.receipts import (canonical, load_receipt_key, persist_receipt_key,
                                        sign, verify_chain, verify_file)

    # cross-language golden vector — MUST equal the Rust test's expected hex
    rec0 = {"seq": 0, "decision": "deny", "host": "evil.example", "kind": "egress",
            "ts": "2026-06-23T00:00:00.000Z"}
    assert canonical(rec0) == '{"decision":"deny","host":"evil.example","kind":"egress","seq":0,"ts":"2026-06-23T00:00:00.000Z"}'
    assert sign("abc123", "", canonical(rec0)) == "52287111acb3b1f2c76bcdb6a7cb6b5d1470d418b8c5a1a30d59dd880cd9b08e"

    # build a valid 3-line chain and verify it
    key = "deadbeef"
    lines, prev = [], ""
    for i in range(3):
        rec = {"seq": i, "decision": "allow", "host": f"h{i}.example", "kind": "egress"}
        r = sign(key, prev, canonical(rec))
        rec["receipt"] = r
        lines.append(rec)
        prev = r
    assert verify_chain(lines, key)["ok"] is True
    full = Path(tempfile.mkdtemp()) / "audit.jsonl"
    full.write_text("".join(json.dumps(x) + "\n" for x in lines))
    assert verify_file(full, key)["ok"] is True

    # tamper a field → receipt mismatch localized to that seq
    bad = [dict(x) for x in lines]
    bad[1]["host"] = "exfil.evil"
    res = verify_chain(bad, key)
    assert res["ok"] is False and res["first_break_seq"] == 1

    # drop the middle line → seq gap detected
    dropped = [lines[0], lines[2]]
    res = verify_chain(dropped, key)
    assert res["ok"] is False and res["gap_before_seq"] == 2
    assert verify_chain([lines[1], lines[0], lines[2]], key)["ok"] is False
    res = verify_chain(lines[1:], key)
    assert res["ok"] is False and res["gap_before_seq"] == 1
    full.write_text(json.dumps(lines[0]) + "\nnot-json\n")
    assert verify_file(full, key)["ok"] is False

    # key custody round-trips and is 0600
    cfg = Config(runtime_dir=Path(tempfile.mkdtemp()))
    k1 = persist_receipt_key(cfg, "sX")
    assert k1 and load_receipt_key(cfg, "sX") == k1 and persist_receipt_key(cfg, "sX") == k1  # stable
    assert (cfg.runtime_dir / "receipt-keys" / "sX.key").stat().st_mode & 0o777 == 0o600
    assert (cfg.runtime_dir / "sessions" / "sX") not in (
        cfg.runtime_dir / "receipt-keys" / "sX.key").parents
    assert load_receipt_key(cfg, "missing") is None
    print("ok  receipts: cross-lang golden + tamper/gap detection + key custody (F2)")


def test_egress_profiles_store():
    """Named egress profiles: CRUD over the firewall-rule model + host/CIDR/port hygiene."""
    from orchestrator.egress_profiles import EgressProfileStore

    st = EgressProfileStore(Path(tempfile.mkdtemp()) / "profiles.json")
    p = st.create(name="gh", rules=[
        {"action": "allow", "dest": "API.GITHUB.COM"},           # canonicalized to lowercase
        {"action": "allow", "dest": "*.evil"},                    # wildcard dropped
        {"action": "allow", "dest": "10.20.0.0/16", "ports": [443, 5432], "note": "db"},
        {"action": "inspect", "dest": "pypi.org"},
        {"action": "bogus", "dest": "x.com"},                     # unknown action → allow
    ])
    dests = {(r["action"], r["dest"]) for r in p["rules"]}
    assert ("allow", "api.github.com") in dests and ("inspect", "pypi.org") in dests
    assert ("allow", "10.20.0.0/16") in dests and ("allow", "x.com") in dests
    assert not any(r["dest"] == "*.evil" for r in p["rules"])    # wildcard dropped
    cidr = next(r for r in p["rules"] if r["dest"] == "10.20.0.0/16")
    assert cidr["ports"] == [443, 5432] and cidr["note"] == "db"
    assert p["mode"] == "enforce" and p["id"].startswith("egp_")
    # persisted + reloads
    st2 = EgressProfileStore(st.path)
    assert st2.get(p["id"])["name"] == "gh" and len(st2.list()) == 1
    # update + bad-mode falls back to enforce
    upd = st2.update(p["id"], name="github", mode="bogus", rules=[{"action": "deny", "dest": "telemetry.bad"}])
    assert upd["name"] == "github" and upd["mode"] == "enforce"
    assert upd["rules"] == [{"action": "deny", "dest": "telemetry.bad", "ports": None, "enabled": True, "note": ""}]
    assert st2.get(None) is None and st2.get("nope") is None
    assert st2.delete(p["id"]) is True and st2.list() == []
    print("ok  egress: profile store CRUD over rules (domain/CIDR/port hygiene, wildcards dropped)")


def test_egress_rules_migration():
    """A pre-rules profile (three lists on disk) is migrated to rules on load, once."""
    import json as _json
    from orchestrator.egress_profiles import EgressProfileStore

    path = Path(tempfile.mkdtemp()) / "profiles.json"
    legacy = {"profiles": [{"id": "egp_old", "name": "old", "mode": "enforce",
                            "allow": ["github.com", "10.0.0.0/8"], "deny": ["bad.com"], "inspect": ["pypi.org"]}]}
    path.write_text(_json.dumps(legacy))
    st = EgressProfileStore(path)
    r = st.get("egp_old")["rules"]
    kinds = {(x["action"], x["dest"]) for x in r}
    assert kinds == {("allow", "github.com"), ("allow", "10.0.0.0/8"), ("deny", "bad.com"), ("inspect", "pypi.org")}
    # migration persisted: the legacy list keys are gone from disk after a save
    st.update("egp_old", name="old2")
    on_disk = _json.loads(path.read_text())["profiles"][0]
    assert "rules" in on_disk and "allow" not in on_disk and "inspect" not in on_disk
    print("ok  egress: pre-rules profiles migrate to the rules model on load (once)")


def test_egress_presets():
    """Built-in presets instantiate into real profiles; rules pass host hygiene; unknown
    preset raises."""
    from orchestrator.egress import clean_rules
    from orchestrator.egress_profiles import EGRESS_PRESETS, EgressProfileStore, list_presets

    lp = list_presets()
    keys = {p["key"] for p in lp}
    assert {"developer", "python", "node", "data-science", "anthropic-only", "web-audit"} <= keys
    assert all({"key", "name", "description", "mode", "rules"} <= p.keys() for p in lp)
    # Presets ship in the canonical rules[] shape — the same one the editor, the store and
    # Warden read. They used to be authored as legacy allow/deny/inspect lists, converted on
    # every instantiate and expanded back by hand in the console.
    for preset in lp:
        assert preset["rules"] == clean_rules(preset["rules"]), f"{preset['key']} is not canonical"
        assert all({"action", "dest", "ports", "enabled"} <= r.keys() for r in preset["rules"])
    dev = {r["dest"] for r in EGRESS_PRESETS["developer"]["rules"]}
    assert {"pypi.org", "registry.npmjs.org"} <= dev
    assert EGRESS_PRESETS["anthropic-only"]["rules"] == []
    assert EGRESS_PRESETS["web-audit"]["mode"] == "monitor"

    st = EgressProfileStore(Path(tempfile.mkdtemp()) / "profiles.json")
    prof = st.create_preset("developer")
    dev_allow = {r["dest"] for r in prof["rules"] if r["action"] == "allow"}
    assert prof["name"] == "Developer" and "github.com" in dev_allow and prof["id"].startswith("egp_")
    named = st.create_preset("python", name="my-py")
    py_allow = {r["dest"] for r in named["rules"] if r["action"] == "allow"}
    assert named["name"] == "my-py" and {"pypi.org", "files.pythonhosted.org", "github.com"} <= py_allow
    try:
        st.create_preset("nope")
        assert False, "unknown preset must raise"
    except KeyError:
        pass
    print("ok  egress: built-in presets (developer/python/node/ds/anthropic-only/web-audit) instantiate")


def test_egress_profile_resolution():
    """A session's effective Warden policy comes from its attached ENVIRONMENTS' egress
    profiles (there is no per-agent egress pin); no environment → the global policy; the
    global KILL switch always overrides (panic button)."""
    import json as _json

    from orchestrator.egress import EgressPolicyStore
    from orchestrator.egress_profiles import EgressProfileStore
    from orchestrator.environments import EnvironmentStore
    from orchestrator.manager import SessionManager
    from orchestrator.runners import SessionConfig

    d = Path(tempfile.mkdtemp())
    mgr = SessionManager(Config(runtime_dir=d, logs_dir=d))
    mgr.egress_store = EgressPolicyStore(d / "policy.json", seed_allow=("global.example",))
    mgr.profile_store = EgressProfileStore(d / "profiles.json")
    mgr.environment_store = EnvironmentStore(d / "envs.json")
    dests = lambda pol, action: {r["dest"] for r in pol["rules"] if r["action"] == action}  # noqa: E731
    prof = mgr.profile_store.create(name="gh", rules=[
        {"action": "allow", "dest": "api.github.com"}, {"action": "inspect", "dest": "api.github.com"}])
    env = mgr.environment_store.create(name="gh", egress_profile=prof["id"])

    # no environment → global policy
    g = _json.loads(mgr._resolve_session_policy(SessionConfig(harness=Harness(model="x"))))
    assert dests(g, "allow") == {"global.example"} and g["kill"] is False

    # attached environment → its profile's rules (NOT the global allow-list)
    scoped = SessionConfig(harness=Harness(model="x", environments=[env["id"]]))
    r = _json.loads(mgr._resolve_session_policy(scoped))
    assert dests(r, "allow") == {"api.github.com"} and dests(r, "inspect") == {"api.github.com"}
    assert "global.example" not in dests(r, "allow")

    # an IP/CIDR grant (with per-rule ports) flows through to the session's Warden policy JSON —
    # this is how one agent gets internal-network reach; allow_metadata rides the global policy.
    internal = mgr.profile_store.create(name="lan",
        rules=[
            {"action": "allow", "dest": "git.internal"},
            {"action": "allow", "dest": "10.20.0.0/16", "ports": [443, 5432]},
            {"action": "allow", "dest": "192.168.5.10"}],
        # host override: Warden resolves this internal name to a private IP instead of DNS
        hosts=[{"host": "git.internal", "ip": "10.1.20.50"}, {"host": "BAD", "ip": "not-an-ip"}])
    env2 = mgr.environment_store.create(name="lan", egress_profile=internal["id"])
    mgr.egress_store.set(allow_metadata=True)
    ri = _json.loads(mgr._resolve_session_policy(SessionConfig(harness=Harness(model="x", environments=[env2["id"]]))))
    assert {"10.20.0.0/16", "192.168.5.10", "git.internal"} <= dests(ri, "allow")
    cidr = next(r for r in ri["rules"] if r["dest"] == "10.20.0.0/16")
    assert cidr["ports"] == [443, 5432] and ri["allow_metadata"] is True    # per-rule ports survive
    assert ri["hosts"] == [{"host": "git.internal", "ip": "10.1.20.50"}]     # override flows through; junk dropped
    mgr.egress_store.set(allow_metadata=False)

    # GLOBAL host overrides (internal DNS) are infrastructural: they must reach an
    # environment-attached session too, not just global-policy sessions — unioned with the
    # profile's own, profile winning on a name conflict. (Regression: previously dropped.)
    mgr.egress_store.set(hosts=[{"host": "vault.internal", "ip": "10.9.9.9"},
                                {"host": "git.internal", "ip": "10.0.0.1"}])  # conflicts with profile's
    rg = _json.loads(mgr._resolve_session_policy(SessionConfig(harness=Harness(model="x", environments=[env2["id"]]))))
    hmap = {h["host"]: h["ip"] for h in rg["hosts"]}
    assert hmap["vault.internal"] == "10.9.9.9"          # global-only override now flows through
    assert hmap["git.internal"] == "10.1.20.50"          # profile-specific wins the conflict
    # and a plain global-policy session still sees the global overrides
    gg = _json.loads(mgr._resolve_session_policy(SessionConfig(harness=Harness(model="x"))))
    assert {h["host"] for h in gg["hosts"]} == {"vault.internal", "git.internal"}
    mgr.egress_store.set(hosts=[])

    # global KILL overrides even a scoped session
    mgr.egress_store.set(kill=True)
    assert _json.loads(mgr._resolve_session_policy(scoped))["kill"] is True
    assert _json.loads(mgr._resolve_session_policy(SessionConfig(harness=Harness(model="x"))))["kill"] is True

    # an environment referencing a deleted/unknown profile fails closed
    ghost = mgr.environment_store.create(name="ghost", egress_profile="egp_gone")
    unknown = SessionConfig(harness=Harness(model="x", environments=[ghost["id"]]))
    mgr.egress_store.set(kill=False)
    assert dests(_json.loads(mgr._resolve_session_policy(unknown)), "allow") == set()
    mgr.registry.close()
    print("ok  egress: per-session resolution (environment profile vs global; global kill overrides)")


def test_environments_scope_secrets_and_egress():
    """Environments group {secrets, egress profile}; an agent attaches to N of them for
    least-privilege scoping. No environments grants no secret. With environments = ONLY
    the union of their named secrets, and egress merged from their profiles."""
    import json as _json

    from orchestrator.egress import EgressPolicyStore
    from orchestrator.egress_profiles import EgressProfileStore
    from orchestrator.environments import EnvironmentStore
    from orchestrator.manager import SessionManager
    from orchestrator.runners import SessionConfig
    from orchestrator.secret_store import SecretStore
    from orchestrator.secrets import UserSecretStore

    d = Path(tempfile.mkdtemp())
    mgr = SessionManager(Config(runtime_dir=d, logs_dir=d))
    mgr.egress_store = EgressPolicyStore(d / "policy.json", seed_allow=("global.example",))
    mgr.profile_store = EgressProfileStore(d / "profiles.json")
    mgr.environment_store = EnvironmentStore(d / "envs.json")
    mgr.secret_store = UserSecretStore(d / "sec-idx.json", SecretStore(d / "sec.enc", kek="k"))

    # two secrets on different hosts
    mgr.secret_store.put("gh", scopes=["api.github.com"], header="Authorization",
                         template="Bearer {value}", value="ghtok")
    mgr.secret_store.put("stripe", scopes=["api.stripe.com"], header="Authorization",
                         template="Bearer {value}", value="sktok")

    dests = lambda pol, action: {r["dest"] for r in pol["rules"] if r["action"] == action}  # noqa: E731
    # env bundles the gh secret + a github egress profile
    ghprof = mgr.profile_store.create(name="gh-egress", rules=[{"action": "allow", "dest": "api.github.com"}])
    env_gh = mgr.environment_store.create(name="github", secrets=["gh"], egress_profile=ghprof["id"])
    env_pay = mgr.environment_store.create(name="payments", secrets=["stripe"])

    def secrets_of(cfg):
        raw = mgr._resolve_session_secrets(cfg)
        return sorted(r["hosts"][0] for r in _json.loads(raw)["secrets"]) if raw else []

    # (1) no environments → no secret grant
    legacy = SessionConfig(harness=Harness(model="x"))
    assert secrets_of(legacy) == []

    # (2) attach only github → ONLY the gh secret (stripe token is NOT injected — the fix)
    only_gh = SessionConfig(harness=Harness(model="x", environments=[env_gh["id"]]))
    assert secrets_of(only_gh) == ["api.github.com"]
    # and the gh host is folded into an inspect rule so Warden MITMs it for injection
    pol = _json.loads(mgr._resolve_session_policy(only_gh))
    assert dests(pol, "allow") == {"api.github.com"} and "api.github.com" in dests(pol, "inspect")
    assert "api.stripe.com" not in dests(pol, "inspect")   # not this agent's secret

    # (3) attach both → union of their secrets
    both = SessionConfig(harness=Harness(model="x", environments=[env_gh["id"], env_pay["id"]]))
    assert secrets_of(both) == ["api.github.com", "api.stripe.com"]

    # (4) deny-safe: environments set but referencing an unknown id ⇒ NO secrets (not all)
    ghost = SessionConfig(harness=Harness(model="x", environments=["env_deadbeef"]))
    assert secrets_of(ghost) == []

    # (5) egress merge: enforce wins, rules union. A monitor env + an enforce env ⇒ enforce.
    monprof = mgr.profile_store.create(name="mon", mode="monitor", rules=[{"action": "allow", "dest": "extra.example"}])
    env_mon = mgr.environment_store.create(name="mon", egress_profile=monprof["id"])
    merged = _json.loads(mgr._resolve_session_policy(
        SessionConfig(harness=Harness(model="x", environments=[env_gh["id"], env_mon["id"]]))))
    assert merged["mode"] == "enforce"  # gh profile is enforce → wins
    assert dests(merged, "allow") == {"api.github.com", "extra.example"}  # union

    # (6) global KILL still overrides a scoped session (panic button)
    mgr.egress_store.set(kill=True)
    assert _json.loads(mgr._resolve_session_policy(only_gh))["kill"] is True

    mgr.registry.close()
    print("ok  environments: per-agent secret scoping + egress merge (least privilege; deny-safe; kill wins)")


def test_environment_store_crud():
    from orchestrator.environments import EnvironmentStore, _UNSET

    d = Path(tempfile.mkdtemp())
    st = EnvironmentStore(d / "envs.json")
    e = st.create(name="  github  ", description="gh access", secrets=["gh", "gh", " "], egress_profile="egp_1")
    assert e["name"] == "github" and e["secrets"] == ["gh"] and e["egress_profile"] == "egp_1"  # trimmed + de-duped
    assert st.get(e["id"])["description"] == "gh access"
    # persists across reopen
    assert EnvironmentStore(d / "envs.json").get(e["id"])["secrets"] == ["gh"]
    # PATCH: omitted egress_profile is preserved; explicit null detaches
    st.update(e["id"], secrets=["gh", "npm"])
    assert st.get(e["id"])["egress_profile"] == "egp_1" and st.get(e["id"])["secrets"] == ["gh", "npm"]
    st.update(e["id"], egress_profile=None)
    assert st.get(e["id"])["egress_profile"] is None
    assert st.update(e["id"], name="gh2", egress_profile=_UNSET)["egress_profile"] is None  # sentinel = keep
    assert st.delete(e["id"]) and st.get(e["id"]) is None and not st.delete(e["id"])
    print("ok  environments: store CRUD (trim/de-dup, persist, PATCH sentinel, delete)")


def test_migrate_agent_egress_pins():
    """The legacy per-agent egress_profile pin is converted to an attached environment at
    boot (raw-JSON migration, since Harness no longer carries the field). Idempotent; agents
    sharing a profile share ONE auto-created egress-only environment; dangling pins still
    migrate (no egress silently lost)."""
    import json as _json

    from orchestrator.egress_profiles import EgressProfileStore
    from orchestrator.environments import EnvironmentStore
    from orchestrator.migrations import migrate_agent_egress_pins

    d = Path(tempfile.mkdtemp())
    profiles = EgressProfileStore(d / "profiles.json")
    envs = EnvironmentStore(d / "envs.json")
    prof = profiles.create(name="dev-tools", rules=[{"action": "allow", "dest": "api.github.com"}])
    agents_path = d / "agents.json"
    agents_path.write_text(_json.dumps({
        "agt_1": {"name": "A", "harness": {"model": "x", "egress_profile": prof["id"]}},
        "agt_2": {"name": "B", "harness": {"model": "y", "egress_profile": prof["id"],  # shares the profile
                                           "environments": ["env_existing"]}},
        "agt_3": {"name": "C", "harness": {"model": "z", "egress_profile": "egp_deleted"}},  # dangling
        "agt_4": {"name": "D", "harness": {"model": "w"}},  # nothing to do
    }))
    n = migrate_agent_egress_pins(agents_path, envs, profiles)
    assert n == 3
    data = _json.loads(agents_path.read_text())
    # pins removed everywhere
    assert all("egress_profile" not in a["harness"] for a in data.values())
    # agt_1 + agt_2 share ONE auto-created env for dev-tools; agt_2 keeps its prior env
    e1 = data["agt_1"]["harness"]["environments"]
    e2 = data["agt_2"]["harness"]["environments"]
    assert len(e1) == 1 and e1[0] in e2 and "env_existing" in e2
    shared = envs.get(e1[0])
    assert shared["egress_profile"] == prof["id"] and shared["secrets"] == [] and shared["name"] == "egress: dev-tools"
    # dangling pin still migrated to an env referencing the missing profile id
    e3 = envs.get(data["agt_3"]["harness"]["environments"][0])
    assert e3["egress_profile"] == "egp_deleted"
    assert "environments" not in data["agt_4"]["harness"]
    # idempotent: a re-run is a no-op (no new envs, no changes)
    before = len(envs.list())
    assert migrate_agent_egress_pins(agents_path, envs, profiles) == 0 and len(envs.list()) == before
    print("ok  migration: legacy egress pin → shared egress-only environment (idempotent, deny-safe)")


def test_propagate_agent_harness_live():
    """Editing an agent live-applies the two hot fields (budget cap + egress profile) to
    that agent's RUNNING sessions, leaves other agents' sessions alone, and a session is
    an immutable snapshot (an agent edit can't leak model/prompt into a running session)."""
    import asyncio

    from orchestrator.agents import AgentSpec
    from orchestrator.egress import EgressPolicyStore
    from orchestrator.egress_profiles import EgressProfileStore
    from orchestrator.environments import EnvironmentStore
    from orchestrator.manager import Session, SessionManager
    from orchestrator.runners import SessionConfig as _SC

    d = Path(tempfile.mkdtemp())
    mgr = SessionManager(Config(runtime_dir=d, logs_dir=d))
    mgr.egress_store = EgressPolicyStore(d / "policy.json", seed_allow=("global.example",))
    mgr.profile_store = EgressProfileStore(d / "profiles.json")
    mgr.environment_store = EnvironmentStore(d / "envs.json")
    prof = mgr.profile_store.create(name="gh", rules=[{"action": "allow", "dest": "api.github.com"}])
    env = mgr.environment_store.create(name="gh", egress_profile=prof["id"])

    # snapshot isolation: a session built from an agent must NOT share the harness object
    spec = AgentSpec(id="agt_a", name="A", harness=Harness(model="opus", max_budget_usd=1.0))
    sc = _SC.from_agent(spec)
    assert sc.harness is not spec.harness
    spec.harness.model = "haiku"                      # later agent edit…
    assert sc.harness.model == "opus"                # …doesn't leak into the live snapshot

    s1 = Session("s1", mgr.config, _SC(harness=Harness(model="x", max_budget_usd=1.0), agent_id="agt_a"),
                 mgr.registry, mgr.notifier)
    s2 = Session("s2", mgr.config, _SC(harness=Harness(model="x"), agent_id="agt_b"),
                 mgr.registry, mgr.notifier)
    mgr.sessions["s1"], mgr.sessions["s2"] = s1, s2

    touched = asyncio.run(mgr.propagate_agent_harness(
        "agt_a", {"max_budget_usd": 5.0, "environments": [env["id"]], "model": "sonnet"}))
    assert touched == ["s1"]                                       # only agt_a's running session
    assert s1.sess.harness.max_budget_usd == 5.0                  # budget cap applied live
    assert s1.sess.harness.environments == [env["id"]]
    assert "api.github.com" in (s1.sess.egress_policy_json or "")  # policy re-resolved via the attached env
    assert s1.sess.harness.model == "sonnet"                      # model now hot-applied live (set_model, cache penalty)
    assert s2.sess.harness.max_budget_usd is None                 # other agent untouched
    mgr.registry.close()
    print("ok  agent edit: budget + environments + model apply live to running sessions")


def test_credential_backoff_persists_across_restart():
    """A 429 refresh backoff must survive an orchestrator restart — else every redeploy
    forgets it and immediately re-hammers the token endpoint, feeding the rate-limit spiral."""
    import asyncio
    import urllib.error

    from orchestrator.credentials import CredentialManager, _now_ms

    d = Path(tempfile.mkdtemp())
    store = d / "credentials.json"
    # token within the 15-min skew → ensure_fresh attempts a refresh
    store.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "a", "refreshToken": "rt", "expiresAt": _now_ms() + 5 * 60_000}}))
    mgr = CredentialManager(seed_path=None, store_path=store)
    mgr._refresh_call = lambda rt: (_ for _ in ()).throw(
        urllib.error.HTTPError(mgr.token_url, 429, "rate limited", {}, None))

    assert asyncio.run(mgr.ensure_fresh()) is False          # near-expiry + 429 → not refreshed
    assert mgr._next_retry_at > _now_ms() and mgr._fail_count == 1
    state = store.with_name("credentials.refresh_state.json")
    assert state.exists() and json.loads(state.read_text())["next_retry_at"] == mgr._next_retry_at

    # "restart": a fresh manager loads the persisted backoff…
    mgr2 = CredentialManager(seed_path=None, store_path=store)
    assert mgr2._next_retry_at == mgr._next_retry_at and mgr2._fail_count == 1 and mgr2._last_error == "refresh HTTP 429"

    # …and RESPECTS it — no re-hammer of the token endpoint while still backed off
    calls = {"n": 0}
    mgr2._refresh_call = lambda rt: calls.__setitem__("n", calls["n"] + 1) or {"access_token": "x", "expires_in": 3600}
    asyncio.run(mgr2.ensure_fresh())
    assert calls["n"] == 0, "must not re-hammer mid-backoff after restart"

    # a freshly pasted token clears the persisted backoff
    asyncio.run(mgr2.set_credentials({"claudeAiOauth": {"accessToken": "p", "refreshToken": "q", "expiresAt": _now_ms() + 7200_000}}))
    assert json.loads(state.read_text())["next_retry_at"] == 0
    print("ok  credentials: 429 backoff persists across restart + cleared on paste")


def test_api_request_models():
    """`effort` reaches the harness via every request model, unknown fields are
    forbidden (no silent field-drop), and the removed `egress` knob is now rejected
    like any other unknown field — Warden is mandatory, so there is nothing to override."""
    import pydantic

    from orchestrator.api import CreateSessionRequest, HarnessRequest, UpdateAgentRequest

    # effort + interactive flow through to the Harness
    assert HarnessRequest(effort="high").to_harness("sonnet").effort == "high"
    assert HarnessRequest(interactive=True).to_harness("sonnet").interactive is True
    assert HarnessRequest().to_harness("sonnet").interactive is False
    assert HarnessRequest(approval="all").to_harness("sonnet").approval == "all"
    assert HarnessRequest(approval=["Bash"]).to_harness("sonnet").approval == ["Bash"]
    assert HarnessRequest().to_harness("sonnet").approval == "off"
    assert not hasattr(HarnessRequest().to_harness("sonnet"), "egress")  # field removed
    from terracore.harness import HARNESS_FIELDS
    assert "egress" not in HARNESS_FIELDS

    # extra='forbid' — a renamed/unknown field (incl. the removed `egress` no-op) surfaces as 422
    for Model in (HarnessRequest, UpdateAgentRequest, CreateSessionRequest):
        for bad in ({"definitely_not_a_field": 1}, {"egress": "warden"}):
            try:
                Model(**bad)
                assert False, f"{Model.__name__} accepted {bad}"
            except pydantic.ValidationError:
                pass

    # create_session's guard: an agent owns its harness, so an inline field set ALONGSIDE
    # agent_id (which would be silently dropped) is detected via model_fields_set → 422.
    from orchestrator.api import SESSION_SCOPED_HARNESS_FIELDS
    allowed = {"agent_id", "title", *SESSION_SCOPED_HARNESS_FIELDS}
    r = CreateSessionRequest(agent_id="a", title="t", model="opus")
    assert set(r.model_fields_set) - allowed == {"model"}, "stray agent-owned field is flagged"
    r_ok = CreateSessionRequest(agent_id="a", title="t")  # session-scoped only → no stray
    assert not (set(r_ok.model_fields_set) - allowed)
    print("ok  api: effort reaches the harness; removed egress rejected; agent_id+inline-field guarded")


def test_sandbox_seeds_are_volumes_not_host_paths():
    """The sandbox rootfs is --read-only, so bootstrap files ride per-session volumes filled
    through a throwaway container. Copying into the container itself fails at the first
    session with "container rootfs is marked read-only" — not in CI."""
    from orchestrator.runners import _CA_PEM

    cfg = Config(api_key="k", runtime_dir=Path(tempfile.mkdtemp()))
    r = DockerRunner(session_id="seed1", config=cfg, sess=SessionConfig())
    r._warden_endpoint = ("172.30.0.9", 8888)
    r._warden_ca_pem = b"pem"
    cmd = " ".join(r.build_command())
    # Every seeded path is a NAMED VOLUME, never a host path: a bind source is resolved by the
    # daemon, and the orchestrator's paths do not exist there under a named-volume deploy.
    assert r._seed_vol("ca") in cmd          # the CA is seeded in every mode
    assert "/etc/ssl/proxy-ca:ro" in cmd     # ...and still mounted read-only
    assert all(not str(dest).startswith("/") for _, dest in r._seed), "seeds must be volumes"
    assert _CA_PEM in [src for src, _ in r._seed]


def test_docker_runner_creates_before_starting():
    """The sandbox is `docker create`d, seeded, then started. `docker run` would race the
    worker against its own CA, which is exactly the ordering the seeding exists to fix."""
    import inspect

    from orchestrator.runners import DockerRunner

    cmd = inspect.getsource(DockerRunner.build_command)
    assert '"create"' in cmd and '"run", "-d"' not in cmd, "sandbox must be created, not run"
    start = inspect.getsource(DockerRunner.start)
    assert start.index("_seed_container") < start.index('"start"'), "seed must precede start"


def test_warden_uses_no_host_paths():
    """No `-v` in the Warden bootstrap. Every bind mount there was a path the host had to
    agree with, and Docker creates a missing source rather than failing — which is how a
    named-volume deploy got an empty /wstate and a Warden that exited 1."""
    import inspect

    from orchestrator.warden import WardenController

    src = inspect.getsource(WardenController.start)
    assert '"-v"' not in src, "Warden must not bind-mount host paths"
    assert '"create"' in src, "Warden must be created, then seeded, then started"


def test_concurrent_memory_is_isolated_on_every_runner():
    """A second live session for the same agent must not share the single-writer memory
    volume. This was k8s-only, because Kubernetes refuses the second mount while Docker
    silently attaches it twice and lets the writes interleave."""
    from orchestrator.manager import SessionManager

    for runner in ("docker", "k8s", "local"):
        mgr = SessionManager.__new__(SessionManager)
        mgr.config = Config(runner=runner, runtime_dir=Path(tempfile.mkdtemp()))
        mgr._memory_busy = lambda _v: True
        vol, isolated = mgr._resolve_memory("terrarium-mem-agt1", "20260802-1111-2222-abcd")
        assert isolated, f"{runner}: concurrent run must not share the memory volume"
        assert vol != "terrarium-mem-agt1"
        mgr._memory_busy = lambda _v: False
        vol, isolated = mgr._resolve_memory("terrarium-mem-agt1", "20260802-1111-2222-abcd")
        assert (vol, isolated) == ("terrarium-mem-agt1", False), f"{runner}: idle must share"
    print("ok  memory: concurrent runs isolate on every runner, not just k8s")


def test_preflight_reports_missing_sandbox_image():
    """A missing TERRA_IMAGE must fail readiness, not the first session. shunt and Compose do
    not pull it (the sandbox is not a service), so a bad tag used to surface as
    "warden sidecar failed to start" when someone launched an agent."""
    import asyncio

    from orchestrator import runners

    calls = []

    async def fake_run(cmd):
        calls.append(cmd[:3])
        return (1, "Error response from daemon: manifest unknown")

    orig, runners._run = runners._run, fake_run
    try:
        cfg = Config(runner="docker", image="ghost:nope", runtime_dir=Path(tempfile.mkdtemp()))
        err = asyncio.run(runners.preflight_image(cfg))
        assert err and "ghost:nope" in err and "could not be pulled" in err
        assert ["docker", "image", "inspect"] in calls   # checks presence first
        assert ["docker", "pull", "ghost:nope"] in calls  # then tries to fetch it

        # present image → no error, and no pull attempted
        calls.clear()

        async def ok(cmd):
            calls.append(cmd[:3])
            return (0, "")

        runners._run = ok
        assert asyncio.run(runners.preflight_image(cfg)) is None
        assert ["docker", "pull", "ghost:nope"] not in calls

        # other runners have no image to check
        for r in ("k8s", "local"):
            c2 = Config(runner=r, image="ghost:nope", runtime_dir=Path(tempfile.mkdtemp()))
            assert asyncio.run(runners.preflight_image(c2)) is None
    finally:
        runners._run = orig
    print("ok  preflight: missing sandbox image fails readiness, not the first session")


def test_warden_failure_reports_container_diagnostics():
    """A dead sidecar has no IP; the reason is its exit code and logs. Those used to be
    discarded by `docker rm -f` before anything read them, so a missing cred, a read-only
    mount and a registry outage all printed the same 'could not resolve IP'."""
    import asyncio

    from orchestrator.warden import WardenController

    cfg = Config(runtime_dir=Path(tempfile.mkdtemp()))
    ctl = WardenController(cfg, "ses_diag", None)

    async def fake_docker(*args):
        if args[0] == "inspect":
            return 0, "exited exit=1"
        if args[0] == "logs":
            return 0, "Error: Permission denied (os error 13)"
        return 0, ""

    ctl._docker = fake_docker
    why = asyncio.run(ctl._why_dead())
    assert "exited exit=1" in why
    assert "Permission denied" in why
    print("ok  warden: failure reports the container's exit code and logs")


def main() -> int:
    return run(globals(), "ALL UNIT TESTS PASSED")


if __name__ == "__main__":
    sys.exit(main())
