# Hardened sandbox image for the Claude agent.
# Build context is the repo root:  docker build -f sandbox/Dockerfile -t terrarium-sandbox .

# --- Warden build stage (same Debian/glibc base as the runtime image) ---
FROM rust:1-slim-bookworm AS warden-build
RUN apt-get update && apt-get install -y --no-install-recommends cmake clang \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# Dependencies get their own cached layer. Warden pulls in rustls, tokio and aws-lc-rs —
# a few hundred crates and the bulk of the build — and keying that on the manifests alone
# means editing warden/src no longer recompiles all of it. The throwaway main.rs exists
# only to give cargo something to link; `touch` then forces the real one to rebuild, since
# cargo compares mtimes and the copied sources may be older than the dummy build.
COPY warden/Cargo.toml warden/Cargo.lock ./
RUN mkdir -p src && echo 'fn main() {}' > src/main.rs \
    && cargo build --release \
    && rm -rf src target/release/warden target/release/deps/warden*
COPY warden/src ./src
RUN touch src/main.rs && cargo build --release && strip target/release/warden

# --- CLI extract stage: pull the Claude Code CLI via npm, keep ONLY its self-contained
# native binary. The binary embeds its own runtime, so the runtime image needs no node. ---
FROM node:22-slim AS cli
RUN npm install -g @anthropic-ai/claude-code@2.1.220
RUN set -e; \
    bin="$(ls /usr/local/lib/node_modules/@anthropic-ai/claude-code-linux-*/claude 2>/dev/null | head -1)"; \
    [ -n "$bin" ] || bin="$(readlink -f "$(command -v claude)")"; \
    cp "$bin" /claude-bin && chmod 0755 /claude-bin

# --- Runtime: python (the worker) + the CLI binary + firewall/privilege tooling. No node. ---
FROM ghcr.io/astral-sh/uv:0.9.9 AS uv

FROM python:3.13-slim

# System deps: nftables/iptables (egress firewall), gosu (privilege drop),
# tini (PID 1 / signal reaping), ca-certs, curl (probes). python is the base image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        nftables iptables \
        gosu tini ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# The Claude Code CLI — the engine the Agent SDK drives (worker points cli_path here via
# PATH). Pinned 2.1.220 — the CLI that claude-agent-sdk 0.2.128 declares in its
# `_cli_version.py`. Keep this in lockstep with the SDK floor below: the SDK's bundled copy
# is stripped, so a drifting pin means the worker drives an untested CLI. Read the pin out
# of the SDK rather than taking npm's `latest` — npm's `stable` tag lags (2.1.212 at the
# time of this bump) and the SDK is the thing we actually drive the CLI with.
# (>=2.1.195 also emits structured workflow_progress — per-phase subagent batches — for the
# console's phased workflow view.)
COPY --from=cli /claude-bin /usr/local/bin/claude

# Python deps for the worker (the Agent SDK only; FastAPI etc. stay on the host). The SDK
# bundles its OWN copy of the CLI (~230 MB) which we never use — the worker drives the
# pinned binary above via cli_path — so strip it out.
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock /tmp/locked-deps/
RUN cd /tmp/locked-deps \
    && uv export --frozen --no-dev --no-emit-project --output-file requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt \
    && rm -rf /tmp/locked-deps /usr/local/bin/uv \
    && rm -rf "$(python3 -c 'import claude_agent_sdk,os;print(os.path.dirname(claude_agent_sdk.__file__))')/_bundled" \
    && find / -xdev -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Unprivileged user + runtime dirs. The orchestration code lives under a neutral
# /opt/runtime (not a project-named path) to avoid handing a compromised agent an
# obvious "I'm in Terrarium" fingerprint via `ls`.
RUN useradd -m -u 1001 -s /bin/sh agent \
    && mkdir -p /workspace /memory /opt/runtime \
    && chown -R agent:agent /workspace /memory

# Shared library (importable via PYTHONPATH) + worker. The worker runs as the agent
# uid, so worker.py + the lib are necessarily agent-readable.
COPY terracore /opt/runtime/terracore
COPY sandbox/worker.py /opt/runtime/worker.py
# Root-only scripts: the firewall ruleset + the entrypoint's deception logic must NOT
# be readable by the agent (they'd reveal the egress design). They run as root before
# the privilege drop, so 0700 root is sufficient and conceals them from uid 1001.
COPY sandbox/firewall.sh /opt/runtime/firewall.sh
COPY sandbox/entrypoint.sh /opt/runtime/entrypoint.sh
# the egress mediator (Rust) runs as a per-session sidecar from this image; named to
# read as a corporate web-gateway agent (matches the Zscaler-shaped interception CA).
COPY --from=warden-build /build/target/release/warden /opt/runtime/zstunnel
RUN chown root:root /opt/runtime/firewall.sh /opt/runtime/entrypoint.sh \
    && chmod 0700 /opt/runtime/firewall.sh /opt/runtime/entrypoint.sh \
    # Harden: the agent runs unprivileged, so strip setuid/setgid bits — no stray
    # setuid binary (su, mount, …) can serve as an in-sandbox privilege-escalation path.
    && find / -xdev -perm /6000 -type f -exec chmod -s {} + 2>/dev/null || true

ENV PYTHONPATH=/opt/runtime \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TERRA_SYSTEM=assistant \
    TERRA_MODEL=sonnet

WORKDIR /workspace
# tini reaps zombies and forwards signals; entrypoint installs the firewall
# then drops privileges.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/runtime/entrypoint.sh"]
