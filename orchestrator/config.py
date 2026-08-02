"""Host orchestrator configuration (env-overridable)."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from terracore.models import DEFAULT_MODEL

REPO_ROOT = Path(__file__).resolve().parent.parent


# Where file-mounted secrets land. This is the convention Docker Swarm, Kubernetes and
# shunt's `mode = "file"` all use, so a deployment that mounts secrets as files needs no
# per-orchestrator configuration to be understood.
SECRETS_DIR = Path(os.environ.get("TERRA_SECRETS_DIR", "/run/secrets"))


def _secret(name: str, default: str | None = None) -> str | None:
    """A secret from the environment, else from a file of that name in SECRETS_DIR.

    File-mounted secrets exist because an env var is visible in `docker inspect`, and
    therefore in anything that captures it — a monitoring agent, a bug report, an image made
    with `docker commit`. TERRA_TOKEN is the admin bearer for the whole API and TERRA_KEK
    unseals the credential store, so neither belongs in container metadata.

    The environment still wins when both are present: an operator overriding a value for one
    run should not be silently outranked by a file they forgot was mounted.
    """
    v = os.environ.get(name)
    if v:
        return v
    try:
        # Exactly the value, with no trailing newline — the file may have been written by an
        # editor that adds one, and a token with a stray \n fails auth in a way that looks
        # like a wrong token rather than a malformed one.
        return (SECRETS_DIR / name).read_text().strip() or default
    except OSError:
        return default


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v) if v else default


def _default_namespace() -> str:
    try:
        return Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text().strip() or "terrarium"
    except Exception:
        return "terrarium"


@dataclass
class Config:
    image: str = os.environ.get("TERRA_IMAGE", "terrarium-sandbox")
    network: str = os.environ.get("TERRA_NETWORK", "terrarium-net")
    memory_volume: str = os.environ.get("TERRA_MEMORY_VOLUME", "terrarium-memory")
    # Default for a session/agent that names no model. From the shared catalog (terracore.models)
    # so it can't drift from what the console offers.
    default_model: str = os.environ.get("TERRA_MODEL") or DEFAULT_MODEL
    # gVisor etc. — set TERRA_DOCKER_RUNTIME=runsc for stronger isolation.
    docker_runtime: str | None = os.environ.get("TERRA_DOCKER_RUNTIME") or None
    # "docker" (default) · "k8s" (sandbox Pods) · "local" (UNSAFE dev runner).
    runner: str = os.environ.get("TERRA_RUNNER", "docker")

    # Kubernetes runner (TERRA_RUNNER=k8s)
    k8s_namespace: str = os.environ.get("TERRA_K8S_NAMESPACE") or _default_namespace()
    k8s_storage_class: str | None = os.environ.get("TERRA_K8S_STORAGE_CLASS") or None
    k8s_memory_size: str = os.environ.get("TERRA_K8S_MEMORY_SIZE", "1Gi")
    k8s_workspace_size: str = os.environ.get("TERRA_K8S_WORKSPACE_SIZE", "2Gi")
    k8s_memory_emptydir_size: str = os.environ.get("TERRA_K8S_MEMORY_EMPTYDIR_SIZE", "256Mi")
    k8s_audit_size: str = os.environ.get("TERRA_K8S_AUDIT_SIZE", "256Mi")
    k8s_ephemeral_storage_limit: str = os.environ.get("TERRA_K8S_EPHEMERAL_STORAGE_LIMIT", "4Gi")
    # Compressed synchronized-memory snapshot accepted by the control plane.
    memory_snapshot_max_bytes: int = int(
        os.environ.get("TERRA_MEMORY_SNAPSHOT_MAX_BYTES", str(64 * 1024 * 1024))
    )
    # Admission bounds. Zero disables a bound; defaults keep one operator or a
    # faulty scheduler from exhausting the host/cluster.
    max_live_sessions: int = int(os.environ.get("TERRA_MAX_LIVE_SESSIONS", "32"))
    max_live_sessions_per_agent: int = int(
        os.environ.get("TERRA_MAX_LIVE_SESSIONS_PER_AGENT", "4")
    )
    # Per-session durable evidence bounds. Crossing a non-zero limit terminates
    # the producer rather than rotating/truncating an audit trail.
    max_event_log_bytes: int = int(
        os.environ.get("TERRA_MAX_EVENT_LOG_BYTES", str(256 * 1024 * 1024))
    )
    max_audit_log_bytes: int = int(
        os.environ.get("TERRA_MAX_AUDIT_LOG_BYTES", str(256 * 1024 * 1024))
    )
    # auth: API key (clean) or subscription creds. Either way Warden injects it at
    # egress so it never enters the sandbox.
    api_key: str | None = _secret("ANTHROPIC_API_KEY")

    # Warden: per-session Rust MITM egress mediator — the SOLE egress path for every
    # session. Credential injection (cred never in the sandbox) + DLP/injection scan +
    # allow-list/audit. gateway_allow seeds the allow-list (beyond always-allowed
    # Anthropic); it is then console-managed at runtime. There is no opt-out and no
    # flag: an unmediated sandbox is not a state this orchestrator can reach.
    warden_port: int = int(os.environ.get("TERRA_WARDEN_PORT", "8888"))
    warden_kek: str | None = _secret("TERRA_KEK")
    gateway_allow: tuple[str, ...] = tuple(
        h.strip() for h in (os.environ.get("TERRA_GATEWAY_ALLOW") or "").split(",") if h.strip()
    )

    skills_dir: Path = field(default_factory=lambda: _env_path("TERRA_SKILLS_DIR", REPO_ROOT / "skills"))
    logs_dir: Path = field(default_factory=lambda: _env_path("TERRA_LOGS_DIR", REPO_ROOT / "logs"))
    runtime_dir: Path = field(default_factory=lambda: _env_path("TERRA_RUNTIME_DIR", Path.home() / ".terrarium"))
    creds_path: Path = field(
        default_factory=lambda: _env_path("TERRA_CREDS", Path.home() / ".claude" / ".credentials.json")
    )
    # Set by the lifespan to the CredentialManager's in-memory accessor. When present,
    # sessions get the (decrypted) credential straight from orchestrator RAM, so no
    # plaintext credential copy is read off the PVC. None → fall back to creds_path.
    creds_provider: Callable[[], dict | None] | None = None

    # optional bearer token to protect the API (recommended if not pure localhost)
    auth_token: str | None = _secret("TERRA_TOKEN")
    # CORS allow-list (comma-separated origins) used only when a token is set;
    # empty → no cross-origin access (the console is same-origin via its own proxy).
    cors_origins: tuple[str, ...] = tuple(
        o.strip() for o in (os.environ.get("TERRA_CORS_ORIGINS") or "").split(",") if o.strip()
    )
    # allow an unauthenticated /metrics scrape (Prometheus) even when a token is set.
    metrics_public: bool = os.environ.get("TERRA_METRICS_PUBLIC") == "1"
    # explicit opt-in to run WITHOUT a token on a non-loopback bind (dangerous —
    # the API is then an unauthenticated admin surface). Default off: fail closed.
    allow_no_auth: bool = os.environ.get("TERRA_ALLOW_NO_AUTH") == "1"
    host: str = os.environ.get("TERRA_HOST", "127.0.0.1")

    # Orchestrator-side budget backstop: hard-kill a session once its cumulative
    # cost exceeds max_budget_usd * this multiplier — a true kill ABOVE the SDK's
    # own soft cap, so an unattended/scheduled agent can't run away on spend.
    budget_hard_mult: float = float(os.environ.get("TERRA_BUDGET_HARD_MULT", "1.25"))
    # Independent runaway backstop: when a budget is set, also hard-kill after this
    # many result events (turns). Both values cross the worker trust boundary:
    # protocol validation bounds their shape, but a compromised worker can omit
    # or fabricate them. This is a runaway guard, not a security boundary.
    budget_max_turns: int = int(os.environ.get("TERRA_BUDGET_MAX_TURNS", "1000"))
    # Runaway-turn backstop: hard-kill a session whose turn has been continuously
    # RUNNING for this many seconds without ever closing (no `result` event). The
    # cost/turn caps above only fire on `result` events, so an agent that streams
    # tokens forever without finishing a turn would evade them; this wall-clock bound
    # is measured on the host after a worker reports that a turn started. A
    # compromised worker can omit that transition, so this remains a best-effort
    # runaway guard. 0 disables it. Generous default (2h).
    budget_max_run_seconds: int = int(os.environ.get("TERRA_BUDGET_MAX_RUN_SECONDS", "7200"))

    # How often to mirror each live session's Warden egress audit onto the orchestrator's
    # runtime volume (k8s: the audit otherwise dies with the Pod, taking the verifiable
    # receipt chain with it). Also what bounds how stale the console's egress feed can be.
    # One pod-exec per live session per interval — independent of how many clients poll.
    # 0 disables the sweep (the stop/detach drains still run, so nothing is lost on a
    # clean teardown — only an abrupt Pod death would lose the tail).
    audit_drain_seconds: int = int(os.environ.get("TERRA_AUDIT_DRAIN_S", "10"))

    # Convenience: fire-and-forget webhook on session events (Discord/Slack/ntfy/…).
    notify_webhook_url: str | None = os.environ.get("TERRA_NOTIFY_WEBHOOK") or None
    notify_on: tuple[str, ...] = tuple(
        e.strip() for e in (os.environ.get("TERRA_NOTIFY_ON") or "session_end,error,budget_exceeded").split(",") if e.strip()
    )

    def __post_init__(self) -> None:
        if self.memory_snapshot_max_bytes <= 0:
            raise ValueError("memory_snapshot_max_bytes must be positive")
        for name in (
            "max_live_sessions", "max_live_sessions_per_agent",
            "max_event_log_bytes", "max_audit_log_bytes",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        # tolerant: in-cluster these point at mounted volumes (set via env);
        # the default paths may be read-only, which is fine.
        for d in (self.logs_dir, self.runtime_dir):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    @property
    def agents_path(self) -> Path:
        return self.runtime_dir / "agents.json"

    @property
    def schedules_path(self) -> Path:
        return self.runtime_dir / "schedules.json"

    @property
    def egress_profiles_path(self) -> Path:
        return self.runtime_dir / "egress_profiles.json"

    @property
    def environments_path(self) -> Path:
        return self.runtime_dir / "environments.json"

    @property
    def egress_dir(self) -> Path:
        # shared with Warden: policy.json (orchestrator writes) + audit/<sid>.jsonl
        # (Warden appends a per-session, individually-verifiable HMAC chain)
        return self.runtime_dir / "egress"

    @property
    def effective_kek(self) -> str | None:
        """Key that seals the credential store at rest. An explicit TERRA_KEK wins;
        otherwise it falls back to the API token (TERRA_TOKEN) so the store is
        encrypted by default without managing a separate key. Note: rotating the
        token then re-keys the store (it re-seals from the seed on next refresh) —
        set a dedicated TERRA_KEK if you want the KEK decoupled from auth."""
        return self.warden_kek or self.auth_token


def managed_creds(config: "Config") -> dict | None:
    """Current credentials.json content for session provisioning.

    Prefers the in-memory provider (the CredentialManager hands over the decrypted
    credential from RAM, so no plaintext lands on the PVC); falls back to reading the
    on-disk creds file for unmanaged/dev paths (no provider set).
    """
    prov = config.creds_provider
    if prov is not None:
        return prov()
    import json
    try:
        return json.loads(config.creds_path.read_text())
    except Exception:
        return None


# --- sandbox deception (defense-in-depth) ----------------------------------------
# Hide the egress mediation from the agent: a generic "corporate TLS-inspection
# proxy" CA path (not /warden-ca) + realistic decoy credentials. The real controls
# (firewall, Warden injection, no real cred in the sandbox) are unchanged — this only
# denies a compromised agent the fingerprints to know it's sandboxed/MITM'd.
AGENT_CA_DIR = "/etc/ssl/proxy-ca"
AGENT_CA_FILE = AGENT_CA_DIR + "/session-ca.pem"


def decoy_api_key() -> str:
    """A realistic-looking (but fake) Anthropic API key. Warden strips any inbound
    api-key/authorization header and injects the real credential, so the value is
    inert — its only job is to look real to the agent."""
    import secrets
    return "sk-ant-api03-" + secrets.token_urlsafe(72)[:95]


def decoy_oauth_token(kind: str = "oat01") -> str:
    """A realistic-looking (but fake) Anthropic OAuth token (subscription path)."""
    import secrets
    return f"sk-ant-{kind}-" + secrets.token_urlsafe(72)[:95]


def decoy_oauth_stub(subscription_type: str = "max") -> dict:
    """A decoy ``~/.claude/.credentials.json`` for the subscription path.

    Shapes the CLI's request like a real OAuth session (``Authorization: Bearer``
    + the ``anthropic-beta: …oauth-2025-04-20…`` header) so Warden can inject the
    real subscription token — an api-key-shaped decoy would make the CLI OMIT that
    beta header and the injected subscription token would be rejected.

    ``expiresAt`` is set ~13 months out: far enough that the CLI never attempts an
    in-session refresh (only the orchestrator can refresh — the sandbox can't), but
    a plausible epoch-ms value, NOT the old ``9999999999999`` sentinel (year ~2286)
    that an agent reading the file could instantly flag as synthetic."""
    import time
    expires_ms = int((time.time() + 400 * 24 * 3600) * 1000)  # ~13 months, non-sentinel
    return {"claudeAiOauth": {
        "accessToken": decoy_oauth_token("oat01"),
        "refreshToken": decoy_oauth_token("ort01"),
        "expiresAt": expires_ms,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": subscription_type,
    }}
