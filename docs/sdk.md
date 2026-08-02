---
title: SDK
nav_order: 7
---

# Python SDK

```bash
pip install terrarium-python
```

The distribution is `terrarium-python`; the import package is `terrarium`.

An async client for the orchestrator API. The surface mirrors the
[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) — `query`,
`TerrariumOptions`, typed messages, `can_use_tool` — with extra parameters for Terrarium's own
features. Async-only, and its sole dependency is `httpx`.

```python
import asyncio
from terrarium import TerrariumClient, TerrariumOptions, AssistantMessage, TextBlock

async def main():
    async with TerrariumClient("https://terrarium.example.com", token="…") as client:
        async with client.session(options=TerrariumOptions(model="claude-haiku-4-5")) as s:
            async for msg in s.receive_response("What is 7 × 6?"):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(block.text)

asyncio.run(main())
```

## Concepts

| Concept | What it is | Lifetime |
|---|---|---|
| **Client** | A connection to one orchestrator (`base_url` + bearer token) | Your process |
| **Agent** | A reusable stored config: model, persona, egress, allowed tools | Persistent, `client.agents` |
| **Session** | One running conversation in a fresh sandbox, from an agent's config or an inline one | Ephemeral by default, or durable |
| **Client tool** | A tool whose handler runs in your process, callable by the sandboxed agent | Per-session |
| **Environment** | A named `{secrets, egress profile}` bundle an agent attaches to | Persistent, `client.environments` |

`TerrariumOptions` is a superset of `ClaudeAgentOptions` and exposes the same configurability
as the console; a parity test enforces that. Only the fields you set are sent.

## Client tools

A client tool's handler runs in **your** process, with your application context. Only the
agent's tool input crosses out, and only the result you return crosses back in. Your code,
state and secrets never enter the sandbox.

## CLI

The package installs `terra-cli`:

```bash
terra-cli sessions list
terra-cli verify-egress <session-id>   # recompute a session's audit hash chain
```

---

Full reference — every resource, streaming, rewind, uploads, downloads, egress profiles and
error types — is in
[`sdk/README.md`](https://github.com/OAISP/terrarium/blob/main/sdk/README.md).
