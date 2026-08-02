<p align="center">
  <img src="web/app/icon.svg" alt="Terrarium" width="84" height="84" />
</p>

<h1 align="center">Terrarium</h1>

<p align="center">
  <b>Self-hosted, single-tenant runtime for autonomous Claude Code agents.</b><br>
  Hardened sandbox · per-session egress firewall · the credential never enters the sandbox.
</p>

<p align="center">
  <a href="https://github.com/OAISP/terrarium/actions/workflows/ci.yml"><img src="https://github.com/OAISP/terrarium/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/terrarium-python/"><img src="https://img.shields.io/pypi/v/terrarium-python?label=sdk" alt="PyPI"></a>
  <a href="https://hub.docker.com/r/k3scat/terrarium-orchestrator"><img src="https://img.shields.io/docker/v/k3scat/terrarium-orchestrator?label=docker&sort=semver" alt="Docker Hub"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="License"></a>
</p>

<p align="center">
  <a href="https://oaisp.github.io/terrarium/"><b>Documentation</b></a> ·
  <a href="https://oaisp.github.io/terrarium/quickstart/">Quickstart</a> ·
  <a href="https://oaisp.github.io/terrarium/architecture/">Architecture</a> ·
  <a href="https://oaisp.github.io/terrarium/security/">Security model</a>
</p>

---

Every session runs the real Claude Code engine, driven by the Claude Agent SDK, inside an
ephemeral sandbox behind a per-session egress firewall. The credential is injected at the
network boundary and never enters the sandbox.

| Feature | Detail |
|---|---|
| **Hardened sandbox** | Non-root, `CapEff=0`, seccomp, no service-account token, kernel egress default-drop, DNS cut. |
| **Credential isolation** | Warden injects the real token at egress. The sandbox holds a decoy behind a generic corporate-proxy CA. |
| **Live egress control** | Allow, deny, inspect and kill, applied to running sessions. |
| **Durable sessions** | Reattach to live sandboxes across orchestrator restarts. |
| **Rewind and edit** | Restore the workspace or the conversation to any past turn. |
| **Signed audit** | An HMAC-chained receipt per connection, verifiable after the sandbox is gone. |

## Quickstart

Needs Docker, Python 3.13 with [`uv`](https://docs.astral.sh/uv/), and [`bun`](https://bun.sh).

```bash
git clone https://github.com/OAISP/terrarium.git && cd terrarium

make setup   # install orchestrator deps
make build   # build the sandbox image, including Warden
make network # create the isolated agent network
make run     # orchestrator  → :8900
make web     # console, in another shell → :3737
```

Open <http://localhost:3737>, add a credential (or set `ANTHROPIC_API_KEY`), create an agent,
and launch a session.

→ [Full quickstart](https://oaisp.github.io/terrarium/quickstart/)

## How it fits together

```
 Operator ── console / SDK ──►  Orchestrator (FastAPI)
                                    │  owns the credential, egress policy, event log
                                    ▼
                        ┌───────── Session sandbox (ephemeral) ─────────┐
                        │  worker (uid 1001)  ──loopback──►  Warden     │
                        │  Claude Code + SDK                 (Rust)     │
                        │  holds a DECOY cred                uid 1002   │
                        │                                       │       │
                        │  nftables: default-DROP,              │ real  │
                        │  only Warden may leave                │ cred  │
                        └───────────────────────────────────────┼───────┘
                                                                ▼
                                                   api.anthropic.com +
                                                   allow-listed hosts only
```

→ [Architecture](https://oaisp.github.io/terrarium/architecture/) ·
[Security model](https://oaisp.github.io/terrarium/security/) ·
[Deployment](https://oaisp.github.io/terrarium/deployment/) — Docker, Kubernetes, or a single
VPS with [shunt](https://github.com/OAISP/shunt) (a ready [`shunt.example.toml`](shunt.example.toml) is included)

## SDK

```bash
pip install terrarium-python
```

```python
async with TerrariumClient("https://terrarium.example.com", token="…") as client:
    async with client.session(options=TerrariumOptions(model="claude-haiku-4-5")) as s:
        async for msg in s.receive_response("What is 7 × 6?"):
            print(msg)
```

→ [SDK docs](https://oaisp.github.io/terrarium/sdk/) · [`sdk/README.md`](sdk/README.md)

## Before you deploy

Two things to read rather than inherit, both covered in the
[security model](https://oaisp.github.io/terrarium/security/):

- **The DLP scan is telemetry, not containment.** A regex cannot stop a determined agent
  exfiltrating over an allow-listed channel. The credential's confidentiality rests on the
  firewall and boundary injection.
- **Running a Claude subscription credential through a programmatic MITM is against
  Anthropic's terms** for that token type. Use the API-key path unless you have explicit
  authorization.

## Contributing

```bash
make test   # every suite CI runs: python x 3 + cargo
make lint   # ruff · uv lock --check · clippy · tsc · eslint
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org/); releases,
versions and the changelog are automated from them.

→ [Development guide](https://oaisp.github.io/terrarium/development/)

## License

[Apache 2.0](LICENSE)
