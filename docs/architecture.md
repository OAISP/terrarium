---
title: Architecture
nav_order: 2
---

# Architecture
{: .no_toc }

1. TOC
{:toc}

---

A control plane (orchestrator + console) spawns one ephemeral sandbox per session. The sandbox
runs two processes — the agent **worker** and the **Warden** egress mediator — sharing a
network namespace but separated by uid and a kernel firewall.

```
             Operator:  console :3737  ·  SDK  ·  curl
                              │
                              │  HTTPS + bearer token
                              ▼
        ┌─────────────────────────────────────────────────┐
        │  Orchestrator — FastAPI :8900                   │
        │  sessions · agents · schedules · tokens · logs   │
        │  egress policy · credentials                     │
        │                                                  │
        │  CredentialManager   SessionRegistry  (sqlite)   │
        │  (rotating refresh,  EventStore       (jsonl)    │
        │   sealed at rest)    EgressPolicyStore           │
        └─────────────────────────┬───────────────────────┘
                                  │  spawns / reattaches
                                  │  (Docker container | k8s Pod)
                                  ▼
        ┌─────────────────────────────────────────────────┐
        │  Session sandbox — ephemeral, single-tenant      │
        │                                                  │
        │   worker  uid 1001                Warden uid 1002│
        │   Agent SDK + Claude Code  ◄─ lo:8888 ─►  Rust   │
        │   holds a DECOY credential        TLS-terminate  │
        │                                   inject cred    │
        │                                   allow-list     │
        │   nftables OUTPUT default-DROP    DLP · audit    │
        │   only Warden may leave                  │       │
        └──────────────────────────────────────────┼──────┘
                                                   │ real credential
                                                   ▼
                              api.anthropic.com + allow-listed hosts
```

## Components

| Component | Tech | Role |
|---|---|---|
| **Orchestrator** | Python, FastAPI (`orchestrator/`) | REST API, session lifecycle, credential ownership, egress policy, durability. Spawns sandboxes via the `docker`, `k8s` or `local` runner. |
| **Worker** | Python (`sandbox/worker.py`) | Drives the Claude Code CLI through the Agent SDK. Streams typed events; handles interrupts, rewind and uploads. |
| **Warden** | Rust (`warden/`) | Per-session MITM egress mediator and the single policy brain. See [Security]({% link security.md %}#egress). |
| **Console** | Next.js, Tailwind, shadcn/ui (`web/`) | Operator UI: sessions, agents, usage, schedules, tokens, egress, logs. |
| **SDK** | Python (`sdk/terrarium/`) | Typed async client for CI and automation. See [SDK]({% link sdk.md %}). |
| **Shared protocol** | `terracore/` | Worker↔orchestrator event and command protocol, harness model, personas, event store. |

## Egress mediation

Every outbound request is forced through Warden, checked against live policy, and
re-credentialed at the boundary.

```
 agent (uid 1001)                  Warden (uid 1002)              upstream
   │  HTTPS_PROXY=127.0.0.1:8888        │  policy.json (hot-reload)
   │  CONNECT host:443 ────────────────►│  host allow-listed?
   │                                    │   ├─ no  → DENY + audit
   │                                    │   └─ yes → TLS-terminate,
   │                                    │            strip decoy header,
   │                                    │            inject real cred ──► api.anthropic.com
   │  ◄─────────────────────────────────│  ◄──────────────────────────────
   ▼                                    ▼
 raw IP / other host → kernel DROP   HMAC-signed receipt → audit.jsonl
```

Policy and credential are hot-reloaded by mtime. Editing the allow-list in the console, or a
credential refresh, is patched into each running session's Warden, so changes apply to live
sessions.

## Session lifecycle

```
 create ─► resolve agent/harness ─► spawn sandbox: [Warden sidecar] + [worker]
                                      │         policy · cred · CA · audit
                                      ▼
                                   worker connects to Claude ─► EV_READY
                                      │
        ┌────────────┬────────────────┼──────────────┬────────────┐
        ▼            ▼                ▼              ▼            ▼
     message     interrupt         rewind         upload        idle
        │                       (files / convo)                   │
        ▼                                                         ▼
     run turn ──────────────────────────────────────►  durable (sandbox kept)
        │
        └──────► event log (jsonl, append-only source of truth)
                                      │
    orchestrator restart ─► rehydrate: reattach RUNNING sandboxes, reap orphans
                                      │
                                   stop ─► reap sandbox + per-session policy/cred/CA
```

The event log is the durable source of truth; the registry is a cache rebuildable from it. A
restarted orchestrator replays from the logs and reattaches to live sandboxes. Sessions
survive orchestrator restarts, not sandbox restarts.

## Credential flow

The orchestrator owns the credential because a subscription token's rotating refresh chain
cannot live in an ephemeral per-session Warden.

```
 CredentialManager (orchestrator)                    per-session Warden
   ├─ refresh before expiry (15-min skew)              reads cred (mtime hot-reload)
   ├─ rotate + persist (AES-256-GCM, KEK)                     ▼
   ├─ exponential backoff on 429                       inject at egress
   └─ on change → patch every live session ──────────► (sandbox sees a decoy)
```

A console re-paste or an auto-refresh propagates to running sessions, so long-lived agents do
not 401 when their start-time token expires.

## Stream loss versus session death

The orchestrator reads events over a stream that is a *client* of the sandbox — for the Docker
runner, a `docker attach` subprocess. That stream can end while the container is still running:
a Docker daemon restart, a host suspend, a dropped idle connection.

The pump probes the sandbox before concluding anything and reattaches up to five times with
backoff while the container is alive. Only a sandbox that is genuinely gone produces
`worker_lost`.

To reattach a session stranded past the retry budget:

```bash
curl -X POST localhost:8900/v1/sessions/<id>/recover
```

It returns **409** unless the sandbox is actually running. A 409 means the transcript is all
that remains: still readable and downloadable, but not resumable, because the CLI's own
transcript lived inside that container.

## Rewind and edit

Built on the Agent SDK's checkpointing. A per-turn anchor (the user-message uuid) supports
three console actions:

| Action | Effect |
|---|---|
| **Edit this message** | Truncate the conversation to before that turn and return its text to the composer. |
| **Restore workspace to here** | Roll files back to that turn (`rewind_files`). |
| **Rewind + restore workspace** | Both. |

A failed rewind is a no-op and keeps the session alive.

## State

All state is local to your deployment. Nothing leaves it except agent-authorized egress.

| Store | Path | Contents |
|---|---|---|
| **Event log** | `logs/<session>.jsonl` | Append-only typed event stream. The source of truth. |
| **Session registry** | `runtime/sessions.db` | Metadata cache: id, agent, status, model, cost, `last_seq`. Rebuildable. |
| **Egress policy** | `runtime/egress/policy.json` | Mode, rules, kill switch. Warden's source of truth. |
| **Egress audit** | `runtime/egress/audit/<session>.jsonl` | HMAC-signed receipts, one chain per session. Survives the sandbox. |
| **Credential store** | `runtime/credentials.json` | Sealed with AES-256-GCM under `TERRA_KEK` (or `TERRA_TOKEN`). |
| **Agent registry** | `runtime/agents.json` | Reusable harness configs. |
| **Schedules** | `runtime/schedules.json` | UTC 5-field cron. |
| **API tokens** | `runtime/tokens.json` | Scoped bearer tokens, hashed. |
| **Memory volume** | per-agent (shared) or per-session (isolated) | The agent's persistent `/memory`. RWO, so concurrent runs get isolated volumes. |

Every durable JSON store is written `0600` through a single `JsonStore` base.

The console's **Logs** view merges event logs and the egress audit into one stream filterable
by agent, session, source, type and host. It bounds its fan-out and reports when a cap
truncated the result.

## Project layout

```
orchestrator/   FastAPI control plane — api, manager, runners (docker/k8s/local),
                credentials, secret_store, egress, store, filebridge, registry, schedules
warden/         Rust egress mediator — proxy, policy, inject, ca, audit
sandbox/        Sandbox image — Dockerfile, worker.py, entrypoint.sh, firewall.sh
terracore/      Shared lib — protocol, harness, personas, models, toolset, events
sdk/            Python SDK (terrarium) + CLI
web/            Next.js console
docs/           This site
tests/          Unit suite + red-team egress tests
```
