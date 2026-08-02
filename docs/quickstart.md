---
title: Quickstart
nav_order: 4
---

# Quickstart
{: .no_toc }

1. TOC
{:toc}

---

## Prerequisites

| Tool | For |
|---|---|
| Docker | Running the sandbox |
| Python 3.13 + [`uv`](https://docs.astral.sh/uv/) | Running the orchestrator |
| [`bun`](https://bun.sh) | Running the console |

Rust is only needed to build or test Warden outside Docker. `make build` compiles it inside
the sandbox image.

## Run it

```bash
git clone https://github.com/OAISP/terrarium.git
cd terrarium

make setup     # install orchestrator deps
make build     # build the sandbox image, including Warden
make network   # create the isolated agent network
make run       # start the orchestrator on :8900
make web       # in another shell: start the console on :3737
```

Open <http://localhost:3737>.

With no `TERRA_TOKEN` set, the console is open for local development. Setting it requires a
bearer token. The orchestrator refuses to start on a non-loopback bind without one.

## Add a credential

Read the [terms-of-service note]({% link security.md %}#terms-of-service) before choosing the
subscription path.

**API key** — no restriction:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make run
```

**Subscription** — paste the contents of `~/.claude/.credentials.json` into the console's
credential badge. It is sealed at rest and injected by Warden.

Either way the real credential never enters the sandbox; the agent sees a decoy.

## First agent

1. **Agents → New agent.** Pick a model and persona, or start from a template.
2. **Sessions → New session.** Choose the agent, describe the task, launch.
3. Watch the transcript stream. The inspector rail shows sub-agent activity, token
   composition, and files the agent wrote.

## Restrict what it can reach

A new session inherits the global egress policy. To narrow it:

1. **Boundary → Egress → New profile**, or start from a preset (Developer, Python, Node, Data
   science).
2. **Boundary → Environments → New environment**, and attach the profile.
3. Edit the agent and attach that environment.

{: .note }
> Attaching an environment scopes secrets (the agent gets only those) but *merges* egress
> profiles (enforce wins, allowed hosts union). Attaching can widen reach, never narrow it.

The **Egress** page shows live Warden decisions. The header carries a kill switch that freezes
all egress, including Anthropic, for every running session.

## Next

- [Configuration]({% link configuration.md %}) — environment variables.
- [SDK]({% link sdk.md %}) — drive this from Python.
- [Deployment]({% link deployment.md %}) — running it off your laptop.
