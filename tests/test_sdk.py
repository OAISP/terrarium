"""SDK robustness tests (offline, via httpx MockTransport): typed errors,
transient retry, and SSE reconnect/resume/dedupe. The SDK is async-only, so the
tests are coroutines run via asyncio.run."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run

try:
    import httpx
    import terrarium as tsdk
    from terrarium.client import TerrariumClient
    import terrarium.client as c
except ImportError as e:  # SDK/httpx not installed in this env — don't fail the gate
    print(f"skip sdk tests: {e}")
    raise SystemExit(0)

c._backoff = lambda *_: 0  # no real backoff sleeps in tests (await asyncio.sleep(0) is instant)


def _client_with(handler) -> TerrariumClient:
    cl = TerrariumClient(base_url="http://orch", token="t")
    cl._http.c = httpx.AsyncClient(base_url="http://orch", transport=httpx.MockTransport(handler),
                                   headers={"Authorization": "Bearer t"})
    return cl


async def test_typed_errors() -> None:
    def h(req):
        return httpx.Response(404, json={"detail": "session not found"})
    cl = _client_with(h)
    try:
        await cl.sessions.get("nope")
        assert False, "should raise"
    except tsdk.NotFoundError as e:
        assert e.status == 404 and "not found" in str(e)
    print("ok  sdk: 404 → typed NotFoundError carrying status + detail")


async def test_transient_retry_then_success() -> None:
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"detail": "starting"})
        return httpx.Response(200, json={"sessions": []})
    cl = _client_with(h)
    out = await cl.sessions.list()
    assert out == [] and calls["n"] == 3, "should retry 5xx then succeed"
    print("ok  sdk: transient 5xx retried with backoff, then succeeds")


async def test_auth_error_not_retried() -> None:
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(403, json={"detail": "requires scope: admin"})
    cl = _client_with(h)
    try:
        await cl.sessions.list()
        assert False
    except tsdk.AuthError:
        pass
    assert calls["n"] == 1, "auth errors must not be retried"
    print("ok  sdk: 403 raises AuthError immediately (no retry)")


async def test_stream_resumes_and_dedupes() -> None:
    """First connection drops mid-stream; the SDK must reconnect with after=last
    and not re-yield already-seen seqs."""
    conn = {"n": 0}

    def h(req):
        conn["n"] += 1
        after = int(req.url.params.get("after", "-1"))
        if conn["n"] == 1:
            # deliver seq 0,1 then "drop" (close without session_end)
            body = "data: {\"seq\":0,\"type\":\"user\"}\n\ndata: {\"seq\":1,\"type\":\"assistant_text\"}\n\n"
            return httpx.Response(200, text=body)
        # reconnect: server replays after=1 → seq 2 then session_end
        assert after == 1, f"must resume from last seq, got after={after}"
        body = "data: {\"seq\":2,\"type\":\"result\"}\n\ndata: {\"seq\":3,\"type\":\"session_end\"}\n\n"
        return httpx.Response(200, text=body)

    cl = _client_with(h)
    seqs = [ev["seq"] async for ev in cl.sessions.stream("s")]
    assert seqs == [0, 1, 2, 3], f"expected contiguous 0..3, got {seqs}"
    assert conn["n"] == 2, "must have reconnected exactly once"
    print("ok  sdk: stream reconnects from last seq, dedupes, ends on session_end")


async def test_overflow_triggers_resync() -> None:
    """An _overflow sentinel forces a reconnect+replay (no event lost)."""
    conn = {"n": 0}

    def h(req):
        conn["n"] += 1
        after = int(req.url.params.get("after", "-1"))
        if conn["n"] == 1:
            body = "data: {\"seq\":0,\"type\":\"user\"}\n\ndata: {\"type\":\"_overflow\"}\n\n"
            return httpx.Response(200, text=body)
        assert after == 0
        return httpx.Response(200, text="data: {\"seq\":1,\"type\":\"session_end\"}\n\n")

    cl = _client_with(h)
    seqs = [ev["seq"] async for ev in cl.sessions.stream("s")]
    assert seqs == [0, 1] and conn["n"] == 2
    print("ok  sdk: _overflow sentinel triggers resync from the durable log")


def test_cli_clean_error_no_traceback() -> None:
    """F18: a 404 (or any API/transport error) must print a clean one-line message
    and exit 1 — NOT dump a raw traceback. The CLI bridges the async SDK via asyncio.run."""
    import io
    from contextlib import redirect_stderr

    import terrarium.cli as cli

    def h(req):
        return httpx.Response(404, json={"detail": "session not found"})

    orig = cli._client
    cli._client = lambda: _client_with(h)
    try:
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = cli.main(["sessions"])
        err = buf.getvalue()
    finally:
        cli._client = orig
    assert rc == 1, "CLI must exit non-zero on error"
    assert "404" in err and "not found" in err and "Traceback" not in err, f"unclean error: {err!r}"
    print("ok  cli: API errors print a clean message + exit 1 (no traceback)")


async def test_non_idempotent_not_retried() -> None:
    """F27: a transient (503) on a POST must NOT be retried — a duplicate session
    create / double-delivered message is worse than a surfaced error."""
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(503, json={"detail": "starting"})
    cl = _client_with(h)
    try:
        await cl.sessions.create()  # POST /v1/sessions
        assert False, "should raise"
    except tsdk.ServerError:
        pass
    assert calls["n"] == 1, f"POST must not be retried, got {calls['n']} attempts"
    # ...but a GET still retries (idempotent)
    calls["n"] = 0
    cl2 = _client_with(h)
    try:
        await cl2.sessions.list()
    except tsdk.ServerError:
        pass
    assert calls["n"] == c._MAX_RETRIES + 1, "GET should still retry transient failures"
    print("ok  sdk: POST not retried on transient; GET still retried (F27)")


async def test_stream_ends_on_synthetic_session_end() -> None:
    """F10: a terminated session whose replay ends WITHOUT session_end must still
    terminate the SDK stream (the orchestrator now appends a synthetic one) — not
    reconnect-loop forever."""
    conn = {"n": 0}

    def h(req):
        conn["n"] += 1
        body = ("data: {\"seq\":0,\"type\":\"user\"}\n\n"
                "data: {\"seq\":1,\"type\":\"session_end\",\"synthetic\":true}\n\n")
        return httpx.Response(200, text=body)
    cl = _client_with(h)
    seqs = [ev["seq"] async for ev in cl.sessions.stream("s")]
    assert seqs == [0, 1] and conn["n"] == 1, f"must stop on synthetic session_end, got {seqs} conns={conn['n']}"
    print("ok  sdk: synthetic session_end terminates the stream (no infinite reconnect) (F10)")


async def test_egress_profile_create_sends_rules() -> None:
    """egress_profiles.create posts the rules[] model (+ host overrides), NOT the old
    allow/deny/inspect lists the backend no longer reads."""
    import json
    seen = {}
    def h(req):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "egp_x", "name": "p", "mode": "enforce", "rules": [], "hosts": []})
    cl = _client_with(h)
    await cl.egress_profiles.create(
        name="lan", mode="enforce",
        rules=[{"action": "allow", "dest": "10.20.0.0/16", "ports": [443, 5432]}],
        hosts=[{"host": "git.internal", "ip": "10.1.20.50"}])
    assert seen["path"] == "/v1/egress/profiles"
    assert seen["body"]["rules"] == [{"action": "allow", "dest": "10.20.0.0/16", "ports": [443, 5432]}]
    assert seen["body"]["hosts"] == [{"host": "git.internal", "ip": "10.1.20.50"}]
    assert "allow" not in seen["body"] and "inspect" not in seen["body"]  # legacy fields gone
    print("ok  sdk: egress_profiles.create sends the rules[] model + host overrides")


def test_verify_egress_cli() -> None:
    """F2: `terra verify-egress <sid>` reports the chain result and exits 2 when the
    chain is broken (distinct from a transport error)."""
    import io
    from contextlib import redirect_stdout

    import terrarium.cli as cli

    def ok(req):
        return httpx.Response(200, json={"session_id": "s1", "ok": True, "checked": 3, "reason": "chain intact: 3 lines"})

    def broken(req):
        return httpx.Response(200, json={"session_id": "s1", "ok": False, "first_break_seq": 2, "reason": "receipt mismatch at seq 2"})

    orig = cli._client
    try:
        cli._client = lambda: _client_with(ok)
        rc = cli.main(["verify-egress", "s1"])
        assert rc == 0, "intact chain exits 0"

        cli._client = lambda: _client_with(broken)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["verify-egress", "s1"])
        assert rc == 2 and "receipt mismatch" in buf.getvalue(), f"broken chain → exit 2, got {rc}"
    finally:
        cli._client = orig
    print("ok  cli: verify-egress prints result + exits 2 on a broken chain (F2)")


def test_options_to_harness() -> None:
    """TerrariumOptions maps Claude-SDK field names + Terrarium extras to the harness:
    system_prompt → custom_prompt (+ custom persona); None fields are dropped; agent_id/
    title/can_use_tool are NOT harness keys."""
    from terrarium import TerrariumOptions, PermissionResultAllow
    o = TerrariumOptions(model="opus", system_prompt="Be terse", effort="high",
                         approval="edits", interactive=True, environments=["env_ci"],
                         agent_id="agt_x", title="T", can_use_tool=lambda *_: PermissionResultAllow())
    h = o.to_harness()
    assert h["custom_prompt"] == "Be terse" and h["system_mode"] == "custom"
    assert h["model"] == "opus" and h["effort"] == "high" and h["approval"] == "edits"
    assert h["interactive"] is True and h["environments"] == ["env_ci"]
    assert "agent_id" not in h and "title" not in h and "can_use_tool" not in h
    assert "permission_mode" not in h, "unset fields must be omitted"
    assert TerrariumOptions(system_mode="claude_code").to_harness() == {"system_mode": "claude_code"}
    # the claude_code preset maps to system_mode without silently dropping it
    assert TerrariumOptions(system_prompt={"type": "preset", "preset": "claude_code"}).to_harness() == {"system_mode": "claude_code"}
    # …but combining that preset with a conflicting persona input is refused loudly (not silently resolved)
    for bad in (dict(custom_prompt="x"), dict(system_mode="assistant")):
        try:
            TerrariumOptions(system_prompt={"type": "preset", "preset": "claude_code"}, **bad).to_harness()
            assert False, f"expected a conflict error for {bad}"
        except ValueError as e:
            assert "conflicting persona" in str(e)
    print("ok  sdk: TerrariumOptions.to_harness maps Claude-SDK + Terrarium fields, drops None")


def test_parse_message_types() -> None:
    """Raw events lift into the Claude-Agent-SDK message/block shapes."""
    from terrarium import (parse_message, AssistantMessage, UserMessage, ResultMessage,
                               SystemMessage, QuestionMessage, PermissionMessage,
                               TextBlock, ToolUseBlock, ToolResultBlock)
    a = parse_message({"type": "assistant_text", "text": "hi", "model": "claude-opus-4-8", "seq": 1})
    assert isinstance(a, AssistantMessage) and isinstance(a.content[0], TextBlock) and a.text == "hi"
    assert a.model == "claude-opus-4-8", "the responding model is carried onto AssistantMessage"
    tu = parse_message({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "ls"}, "seq": 2})
    assert isinstance(tu, AssistantMessage) and isinstance(tu.content[0], ToolUseBlock) and tu.content[0].name == "Bash"
    tr = parse_message({"type": "tool_result", "tool_use_id": "t", "content": "ok", "is_error": False, "seq": 3})
    assert isinstance(tr, UserMessage) and isinstance(tr.content[0], ToolResultBlock)
    assert isinstance(parse_message({"type": "result", "total_cost_usd": 0.1, "seq": 4}), ResultMessage)
    assert isinstance(parse_message({"type": "system", "subtype": "init", "data": {}, "seq": 5}), SystemMessage)
    assert isinstance(parse_message({"type": "question", "question_id": "q", "questions": [], "seq": 6}), QuestionMessage)
    assert isinstance(parse_message({"type": "permission", "request_id": "p", "tool_name": "Bash", "seq": 7}), PermissionMessage)
    assert parse_message({"type": "status", "status": "idle", "seq": 8}) is None  # no message equivalent
    print("ok  sdk: parse_message lifts events into Claude-SDK message/block types")


async def test_unknown_harness_kwarg_raises() -> None:
    """A harness-kwarg typo must raise (not silently drop) so `mdoel=` can't no-op."""
    cl = _client_with(lambda req: httpx.Response(200, json={"id": "a1"}))
    try:
        await cl.agents.create("x", mdoel="sonnet")
        assert False, "expected TypeError on unknown harness kwarg"
    except TypeError as e:
        assert "mdoel" in str(e)
    # a valid kwarg still goes through
    out = await cl.agents.create("x", model="sonnet")
    assert out["id"] == "a1"
    print("ok  sdk: unknown harness kwarg raises (typo can't silently no-op)")


async def test_query_typed_flow_with_can_use_tool() -> None:
    """The top-level query() opens a session, streams typed messages, and resolves a
    permission prompt via a Claude-SDK-style (async) can_use_tool callback (→ decide)."""
    from terrarium import (query, TerrariumOptions, PermissionResultAllow,
                               PermissionMessage, ResultMessage)
    calls = {"create": 0, "send": 0, "decide": None, "deleted": False}

    def h(req):
        p, m = req.url.path, req.method
        if m == "POST" and p == "/v1/sessions":
            calls["create"] += 1
            return httpx.Response(200, json={"id": "s1", "agent_id": None})
        if m == "POST" and p.endswith("/messages"):
            calls["send"] += 1
            return httpx.Response(200, json=None)
        if m == "POST" and p.endswith("/permission"):
            import json as _j
            calls["decide"] = _j.loads(req.content)["decision"]
            return httpx.Response(200, json={"ok": True})
        if m == "DELETE" and p.startswith("/v1/sessions/"):
            calls["deleted"] = True
            return httpx.Response(200, json=None)
        if p.endswith("/events"):
            body = ('data: {"seq":0,"type":"ready"}\n\n'
                    'data: {"seq":1,"type":"permission","request_id":"p1","tool_name":"Bash","input":{"command":"ls"}}\n\n'
                    'data: {"seq":2,"type":"assistant_text","text":"done"}\n\n'
                    'data: {"seq":3,"type":"result","total_cost_usd":0.02}\n\n')
            return httpx.Response(200, text=body)
        return httpx.Response(404, json={"detail": p})

    cl = _client_with(h)
    seen = []

    async def can_use_tool(tool, _input, _ctx):  # async callback (Claude-SDK style)
        seen.append(tool)
        return PermissionResultAllow(always=False)

    opts = TerrariumOptions(model="sonnet", interactive=True, approval="all", can_use_tool=can_use_tool)
    msgs = [m async for m in query(prompt="go", options=opts, client=cl)]

    assert calls["create"] == 1 and calls["send"] == 1, "must create the session and send the prompt"
    assert calls["decide"] == "allow", f"can_use_tool Allow → decide('allow'), got {calls['decide']}"
    assert seen == ["Bash"], "can_use_tool called with the gated tool name"
    assert any(isinstance(m, PermissionMessage) for m in msgs)
    assert any(isinstance(m, ResultMessage) for m in msgs), "turn yields a typed ResultMessage"
    assert calls["deleted"], "ephemeral query() session is deleted on exit"
    print("ok  sdk: query() streams typed messages + resolves permission via async can_use_tool")


async def test_receive_response_coalesces_assistant_blocks() -> None:
    """A turn's consecutive assistant blocks (text + tool_use) coalesce into ONE multi-block
    AssistantMessage (Claude-Agent-SDK shape); a tool_result closes it and opens a new one."""
    from terrarium import AssistantMessage, TextBlock, ToolUseBlock  # noqa: F401

    def h(req):
        p, m = req.url.path, req.method
        if m == "POST" and p == "/v1/sessions":
            return httpx.Response(200, json={"id": "s1", "agent_id": None})
        if m == "POST" and p.endswith("/messages"):
            return httpx.Response(200, json=None)
        if m == "DELETE" and p.startswith("/v1/sessions/"):
            return httpx.Response(200, json=None)
        if p.endswith("/events"):
            body = ('data: {"seq":0,"type":"ready"}\n\n'
                    'data: {"seq":1,"type":"assistant_text","text":"checking"}\n\n'
                    'data: {"seq":2,"type":"tool_use","id":"t1","name":"Read","input":{}}\n\n'
                    'data: {"seq":3,"type":"tool_result","tool_use_id":"t1","content":"ok"}\n\n'
                    'data: {"seq":4,"type":"assistant_text","text":"done"}\n\n'
                    'data: {"seq":5,"type":"result","total_cost_usd":0.01}\n\n')
            return httpx.Response(200, text=body)
        return httpx.Response(404, json={"detail": p})

    cl = _client_with(h)
    async with cl.session() as s:
        msgs = [m async for m in s.receive_response("go")]
    kinds = [type(m).__name__ for m in msgs]
    assert kinds == ["AssistantMessage", "UserMessage", "AssistantMessage", "ResultMessage"], kinds
    assert len(msgs[0].content) == 2, f"text + tool_use coalesce into one message, got {len(msgs[0].content)}"
    assert isinstance(msgs[0].content[0], TextBlock) and isinstance(msgs[0].content[1], ToolUseBlock)
    assert len(msgs[2].content) == 1, "post-tool_result text is its own assistant message"
    print("ok  sdk: receive_response coalesces assistant blocks into multi-block messages")


async def test_non_ephemeral_session_not_deleted() -> None:
    """ephemeral=False (and attach) leave the session running server-side — the durable,
    long-running pattern — instead of deleting it on context exit."""
    calls = {"deleted": 0}

    def h(req):
        p, m = req.url.path, req.method
        if m == "POST" and p == "/v1/sessions":
            return httpx.Response(200, json={"id": "s1", "agent_id": None})
        if m == "DELETE" and p.startswith("/v1/sessions/"):
            calls["deleted"] += 1
            return httpx.Response(200, json=None)
        if p.endswith("/events"):
            return httpx.Response(200, text='data: {"seq":0,"type":"ready"}\n\n')
        if m == "GET" and p == "/v1/sessions/s1":
            # attach() resolves the resume cursor first; -1 = no turn boundary yet, so it
            # falls through to the normal drain-to-ready path.
            return httpx.Response(200, json={"id": "s1", "status": "idle", "resume_cursor": -1})
        return httpx.Response(404, json={"detail": p})

    cl = _client_with(h)
    async with cl.session(ephemeral=False) as s:
        assert s.id == "s1"
    assert calls["deleted"] == 0, "ephemeral=False must NOT delete on exit"
    async with cl.attach("s1"):
        pass
    assert calls["deleted"] == 0, "attach() must NOT delete the session on exit"
    print("ok  sdk: ephemeral=False + attach() keep the session alive (durable)")


async def test_memory_store_and_tools() -> None:
    """SqliteMemory round-trips write→search→get; memory_tools wires it to client tools."""
    from terrarium.memory import SqliteMemory, memory_tools
    store = SqliteMemory()  # in-memory
    mid = await store.write("the db password rotates on the 1st", tags=["ops"])
    assert mid and (await store.get(mid))["content"] == "the db password rotates on the 1st"
    hits = await store.search("password")
    assert hits and "password" in hits[0]["content"], "FTS search finds the memory"
    assert await store.get("9999") is None, "missing id → None"
    tools = {t.name: t for t in memory_tools(store)}
    assert set(tools) == {"memory_write", "memory_search", "memory_get"}
    assert "saved memory" in await tools["memory_write"].handler({"content": "prefer dark mode"})
    assert "dark mode" in await tools["memory_search"].handler({"query": "dark mode"})
    assert await tools["memory_search"].handler({"query": "zzzneverwritten"}) == "no memories matched"
    print("ok  sdk: SqliteMemory + memory_tools round-trip (write/search/get)")


async def test_memory_recall_and_tag_coercion() -> None:
    """issue #3: recall must survive plural/singular inflection (porter + prefix), and a string
    `tags` (which models routinely pass despite the array schema) must NOT be char-joined."""
    from terrarium.memory import SqliteMemory, memory_tools, _coerce_tags
    store = SqliteMemory()
    # Bug 1 — morphological recall: write plural, search singular (and vice-versa)
    await store.write("Preferred programming languages: Python over Go")
    assert await store.search("language"), "singular query must find the plural memory"
    assert await store.search("preference"), "query 'preference' must find 'Preferred'"
    await store.write("I have two cats")
    assert await store.search("cat"), "query 'cat' must find 'cats'"
    # Bug 2 — string tags coerced to a clean list, not joined character-by-character
    assert _coerce_tags("preferences, timezone, programming-languages") == \
        ["preferences", "timezone", "programming-languages"]
    assert _coerce_tags(["a", "b"]) == ["a", "b"] and _coerce_tags(None) == [] and _coerce_tags(42) == []
    sid = await store.write("user timezone is JST", tags="preferences, timezone")
    assert (await store.get(sid))["tags"] == "preferences timezone", "string tag → clean list, no char-join"
    tools = {t.name: t for t in memory_tools(store)}
    await tools["memory_write"].handler({"content": "likes terse replies", "tags": "style,prefs"})
    assert await store.search("terse"), "handler path stores + recalls"
    # Bug 3 — a recall query is a bag of terms spread across DIFFERENT memories. With implicit
    # AND a broad query recalls nothing; OR must surface every partial match.
    store2 = SqliteMemory()
    await store2.write("User's timezone is JST")            # 'timezone' only here
    await store2.write("User prefers Python over Go")       # 'prefers' only here
    spread = await store2.search("timezone preference")     # one term per row → AND=0, OR=both
    assert len(spread) == 2, f"OR recall surfaces facts spread across rows (AND would give 0), got {len(spread)}"
    print("ok  sdk: memory recall across inflection + multi-row OR + tag coercion (issue #3)")


async def test_reused_session_second_turn_not_dropped() -> None:
    """issue #4: on a reused session, the previous turn's trailing 'status: idle' (emitted just
    after its result, at a higher seq) leads turn 2's after= window. It must NOT end turn 2 before
    the real events arrive."""
    from terrarium import AssistantMessage, TextBlock
    body = (
        'data: {"seq":0,"type":"ready"}\n\n'
        'data: {"seq":1,"type":"user","text":"hi"}\n\n'
        'data: {"seq":2,"type":"assistant_text","text":"hello"}\n\n'
        'data: {"seq":3,"type":"result","total_cost_usd":0.01}\n\n'
        'data: {"seq":4,"type":"status","status":"idle"}\n\n'        # <- trailing idle from turn 1
        'data: {"seq":5,"type":"user","text":"bye"}\n\n'
        'data: {"seq":6,"type":"status","status":"running"}\n\n'
        'data: {"seq":7,"type":"assistant_text","text":"goodbye"}\n\n'
        'data: {"seq":8,"type":"result","total_cost_usd":0.02}\n\n'
        'data: {"seq":9,"type":"status","status":"idle"}\n\n')

    def h(req):
        p, m = req.url.path, req.method
        if m == "POST" and p == "/v1/sessions": return httpx.Response(200, json={"id": "s1", "agent_id": None})
        if m == "POST" and p.endswith("/messages"): return httpx.Response(200, json=None)
        if m == "DELETE" and p.startswith("/v1/sessions/"): return httpx.Response(200, json=None)
        if p.endswith("/events"): return httpx.Response(200, text=body)
        return httpx.Response(404, json={"detail": p})

    cl = _client_with(h)
    async with cl.session() as s:
        [m async for m in s.receive_response("hi")]                    # turn 1 (ends at seq 3)
        t2 = [m async for m in s.receive_response("bye")]              # turn 2 (window leads with idle@4)
    txt2 = " ".join(b.text for m in t2 if isinstance(m, AssistantMessage)
                    for b in m.content if isinstance(b, TextBlock))
    assert "goodbye" in txt2, f"turn 2 was dropped by the stale idle; got {[type(m).__name__ for m in t2]}"
    print("ok  sdk: reused session 2nd turn survives the previous turn's trailing idle (issue #4)")


async def test_client_tool_returns_image_blocks() -> None:
    """issue #5: a client tool can return Anthropic content blocks (image) — passed through to the
    tool_result, NOT stringified — so hosted agents can do vision/computer-use."""
    from terrarium import tool, TerrariumOptions
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KG"}}

    @tool("screenshot", "grab the screen", {})
    async def screenshot(args):
        return {"content": [img]}

    captured = {}

    def h(req):
        p, m = req.url.path, req.method
        if m == "POST" and p == "/v1/sessions": return httpx.Response(200, json={"id": "s1", "agent_id": None})
        if m == "POST" and p.endswith("/messages"): return httpx.Response(200, json=None)
        if m == "POST" and p.endswith("/tool_result"):
            import json as _j; captured.update(_j.loads(req.content)); return httpx.Response(200, json={"ok": True})
        if m == "DELETE" and p.startswith("/v1/sessions/"): return httpx.Response(200, json=None)
        if p.endswith("/events"):
            return httpx.Response(200, text=(
                'data: {"seq":0,"type":"ready"}\n\n'
                'data: {"seq":1,"type":"client_tool_call","call_id":"c1","name":"screenshot","input":{}}\n\n'
                'data: {"seq":2,"type":"result"}\n\n'))
        return httpx.Response(404, json={"detail": p})

    cl = _client_with(h)
    async with cl.session(options=TerrariumOptions(tools=[screenshot])) as s:
        async for _ in s.receive_response("look at my screen"):
            pass
    assert captured.get("content") == [img], f"image blocks must pass through intact, got {captured.get('content')!r}"
    # and a string user turn still posts {"text": ...}; a block list posts {"content": [...]}
    import inspect
    assert "content" in inspect.signature(cl.sessions.send).parameters
    print("ok  sdk: client tool returns image content blocks, passed through (issue #5)")



async def test_attach_resumes_without_replaying_history() -> None:
    """attach() must resume from the durable cursor, not replay the log (issue #6).

    Replaying re-delivers completed `client_tool_call` events, and the SDK runs those
    handlers in the CONSUMER's process — so a reconnect after a restart re-fires real side
    effects and posts stale results back. Criteria 1 and 2 of the issue.
    """
    from terrarium.client import Session

    ran: list[str] = []
    asked: list[str] = []

    def handler(req):
        if req.url.path == "/v1/sessions/s9":
            return httpx.Response(200, json={"id": "s9", "status": "idle",
                                             "agent_id": "a1", "resume_cursor": 7})
        if req.url.path.endswith("/events") or "stream" in req.url.path:
            asked.append(str(req.url))
            # Only NEW events live past the cursor. If the SDK streamed from -1 the
            # orchestrator would also hand back seq<=7, including a client_tool_call.
            return httpx.Response(200, text=(
                'data: {"seq":8,"type":"user"}\n\n'
                'data: {"seq":9,"type":"assistant_text","text":"hi"}\n\n'
                'data: {"seq":10,"type":"result"}\n\n'))
        return httpx.Response(200, json={})

    cl = _client_with(handler)
    tool = tsdk.ClientTool(name="lookup", description="d", input_schema={},
                           handler=lambda inp: ran.append("lookup") or "x")

    sess = cl.attach("s9", tools=[tool])
    await sess.connect()
    # Seeded from resume_cursor, and connect() did not have to drain to a historical ready.
    assert sess._last_seq == 7, sess._last_seq
    assert sess.agent_id == "a1"

    events = [ev async for ev in sess._iter_turn()]
    assert [e["seq"] for e in events] == [8, 9, 10], events
    assert ran == [], f"attach must not re-run completed client tools, ran={ran}"
    assert all("after=7" in u or "after=-1" not in u for u in asked), asked

    # replay=True keeps the old full-history behavior for log consumers.
    assert cl.attach("s9", replay=True)._resume is False

    # A terminated session can't be driven — fail loudly rather than hang on a dead worker.
    def dead(req):
        return httpx.Response(200, json={"id": "s0", "status": "terminated", "resume_cursor": 4})
    try:
        await _client_with(dead).attach("s0").connect()
        assert False, "attach to a terminated session must raise"
    except tsdk.TerrariumError as e:
        assert "terminated" in str(e)

    print("ok  attach: resumes from cursor; no client-tool replay; terminated raises")


async def test_connect_raises_on_startup_failure() -> None:
    """connect() returns only on `ready`; a benign error before ready is tolerated, but a
    session that ends before ready raises (surfacing the worker's message) instead of handing
    back a dead session."""
    from terrarium.client import Session

    # (a) ready → connect returns the live session
    def ready(req):
        return httpx.Response(200, text='data: {"seq":0,"type":"ready"}\n\n')
    s = await Session(_client_with(ready), session_id="s1").connect()
    assert s.id == "s1"

    # (b) a non-fatal error BEFORE ready (e.g. "client tools disabled") is tolerated
    def warn_then_ready(req):
        return httpx.Response(200, text=(
            'data: {"seq":0,"type":"error","message":"client tools disabled: x"}\n\n'
            'data: {"seq":1,"type":"ready"}\n\n'))
    s2 = await Session(_client_with(warn_then_ready), session_id="s2").connect()
    assert s2.id == "s2"

    # (c) error + session_end before ready → raises, surfacing the worker's message
    def fail(req):
        return httpx.Response(200, text=(
            'data: {"seq":0,"type":"error","message":"invalid harness config: bad model"}\n\n'
            'data: {"seq":1,"type":"session_end"}\n\n'))
    try:
        await Session(_client_with(fail), session_id="s3").connect()
        assert False, "connect must raise when the session ends before ready"
    except tsdk.TerrariumError as e:
        assert "invalid harness config" in str(e) and "s3" in str(e)
    print("ok  sdk: connect() raises on startup failure, tolerates a benign pre-ready error")


def test_version_resolves_from_distribution():
    """__version__ must come from the DISTRIBUTION name, which differs from the import name.

    The package is imported as `terrarium` but distributed as `terrarium-python`. Looking the
    metadata up by the import name doesn't raise — it just falls through to "0+unknown" and
    every request goes out with a wrong User-Agent, permanently and silently.
    """
    import tomllib
    from pathlib import Path as _P

    dist = tomllib.loads((_P(__file__).resolve().parents[1] / "sdk" / "pyproject.toml").read_text())
    name = dist["project"]["name"]
    assert name == "terrarium-python", f"distribution renamed to {name}: update this guard + __init__"
    assert tsdk.__version__ != "0+unknown", (
        "terrarium.__version__ did not resolve — __init__ is looking up the wrong distribution name")
    print(f"ok  sdk: __version__ resolves from the {name} distribution ({tsdk.__version__})")


def main() -> int:
    return run(globals(), "ALL SDK TESTS PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
