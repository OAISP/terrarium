# terrarium-python

Python client for the **Terrarium** orchestrator API — create and drive sandboxed Claude
agents over HTTP. The surface mirrors the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
(`query`, `TerrariumOptions`, typed messages, `can_use_tool`) with extra parameters for
Terrarium's own features. Like the Claude Agent SDK, it is **async-only** and its sole
dependency is `httpx`.

```bash
pip install terrarium-python
```

## Quickstart

```python
import asyncio
from terrarium import TerrariumClient, TerrariumOptions, AssistantMessage, TextBlock

async def main():
    async with TerrariumClient("https://terrarium-api.example.com", token="…") as client:
        async with client.session(options=TerrariumOptions(model="claude-haiku-4-5")) as s:
            async for msg in s.receive_response("What is 7 × 6?"):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(block.text)

asyncio.run(main())
```

One-shot, exactly like `claude_agent_sdk.query`:

```python
from terrarium import query, TerrariumOptions

async for msg in query(prompt="Summarise the repo", options=TerrariumOptions(model="opus")):
    print(msg)
```

## Mental model

| Concept | What it is | Lifetime |
|---|---|---|
| **Client** | a connection to one orchestrator (`base_url` + bearer `token`) | your process |
| **Agent** | a *reusable, stored config* — model, persona, egress, allowed tools | persistent; `client.agents` |
| **Session** | one *running conversation* in a fresh sandbox; inherits an agent's config (or takes an inline one) | ephemeral by default, or durable (see below) |
| **Client tool** | a tool whose handler runs in **your** process, callable by the sandboxed agent | per-session |
| **Egress profile** | a named Warden allow/deny bundle controlling what the sandbox can reach | persistent; `client.egress_profiles` |

`TerrariumOptions` is a superset of the Claude SDK's `ClaudeAgentOptions`: the Claude fields
(`model`, `system_prompt`, `allowed_tools`, `builtin_tools`, `permission_mode`, `max_turns`,
`max_budget_usd`, `mcp_servers`, `agents`, `thinking`, `effort`, `fallback_model`, `max_thinking_tokens`,
`betas`, `setting_sources`, `env`, `skills`, `can_use_tool`) plus Terrarium extras (`system_mode`,
`tools`, `approval`, `interactive`, `environments`, `memory_scope`, `agent_id`, `title`). It
exposes the **same configurability as the console** (a parity test enforces this). Only fields you
set are sent — everything else is the orchestrator default.

### Choosing the agent's built-in tools

Two distinct knobs (both Claude-SDK-aligned):

- **`builtin_tools`** — the **availability** allowlist: the base set of built-in tools the agent
  may use. `None` = all defaults; a list = *only* those; `[]` = none.
- **`allowed_tools`** — the **auto-approve** list: which available tools skip the permission
  *prompt*. Only matters for `interactive` / gated sessions (under the default `bypassPermissions`
  it's a no-op — everything's already approved).

```python
# a read-only research agent — it simply doesn't have Bash/Write/etc.
TerrariumOptions(model="haiku", builtin_tools=["Read", "Grep", "Glob", "WebSearch"])
```

### Trimming the default Claude harness

A fresh Claude Code session ships a lot of harness by default: ~30 built-in tools
(`Task`, `Workflow`, `CronCreate`, …), a dozen built-in skills / slash commands
(`code-review`, `deep-research`, `verify`, …) and a roster of built-in subagents. Three
knobs strip it down:

- **`builtin_tools`** — the tool availability allowlist (above). Leaving `Task` out removes
  the whole subagent system; leaving `Skill` out removes skill *invocation*.
- **`skills`** — the skill filter: `[]` hides **every** skill, including the CLI's built-ins
  (this is the "bare harness" setting — `False` only skips mounting your `skills/` dir, the
  CLI defaults still load); `["code-review"]` enables only the named ones; `"all"` / `True`
  enables everything discovered. `skills=[]` also pins `setting_sources` empty (the Claude
  SDK would otherwise re-enable `user`+`project` settings for skill discovery); pass
  `setting_sources` yourself to override.

  > **Don't judge the filter by the init event.** The CLI's `system/init` message reports
  > every skill/slash command it *discovered on disk*, unfiltered — that's inventory
  > metadata, not what the model sees. The filter applies to the model's context (the
  > skill listing is absent from the actual API request) and to invocation (the Skill tool
  > rejects unlisted skills). The `Skill` tool itself is availability, i.e. `builtin_tools`.
- **`agents`** — programmatically *define* subagents, instead of / on top of the built-in
  roster. Takes `terrarium.AgentDefinition` (field-for-field the Claude Agent SDK's, so
  ported code drops in unchanged) or an equivalent plain dict.

```python
# a minimal, no-frills agent: 4 tools, zero skills, no subagents
TerrariumOptions(
    builtin_tools=["Read", "Grep", "Glob", "Bash"],
    skills=[],
)

# or: bespoke subagents only, with a single curated skill
from terrarium import AgentDefinition
TerrariumOptions(
    skills=["code-review"],
    agents={"triager": AgentDefinition(description="Triage failing tests",
                                       prompt="You triage…",
                                       tools=["Read", "Grep", "Bash"], maxTurns=10)},
)
```

The same stripped setup is available as the one-click **`bare`** template:

```python
agent = await client.agents.create(name="minimal", template="bare")
```

`system_prompt` also accepts the Claude SDK's preset form —
`{"type": "preset", "preset": "claude_code"}` maps to the `claude_code` persona (`append`
has no Terrarium equivalent and is refused rather than silently dropped).

## Recipes

### Reusable agent + sessions

Create the config once; open as many sessions against it as you like.

```python
agent = await client.agents.create(
    name="Researcher", model="sonnet", system_mode="assistant",
    thinking={"type": "adaptive"}, allowed_tools=["WebSearch", "Read", "Bash"],
)
async with client.session(options=TerrariumOptions(agent_id=agent["id"])) as s:
    async for msg in s.receive_response("Summarize the latest on X."):
        print(msg)
```

### Custom tools with your application context

Define tools whose handlers run in **your** process — with your database, auth, secrets,
whatever — and the sandboxed agent can call them. Mirrors the Claude Agent SDK's `@tool`,
except the handler executes client-side: the agent's tool **input** crosses out, your handler
runs here, and only the **result** you return crosses back in. Your code and state never enter
the sandbox.

```python
from terrarium import query, TerrariumOptions, tool

@tool("get_user", "Look up a user by id", {"id": {"type": "string"}})
async def get_user(args):
    return await db.users.find(args["id"])     # runs in YOUR process, with YOUR context

async for msg in query(prompt="What's user 42's plan tier?",
                       options=TerrariumOptions(model="sonnet", tools=[get_user])):
    print(msg)
```

Only the schemas (`name`, `description`, `input_schema`) travel to the orchestrator; the
handlers stay local and are invoked automatically when the agent calls the tool (return a
string, or `{"content": ..., "is_error": bool}`). This is the sandbox-safe equivalent of the
Claude SDK's in-process MCP tools — and it works the same whether the session is inline or
bound to an `agent_id`.

#### Vision: returning images (screenshots / computer-use)

A client tool's `content` may be a **list of Anthropic content blocks**, so it can return *pixels*
to the agent — not just words. This is how you build screenshot / computer-use tooling: the screen
lives on your host, the tool grabs it, and the hosted agent reasons over the image directly (one
agent, one context — no separate vision loop).

```python
import base64

@tool("screenshot", "Capture the screen and return the image.", {})
async def screenshot(args):
    png = take_screenshot()  # your host-side capture → bytes
    return {"content": [{
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png",
                   "data": base64.b64encode(png).decode()},
    }]}
```

Image content blocks work the same on a **user turn** — pass a block list to
`receive_response` / `ask` / `send`:

```python
await s.ask([{"type": "text", "text": "What's on my screen?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}])
```

### Long-running / durable sessions

By default a session is **ephemeral** — `async with` deletes it on exit. For a session that
outlives the block (a persistent assistant, a scheduled job), pass `ephemeral=False`, then
reattach later with `client.attach(id)`:

```python
# start it, do a turn, leave it running
async with client.session(options=opts, ephemeral=False) as s:
    sid = s.id
    async for _ in s.receive_response("Boot up and remember you are my ops assistant."):
        pass

# … hours later, in another process …
async with client.attach(sid, tools=[get_user]) as s:   # attach() never deletes
    async for msg in s.receive_response("Any alerts overnight?"):
        print(msg)
```

`attach()` re-registers client-tool handlers via `tools=[…]` (the schemas were already sent at
creation). See [`examples/long_running.py`](../examples/long_running.py).

**Resume vs. replay.** `attach()` **resumes** by default: it reads the session's durable
`resume_cursor` and starts the stream there, so the next turn yields only new events. This is
what makes reattaching safe after *your* process restarts — without it the handle would replay
the log, and a replayed `client_tool_call` re-runs its handler here in your process, firing real
side effects a second time and posting stale results back to the agent.

The cursor is the seq of the last turn boundary, derived from the orchestrator's log/registry —
so it survives an orchestrator restart too. It deliberately lags rather than leads: mid-turn, the
tail replays, which is correct because a `client_tool_call` after the cursor is still waiting on
an answer.

```python
async with client.attach(sid, tools=[get_user]) as s:        # resume (default)
    ...
async with client.attach(sid, replay=True) as s:             # full history from seq 0
    ...
```

Use `replay=True` only to rebuild state from the whole log, and only with idempotent tools (or
none registered). Attaching to a **terminated** session raises — read its log via
`client.sessions.events(sid)` instead.

### Structured memory (retrieval, not a folder)

The sandbox's `/memory` is an FS volume — no retrieval, and on k8s its RWO PVC can't multi-attach
(concurrent sessions of one agent fork). `terrarium.memory` offers memory as **client tools** over
a real store instead: the agent calls `memory_search` / `memory_write` / `memory_get`, the handlers
run in your process, and concurrent sessions share one store (no fork). The reference
`SqliteMemory` is stdlib-only (FTS5, LIKE fallback); implement `MemoryStore` for pgvector/etc.

```python
from terrarium import TerrariumClient, TerrariumOptions
from terrarium.memory import SqliteMemory, memory_tools

store = SqliteMemory("assistant.db")      # durable across sessions/processes
opts = TerrariumOptions(agent_id=agent_id, tools=memory_tools(store))
# the agent can now memory_write facts and memory_search them on later turns / sessions
```

### Egress profiles

A named set of **firewall rules** controlling what a sandbox can reach. It's applied to an
agent by attaching an **environment** that references it (there is no direct per-agent egress
pin; see below). Instantiate a built-in preset (`anthropic-only`, `developer`, `python`,
`node`, `data-science`, `web-audit` — `await client.egress_profiles.presets()`), or build
rules directly:

```python
prof = await client.egress_profiles.create(
    name="internal-git", mode="enforce",
    rules=[
        {"action": "allow", "dest": "github.com"},                       # domain, ports 80/443
        {"action": "allow", "dest": "10.20.0.0/16", "ports": [443, 5432]},  # internal CIDR + DB port
        {"action": "deny",  "dest": "telemetry.example.com"},
        {"action": "inspect", "dest": "registry.npmjs.org"},             # TLS-terminate + scan
    ],
    # resolve an internal name to a fixed IP when your DNS server (not the sandbox) knows it
    hosts=[{"host": "git.internal.example", "ip": "10.1.20.200"}],
)
```

Each rule is `{"action": "allow"|"deny"|"inspect", "dest": <domain|ip|cidr>, "ports": [..],
"enabled": true, "note": ".."}`. `ports` (allow/inspect) lifts the default 80/443 wall for
that destination; `deny` is dest-only. Private ranges are denied unless an allow/inspect rule
covers them; the cloud-metadata IP is a hard floor.

### Environments — the per-agent egress + secret-scoping mechanism

Operator **secrets** are injected by Warden at the network boundary (the value never enters
the sandbox). By default *every* enabled secret is injected into *every* agent's Warden —
so any agent that can reach the scoped host can use the token. **Environments** fix that: an
environment groups a set of secrets and, optionally, an egress profile; an agent attaches to
one or more and then receives **only** those environments' secrets, under the egress merged
from their profiles. Environments are the single way to scope an agent's egress and secrets.

```python
# an operator secret (value lives in the vault + Warden, never the sandbox)
await client.secrets.put("gh", scopes=["api.github.com"], value=os.environ["GH_TOKEN"])
# bundle it with a matching egress profile
gh_prof = await client.egress_profiles.create(preset="developer", name="gh-egress")
env = await client.environments.create(name="github", secrets=["gh"], egress_profile=gh_prof["id"])
# only agents attached to this environment get the GitHub token injected + this egress
agent = await client.agents.create(name="pr-bot", environments=[env["id"]])
```

Need egress with no injected credential? Create an environment with an egress profile and no
secrets (`secrets=[]`) and attach it. An agent with **no** environments receives no operator
secrets under the global egress policy. Attaching multiple
environments unions their secrets; their egress profiles merge (enforce wins over monitor;
allowed hosts union — attaching can only widen reach, never narrow it). Referenced environments
and profiles must be detached before deletion; dangling references fail closed at resolution.
`client.environments.{list, create, update, delete}` and `client.secrets.{list, put, delete}`.

### Interactive agents (questions + permission gating)

An agent created with `interactive=True` can pause to ask the operator structured questions
(`question` events) or for tool approval (`permission` events, when `approval` is set). Resolve
both with one Claude-SDK-style `can_use_tool` callback (sync **or** async):

```python
from terrarium import TerrariumOptions, PermissionResultAllow, PermissionResultDeny

async def can_use_tool(tool, tool_input, ctx):
    if tool == "AskUserQuestion":
        qs = tool_input["questions"]
        return PermissionResultAllow(updated_input={"answers": {q["question"]: q["options"][0]["label"] for q in qs}})
    if tool == "Bash":
        return PermissionResultDeny(message="no shell")
    return PermissionResultAllow()  # or .always to remember for the session

opts = TerrariumOptions(interactive=True, approval="all", can_use_tool=can_use_tool)
async for msg in query(prompt="Design me a portfolio", options=opts, client=client):
    print(msg)
```

Or resolve manually while streaming: on a `QuestionMessage` call `await s.answer(question_id, {...})`;
on a `PermissionMessage` call `await s.decide(request_id, "allow"|"always"|"deny")`.

> **Divergence from the Claude Agent SDK:** persistence here is a Terrarium convenience —
> `PermissionResultAllow(always=True)` (or `decide(..., "always")`) remembers the decision for
> the session. The upstream SDK instead expresses this via `updated_permissions=[...]` on the
> result and has a `PermissionResultDeny(interrupt=...)` field; Terrarium models neither. If you
> are porting a Claude-SDK `can_use_tool` that returns `updated_permissions`, switch it to
> `always=True` here.

### Raw event stream

`receive_response()` yields typed, coalesced messages. For the underlying per-event stream
(token deltas, status, rewind points) use `ask()`:

```python
async for ev in s.ask("now dig deeper"):
    print(ev["type"], ev.get("text", ""))
```

## Message shape

`receive_response()` yields the Claude-Agent-SDK message types — `AssistantMessage` (with
`.content` blocks: `TextBlock` / `ThinkingBlock` / `ToolUseBlock`), `UserMessage`
(`ToolResultBlock`), `ResultMessage`, plus `SystemMessage` / `QuestionMessage` /
`PermissionMessage`. A turn's consecutive assistant blocks (text + tool_uses) are **coalesced
into one multi-block `AssistantMessage`**, just like the real Claude Agent SDK. (Need the raw
events instead? Use `ask()`.)

## API surface

```
client.agents.{create, list, get, update, delete}
client.sessions.{create, get, list, delete, send, interrupt, answer, decide,
                 set_model, set_permission_mode, stream}
client.schedules.{create, list, update, delete}
client.tokens.{create, list, delete}
client.egress_profiles.{list, presets, create, update, delete}
client.secrets.{list, put, delete}
client.environments.{list, create, update, delete}
client.{session, attach, health, fleet, templates}

Session.{connect, ask, run, query, receive_response, interrupt, answer, decide,
         set_model, set_permission_mode, rewind, upload_file, verify_egress, summary, close}
```

The `terra-cli` command bridges this async SDK via `asyncio.run`.
