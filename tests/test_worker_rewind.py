"""Worker conversation-rewind robustness (offline, mocked ClaudeSDKClient).

Regression for a live crash: a turn that 401'd (bad credential) never persists a
CLI conversation, so a later "Edit this message" (conversation rewind) resumes a
session the CLI can't find → the worker used to treat that reconnect failure as a
fatal "session client error" and TERMINATE. The documented guarantee is that a
failed rewind never crashes the session — it must drop the bad resume and
reconnect FRESH instead.

Run:  uv run python tests/test_worker_rewind.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ["TERRA_NO_RESEAL"] = "1"  # don't re-exec on import
os.environ.setdefault("TERRA_HARNESS", json.dumps({"model": "opus", "system_mode": "custom"}))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sandbox"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run  # noqa: E402

try:
    import worker  # noqa: E402  (needs claude_agent_sdk — present in the sandbox env)
    from terracore import protocol as P  # noqa: E402
except Exception as e:  # SDK absent in this env — don't fail the gate
    print(f"skip worker rewind tests: {e}")
    raise SystemExit(0)


class FakeClient:
    """Connect succeeds/fails per a class-level script (by construction order)."""

    outcomes: list[str] = []
    n = 0

    def __init__(self, options=None):
        self.idx = FakeClient.n
        FakeClient.n += 1

    async def __aenter__(self):
        outcome = FakeClient.outcomes[self.idx] if self.idx < len(FakeClient.outcomes) else "ok"
        if outcome == "fail":
            raise RuntimeError("No conversation found with session ID: x")
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, text):  # noqa: D401
        pass

    async def receive_messages(self):
        # yield one message carrying a session_id (as the real CLI does) so the
        # worker records state["sid"] — the prerequisite for a conversation rewind —
        # then block like the real persistent stream until the worker cancels the pump
        # (on a conversation rewind or shutdown).
        yield type("Msg", (), {"session_id": "cli-sid-1"})()
        await asyncio.Event().wait()

    async def interrupt(self):
        pass

    async def rewind_files(self, mid):
        pass


def _drive(commands, outcomes):
    """Run worker.main() feeding `commands` then EOF; connects resolve per `outcomes`.
    Returns the emitted event dicts."""
    FakeClient.outcomes, FakeClient.n = outcomes, 0
    emitted: list[dict] = []

    saved = (worker.emit, worker.ClaudeSDKClient, worker._truncate_transcript, sys.stdin)
    worker.emit = lambda type, **f: emitted.append({"type": type, **f})
    worker.ClaudeSDKClient = FakeClient
    worker._truncate_transcript = lambda sid, to: (True, "new-sid-123")  # rewind "succeeds" → triggers resume

    lines = iter([json.dumps(c) + "\n" for c in commands] + [""])  # "" = EOF → shutdown
    sys.stdin = type("S", (), {"readline": staticmethod(lambda: next(lines, ""))})()
    try:
        asyncio.run(asyncio.wait_for(worker.main(), timeout=10))
    finally:
        worker.emit, worker.ClaudeSDKClient, worker._truncate_transcript, sys.stdin = saved
    return emitted


def test_rewind_resume_failure_recovers_not_crashes():
    """A failed resume after a conversation rewind reconnects FRESH and keeps the
    session alive — it does NOT emit a fatal session-client-error/terminated mid-rewind."""
    ev = _drive(
        commands=[P.query_cmd("hi"), P.rewind_cmd("m1", mode="conversation")],
        outcomes=["ok", "fail", "ok"],  # initial ok · resume reconnect FAILS · fresh reconnect ok
    )
    types = [e["type"] for e in ev]
    errs = [e.get("message", "") for e in ev if e["type"] == P.EV_ERROR]
    assert any("resume failed" in m for m in errs), f"expected a recovery error, got {errs}"
    assert not any("session client error" in m for m in errs), "must NOT take the fatal path"
    # the session reconnected after the rewind (an idle status AFTER the rewound event)...
    assert P.EV_REWOUND in types and types.count(P.EV_STATUS) >= 1
    # ...and terminated only ONCE, at the very end (clean shutdown — not a mid-rewind crash)
    assert types[-1] == P.EV_STATUS and ev[-1].get("status") == "terminated"
    assert sum(1 for e in ev if e.get("status") == "terminated") == 1
    print("ok  worker: failed conversation-rewind resume recovers fresh (no crash)")



def test_rewind_to_first_message_starts_fresh():
    """Rewinding to the FIRST turn must reconnect fresh, not fail to resume.

    Truncating at the first user message leaves a transcript with no turns. The CLI
    rejects `--resume` on that ("No conversation found with session ID: …"), which
    surfaced to operators as "resume failed — continuing in a fresh conversation".
    The end state was right; the error was noise. Now the empty case is reported as a
    successful rewind with nothing to resume.
    """
    import json as _json, tempfile, os as _os
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as td:
        home = _Path(td)
        proj = home / ".claude" / "projects" / "p"
        proj.mkdir(parents=True)
        sid = "sess-abc"
        first, second = "u-1", "u-2"
        (proj / f"{sid}.jsonl").write_text("\n".join([
            _json.dumps({"type": "summary", "summary": "bookkeeping, not a turn"}),
            _json.dumps({"type": "user", "uuid": first, "message": "hi"}),
            _json.dumps({"type": "assistant", "uuid": "a-1", "message": "hello"}),
            _json.dumps({"type": "user", "uuid": second, "message": "again"}),
            _json.dumps({"type": "assistant", "uuid": "a-2", "message": "yes"}),
        ]) + "\n")

        old_home = _os.environ.get("HOME")
        _os.environ["HOME"] = str(home)
        try:
            # Rewinding to the SECOND turn leaves real turns behind → resume in place.
            ok, resume = worker._truncate_transcript(sid, second)
            assert (ok, resume) == (True, sid), (ok, resume)

            # Rewinding to the FIRST turn leaves only the summary line, which is not a
            # conversation → succeed, but with nothing to resume.
            ok, resume = worker._truncate_transcript(sid, first)
            assert ok is True and resume is None, (ok, resume)

            # An anchor that is no longer present is a real failure, not a fresh start.
            assert worker._truncate_transcript(sid, "u-missing") == (False, None)
        finally:
            if old_home is None: _os.environ.pop("HOME", None)
            else: _os.environ["HOME"] = old_home

    print("ok  worker: rewind to the first message starts fresh (no resume error)")


def test_persistent_connect_failure_still_terminates():
    """If reconnect keeps failing (e.g. the new credential is also bad), the retry cap
    still terminates instead of looping forever."""
    ev = _drive(
        commands=[P.query_cmd("hi"), P.rewind_cmd("m1", mode="conversation")],
        outcomes=["ok"] + ["fail"] * 6,  # initial ok, then every reconnect fails
    )
    errs = [e.get("message", "") for e in ev if e["type"] == P.EV_ERROR]
    assert any("session client error" in m for m in errs), "must give up after the cap"
    assert ev[-1].get("status") == "terminated"
    print("ok  worker: persistent reconnect failure terminates after the retry cap")


def test_background_events_flow_after_turn_result():
    """The fix: a Workflow/Agent launched ``run_in_background`` keeps emitting AFTER the
    main turn's ``result``. The persistent pump must forward those post-result events
    instead of stranding them — the old per-turn ``receive_response()`` stopped at the
    result, so a backgrounded deep-research workflow's progress/completion was lost and
    its UI card spun forever."""
    from claude_agent_sdk import ResultMessage, SystemMessage

    result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                           is_error=False, num_turns=1, session_id="cli-sid-1",
                           total_cost_usd=0.01, usage={})
    bg = SystemMessage(subtype="task_notification",
                       data={"task_id": "t1", "tool_use_id": "tu1", "status": "completed"})

    class StreamClient:
        def __init__(self, options=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, text):
            pass

        async def receive_messages(self):
            yield result          # main turn ends here (old code would stop reading now)
            yield bg              # background event AFTER the turn — must still flow
            await asyncio.Event().wait()

        async def interrupt(self):
            pass

        async def rewind_files(self, mid):
            pass

    emitted: list[dict] = []
    saved = (worker.emit, worker.ClaudeSDKClient, sys.stdin)
    worker.emit = lambda type, **f: emitted.append({"type": type, **f})
    worker.ClaudeSDKClient = StreamClient
    lines = iter([json.dumps(P.query_cmd("go")) + "\n", ""])  # one query, then EOF
    sys.stdin = type("S", (), {"readline": staticmethod(lambda: next(lines, ""))})()
    try:
        asyncio.run(asyncio.wait_for(worker.main(), timeout=10))
    finally:
        worker.emit, worker.ClaudeSDKClient, sys.stdin = saved

    types = [e["type"] for e in emitted]
    assert P.EV_RESULT in types, "main turn result should be emitted"
    bg_ev = [e for e in emitted if e["type"] == P.EV_SYSTEM and e.get("subtype") == "task_notification"]
    assert bg_ev, f"background event after the result was stranded; emitted={types}"
    # it really arrived AFTER the turn result (the regression the fix targets)
    assert types.index(P.EV_RESULT) < types.index(P.EV_SYSTEM)
    print("ok  worker: background-task events after the turn result still flow (no stranding)")


def test_subprocess_env_scrub_disabled_by_default():
    """CLAUDE_CODE_SUBPROCESS_ENV_SCRUB must default to "0": the feature requires
    bubblewrap (absent in our hardened image) and the CLI fatally errors at startup
    if it's set without bwrap. Operator can still opt in via harness env."""
    from terracore.harness import Harness

    opts = worker._build_options(Harness(model="opus"))
    assert opts.env.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB") == "0"
    # operator override is still respected (e.g. if they install bwrap)
    opts2 = worker._build_options(Harness(model="opus", env={"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}))
    assert opts2.env.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB") == "1"
    print("ok  worker: subprocess env scrub disabled by default (bwrap absent); harness-overridable")


def main() -> int:
    return run(globals(), "ALL WORKER REWIND TESTS PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
