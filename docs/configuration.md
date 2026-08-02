---
title: Configuration
nav_order: 6
---

# Configuration
{: .no_toc }

1. TOC
{:toc}

---

Everything is an environment variable on the orchestrator. Each value may instead be supplied
as a file at `/run/secrets/<NAME>`, which is how Compose, Swarm and shunt deliver secrets.

Warden's own variables are set per session by the orchestrator and are listed at the end for
reading logs, not for you to set.

## Core

| Variable | Default | Purpose |
|---|---|---|
| `TERRA_RUNNER` | `docker` | Sandbox backend: `docker`, `k8s` or `local`. |
| `TERRA_IMAGE` | `terrarium-sandbox` | Sandbox image the orchestrator launches. |
| `TERRA_MODEL` | `sonnet` | Default model alias for new sessions. |
| `TERRA_HOST` / `TERRA_PORT` | `127.0.0.1` / `8900` | API bind. |
| `TERRA_RUNTIME_DIR` / `TERRA_LOGS_DIR` | `~/.terrarium` / `logs/` | State and event-log roots. |
| `TERRA_NETWORK` | `terrarium-net` | Docker network prefix; each session gets its own bridge. |
| `TERRA_DOCKER_RUNTIME` | — | Set to `runsc` for gVisor. |

## Auth

| Variable | Default | Purpose |
|---|---|---|
| `TERRA_TOKEN` | — | Admin bearer token. Also the fallback KEK. |
| `TERRA_ALLOW_NO_AUTH` | `0` | `1` permits a non-loopback bind with no token. |
| `TERRA_CORS_ORIGINS` | — | Comma-separated allow-list, used only when a token is set. Empty means no cross-origin access. |
| `TERRA_METRICS_PUBLIC` | `0` | `1` allows an unauthenticated `/metrics` scrape. |

{: .note }
> The orchestrator refuses to start on a non-loopback bind with no `TERRA_TOKEN` unless
> `TERRA_ALLOW_NO_AUTH=1`. The failure mode it prevents is an open admin API.

## Credentials

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | API-key mode. |
| `TERRA_KEK` | — | Seals the credential and secret stores at rest. Falls back to `TERRA_TOKEN`. |
| `TERRA_CREDS` | `~/.claude/.credentials.json` | Seed path for a subscription credential. |

{: .warning }
> Set a real `TERRA_KEK`. Without it the stores are sealed under `TERRA_TOKEN`, so rotating
> the API token re-keys the credential store — and operator secrets, which have no seed to
> recover from, become permanently undecryptable.

## Egress

| Variable | Default | Purpose |
|---|---|---|
| `TERRA_WARDEN_PORT` | `8888` | Loopback port the worker proxies through. |
| `TERRA_GATEWAY_ALLOW` | — | Comma-separated seed for the initial allow-list. Console-managed thereafter. |
| `TERRA_AUDIT_DRAIN_S` | `10` | How often each live session's audit is mirrored onto the orchestrator's volume. Bounds how stale the console's egress feed can be. `0` disables the sweep; stop and detach still drain. |
| `WARDEN_CA_CN` / `WARDEN_CA_ORG` / `WARDEN_CA_C` | `Zscaler Root CA` / `Zscaler Inc.` / `US` | Identity the interception CA presents to the agent. |

## Budgets

Three independent bounds. The first is worker-reported and therefore forgeable; the other two
are not.

| Variable | Default | Purpose |
|---|---|---|
| `TERRA_BUDGET_HARD_MULT` | `1.25` | Hard-kill once cumulative cost exceeds `max_budget_usd` times this. |
| `TERRA_BUDGET_MAX_TURNS` | `1000` | Orchestrator-counted result events before a hard kill. `0` disables. |
| `TERRA_BUDGET_MAX_RUN_SECONDS` | `7200` | Hard-kill a turn that never produces a `result`. `0` disables. |

## Limits

| Variable | Default | Purpose |
|---|---|---|
| `TERRA_MAX_LIVE_SESSIONS` | `32` | Global admission cap. |
| `TERRA_MAX_LIVE_SESSIONS_PER_AGENT` | `4` | Per-agent admission cap. |
| `TERRA_MAX_EVENT_LOG_BYTES` | 256 MiB | Per-session event log cap. Crossing it terminates the producer rather than truncating evidence. |
| `TERRA_MAX_AUDIT_LOG_BYTES` | 256 MiB | Same, for the egress audit. |
| `TERRA_MEMORY_SNAPSHOT_MAX_BYTES` | — | Cap on a `synced` memory snapshot. |

## Kubernetes

| Variable | Default | Purpose |
|---|---|---|
| `TERRA_K8S_NAMESPACE` | from the service-account mount | Namespace for sandbox Pods. |
| `TERRA_K8S_STORAGE_CLASS` | cluster default | Storage class for memory and workspace PVCs. |
| `TERRA_K8S_MEMORY_SIZE` | `1Gi` | Per-agent memory PVC. |
| `TERRA_K8S_WORKSPACE_SIZE` | `2Gi` | Per-session workspace. |
| `TERRA_K8S_MEMORY_EMPTYDIR_SIZE` | `256Mi` | `synced`-mode scratch. |
| `TERRA_K8S_AUDIT_SIZE` | `256Mi` | Warden's in-Pod audit emptyDir. |

## Notifications

| Variable | Default | Purpose |
|---|---|---|
| `TERRA_NOTIFY_WEBHOOK` | — | Fire-and-forget webhook (Discord, Slack, ntfy). |
| `TERRA_NOTIFY_ON` | `session_end,error,budget_exceeded` | Comma-separated event types. |

## Memory

`memory_mode` is a per-agent harness setting, not an environment variable, and it is the
biggest lever on Kubernetes launch latency.

| Mode | Behaviour |
|---|---|
| `volume` | Mount the per-agent RWO PVC at `/memory`. Survives anything; costs ~11s of volume attach per launch on Kubernetes. |
| `synced` *(default)* | No mount. The orchestrator restores a snapshot before the agent runs and snapshots back at each turn end. Fast launch; writes since the last turn are lost if the sandbox dies abruptly. |
| `none` | No mount, no snapshot. `/memory` is container-local scratch. |

Under Docker a local volume attaches in roughly 0ms, so `synced` and `volume` behave
identically.

## Per-agent CLI settings

An agent's harness `env` is merged into the sandboxed CLI's environment, so any Claude Code
environment variable can be set per agent.

One matters for budgeting: since CLI 2.1.219 a subagent may spawn nested subagents to depth 3,
up from 1, which multiplies the fan-out of a single turn. The backstops above still bound a
runaway, but later. Set `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` to restore flat delegation.

## Warden variables

Set per session by the orchestrator. Listed for debugging:
`WARDEN_LISTEN`, `WARDEN_POLICY`, `WARDEN_CRED`, `WARDEN_SECRETS`, `WARDEN_CA_DIR`,
`WARDEN_AUDIT`, `WARDEN_RECEIPT_KEY`, `WARDEN_ALLOW`, `WARDEN_INJECT`, `WARDEN_MAX_CONNS`,
`WARDEN_STREAM_IDLE_SECS`.
