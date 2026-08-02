---
title: Security model
nav_order: 3
---

# Security model
{: .no_toc }

1. TOC
{:toc}

---

Isolation is layered, and each layer holds independently. The credential's confidentiality
does not rest on any single control.

```
   agent code (untrusted)
        │
   ┌────┴──────────────────────────────────────────────────────────────┐
   │ 1. Process     non-root (uid 1001) · CapEff=0 · seccomp            │
   │ 2. Mounts      no service-account token · read-only where possible │
   │                · setuid bits stripped from the image               │
   │ 3. Chokepoint  nftables OUTPUT default-DROP. k8s permits Warden's  │
   │                uid; Docker permits only the per-session Warden     │
   │                IP/port. DNS cut. L3/L4 only, so Warden is          │
   │                unbypassable                                        │
   │ 4. Policy      Warden holds all destination policy — domain / IP / │
   │                CIDR allow · deny · inspect · kill · private-range  │
   │                and metadata floors · signed audit                  │
   │ 5. Credential  sandbox holds a decoy; Warden gets the real token   │
   │                from host state (Docker) or a per-Pod Secret (k8s)  │
   │ 6. Hygiene     mediation details removed from the child env.       │
   │                Noise reduction, not a boundary                     │
   └────────────────────────────────────────────────────────────────────┘
```

## What an agent cannot do

| | Why |
|---|---|
| **Reach a raw IP** | The kernel drops every destination but the Warden pinhole. Warden denies CONNECT targets not covered by policy; bare IPs and private/metadata ranges are denied by default. |
| **Resolve a host itself** | DNS is cut. Warden resolves and dials exactly the evaluated address. |
| **Read the real credential** | It is not in the sandbox's environment, filesystem or memory. Only Warden holds it. |
| **Modify the firewall** | No `CAP_NET_ADMIN` after the entrypoint drops privileges. |
| **Escalate to the cluster** | No mounted service-account token. |

## Egress

Warden is mandatory. There is no unmediated path and no flag that creates one: the manifest
builder cannot emit a sandbox without the sidecar, and the Docker runner refuses to launch if
the sidecar did not come up.

Warden enforces destination policy — domains, IP literals or CIDRs — against the *resolved*
address, which makes it rebinding-safe. The private-range block and cloud-metadata deny are
defaults that an explicit allow CIDR can lift; metadata additionally requires
`allow_metadata`. Allowing an agent to reach `10.20.0.0/16` is one policy entry, and the
traffic stays tunneled, audited and kill-switchable.

Every session routes through its own Warden, API-key and subscription alike, so the real
credential never enters the sandbox in any mode.

## DLP scanning is telemetry, not containment
{: .warning }

The DLP pass is a compact regex scan. It cannot stop a motivated agent from exfiltrating over
an allow-listed channel: base64, gzip, field-splitting or the prompt body itself all defeat a
regex.

| Traffic | Behaviour |
|---|---|
| To an untrusted, non-credentialed host | Blocked in enforce mode |
| To the credentialed LLM host | Logged, not blocked |

The credential's confidentiality rests on layers 3–5 above, not on this. DLP narrows the easy
exfiltration paths and makes the rest auditable.

## Audit receipts

Warden signs an HMAC-chained receipt for every connection decision, one chain per session. The
orchestrator persists the per-session key and drains the chain onto its own volume on every
runner, so verification works after the sandbox is gone:

```bash
terra-cli verify-egress <session-id>
```

## Test coverage

| Scope | Status |
|---|---|
| Warden logic — allow-list, SSRF guard, credential injection, audit chain | `cargo test` in CI |
| Control plane — mediation cannot be disabled, stores are `0600`, download refuses symlinks and traversal | Python unit suite in CI |
| Firewall pinhole, sandbox isolation, concealment | `make redteam*`, manual — needs a built sandbox image |
| On-cluster behaviour | Spot-checked, not continuously asserted |

## Terms of service
{: .warning }

Running a Claude **subscription** credential through a programmatic MITM is against
Anthropic's terms for that token type. Terrarium supports the subscription path because it was
explicitly authorized for the single-operator deployment it was built for.

Without that authorization, use the API-key path (`ANTHROPIC_API_KEY`), which carries no such
restriction. Decide before you deploy.

## Reporting a vulnerability

Open a [security advisory](https://github.com/OAISP/terrarium/security/advisories/new) rather
than a public issue.
