---
title: Home
layout: home
nav_order: 1
---

# Terrarium

**Self-hosted, single-tenant runtime for autonomous Claude Code agents.**
{: .fs-6 .fw-300 }

Every session runs the real Claude Code engine, driven by the Claude Agent SDK, inside an
ephemeral sandbox behind a per-session egress firewall. The credential is injected at the
network boundary and never enters the sandbox.

[Get started]({% link quickstart.md %}){: .btn .btn-primary .mr-2 }
[View on GitHub](https://github.com/OAISP/terrarium){: .btn }

---

## Features

| Feature | Detail |
|---|---|
| **Hardened sandbox** | Non-root, `CapEff=0`, seccomp, no service-account token, kernel egress default-drop, DNS cut. |
| **Credential isolation** | Warden injects the real token at egress. The sandbox holds a decoy behind a generic corporate-proxy CA. |
| **Live egress control** | Allow, deny, inspect and kill, applied to running sessions rather than only new ones. |
| **Durable sessions** | Reattach to live sandboxes across orchestrator restarts. |
| **Rewind and edit** | Restore the workspace or the conversation to any past turn. |
| **Signed audit** | An HMAC-chained receipt per connection, verifiable after the sandbox is gone. |
| **Concurrent runs** | An agent is configuration; parallel sessions isolate their memory automatically. |

## Documentation

- [Quickstart]({% link quickstart.md %}) — run it locally with Docker.
- [Architecture]({% link architecture.md %}) — control plane, sandbox, session flow.
- [Security model]({% link security.md %}) — the isolation layers and their limits.
- [Configuration]({% link configuration.md %}) — environment variables.
- [SDK]({% link sdk.md %}) — the Python client.
- [Deployment]({% link deployment.md %}) — Docker, Kubernetes, or a single VPS.

## Status

Pre-1.0. The API surface is not frozen, and the Kubernetes path ships no reference manifests
(see [Deployment]({% link deployment.md %}#kubernetes)). Breaking changes are flagged in the
[release notes](https://github.com/OAISP/terrarium/releases).
