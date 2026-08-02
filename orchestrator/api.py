"""FastAPI control plane (single-tenant, localhost by default).

The event envelope returned by the SSE stream is the same JSONL schema the
worker emits and the web inspector reads — the durable log *is* the wire format.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional, Union

from fastapi import Depends, FastAPI, File, Form, HTTPException, Header, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, create_model, field_validator

from terracore import models, templates, toolset
from terracore.events import tail_events
from terracore.harness import HARNESS_FIELDS, Harness
from terracore.protocol import PROTOCOL_VERSION

from . import filebridge, metrics
from .agents import AgentStore
from .config import Config
from .manager import CapacityError, SessionManager
from .registry import LIVE_STATUSES
from .runners import SessionConfig
from .schedules import ScheduleStore, Scheduler
from .tokens import Principal, TokenStore
from .egress import EgressPolicyStore, read_audit, session_audit_path
from .egress_profiles import EgressProfileStore
from .environments import EnvironmentStore
from .migrations import migrate_agent_egress_pins, migrate_pin_memory_mode

def _orchestrator_version() -> str:
    """Read the installed package version so the OpenAPI version never drifts from a
    hardcoded literal. Falls back to 0+unknown when running from a source tree."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("terrarium-orchestrator")
    except PackageNotFoundError:
        return "0+unknown"


# The CLI's permission modes — validated at the API boundary so a typo is a loud 422
# (and never reaches the worker, where a bad mode silently mis-applies live).
PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]
# How /memory is provided. "volume" mounts the per-agent PVC (durable, but ~11s of Longhorn
# attach per launch on k8s); "synced" snapshots it in/out around a fast volume-less pod;
# "none" gives container-local scratch. See Harness.memory_mode.
MemoryMode = Literal["volume", "synced", "none"]


def _agents_have_required_keys(cls, v):
    """Fail at request time (422) rather than at session start: the SDK's AgentDefinition
    requires description + prompt. Module-level so both the create and the derived PATCH
    model can bind it (create_model can't inherit a validator off a field source)."""
    for name, spec in (v or {}).items():
        missing = [k for k in ("description", "prompt") if not (spec or {}).get(k)]
        if missing:
            raise ValueError(f"agent {name!r} is missing {', '.join(missing)}")
    return v


class HarnessRequest(BaseModel):
    """The full configurable harness — shared by agent + inline-session create.

    ``extra='forbid'`` so an unknown or renamed field is a loud 422 rather than a
    silent no-op — a dropped harness field is otherwise invisible end-to-end.
    """

    model_config = ConfigDict(extra="forbid")

    model: Optional[str] = None
    system_mode: str = "assistant"
    custom_prompt: Optional[str] = None
    permission_mode: PermissionMode = "bypassPermissions"
    allowed_tools: Optional[list[str]] = None      # auto-approve list (does NOT change availability)
    builtin_tools: Union[list[str], dict, None] = None  # availability allowlist: base set of built-ins
    #                                                      (None=all defaults; list=only these; {preset})
    thinking: Optional[dict] = None          # reasoning/effort, e.g. {"type":"adaptive"}
    effort: Optional[str] = None             # CLI "thinking level": low|medium|high|xhigh|max
    fallback_model: Optional[str] = None     # model to retry with on overload/refusal
    max_thinking_tokens: Optional[int] = None
    betas: Optional[list[str]] = None        # API beta flags to opt into
    max_turns: Optional[int] = None
    max_budget_usd: Optional[float] = None
    mcp_servers: Optional[dict] = None       # name -> server config
    client_tools: Optional[list[dict]] = None  # SDK-bridged tool schemas [{name,description,input_schema}]
    agents: Optional[dict[str, dict]] = None   # programmatic subagents: name -> {description, prompt, ...}
    # "all"/[names]/[] = the SDK skills option (enables the Skill tool); a bool is the legacy mount
    # knob that does NOT enable it. Defaults to "all" so built-in skills work out of the box; [] is
    # the "bare harness" that hides even the CLI's built-ins.
    skills: Union[bool, list[str], Literal["all"]] = "all"
    interactive: bool = False                # allow the agent to ask the operator questions (AskUserQuestion)
    approval: Union[str, list[str]] = "off"  # tool-approval scope: "off"|"edits"|"all"|[tools] (interactive only)
    setting_sources: Optional[list[str]] = None
    env: Optional[dict] = None
    memory_mode: MemoryMode = "synced"
    environments: Optional[list[str]] = None # attach to {secrets, egress} bundles — the sole per-agent
    #                                          egress + secret-scoping mechanism (there is no direct egress pin)
    extra_options: Optional[dict] = None     # forward-compatible SDK fields; managed keys rejected

    _check_agents = field_validator("agents")(_agents_have_required_keys)

    def to_harness(self, default_model: str) -> Harness:
        return Harness(
            model=self.model or default_model,
            system_mode=self.system_mode,
            custom_prompt=self.custom_prompt,
            permission_mode=self.permission_mode,
            allowed_tools=self.allowed_tools,
            builtin_tools=self.builtin_tools,
            thinking=self.thinking,
            effort=self.effort,
            fallback_model=self.fallback_model,
            max_thinking_tokens=self.max_thinking_tokens,
            betas=self.betas,
            max_turns=self.max_turns,
            max_budget_usd=self.max_budget_usd,
            mcp_servers=self.mcp_servers,
            client_tools=self.client_tools,
            agents=self.agents,
            skills=self.skills,
            interactive=self.interactive,
            approval=self.approval,
            setting_sources=self.setting_sources,
            env=self.env,
            memory_mode=self.memory_mode,
            environments=self.environments,
            extra_options=self.extra_options,
        )


class CreateAgentRequest(HarnessRequest):
    name: str
    memory_scope: Optional[str] = None
    template: Optional[str] = None   # seed the harness from a built-in template


class ScheduleRequest(BaseModel):
    name: str
    agent_id: str
    prompt: str
    cron: str                        # 5-field cron (minute hour dom month dow)
    enabled: bool = True
    max_budget_usd: Optional[float] = None


class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    agent_id: Optional[str] = None
    prompt: Optional[str] = None
    cron: Optional[str] = None
    enabled: Optional[bool] = None
    max_budget_usd: Optional[float] = None


# Harness fields that are SESSION-scoped: request-only, never persisted on an agent, and
# overlaid onto an agent's snapshot when a session binds via agent_id. client_tools qualify
# because their handlers run in the SDK client's process — they can only arrive per-session.
# (Agent-level fields — model, environments, allowed_tools, … — come from the agent and are
# NOT overridden by the session request.)
SESSION_SCOPED_HARNESS_FIELDS = ("client_tools",)

# /v1/logs fans out across sessions and reads a window of each one's history. These bound
# that work; the response reports whichever of them actually bit (see "truncated").
LOG_SCAN_SESSIONS = 40        # matching sessions opened, newest first
LOG_EVENTS_PER_SESSION = 500  # tail of each session's event log
LOG_AUDIT_PER_SESSION = 300   # tail of each session's egress audit

# Max size of a file uploaded straight into a session workspace (decoded bytes). The upload
# middleware pre-rejects on Content-Length; the handler enforces this exactly on the body.
UPLOAD_MAX_BYTES = 25 * 1024 * 1024


def apply_session_overlay(sess: "SessionConfig", body: "HarnessRequest") -> "SessionConfig":
    """Overlay a session request's SESSION-scoped harness fields onto an agent snapshot.
    The agent owns its durable config; only request-only fields (client_tools, whose handlers
    run in the SDK client) are layered on. Mutates and returns ``sess``."""
    overlay = {f: getattr(body, f) for f in SESSION_SCOPED_HARNESS_FIELDS if getattr(body, f, None)}
    if overlay:
        sess.harness = replace(sess.harness, **overlay)
    return sess


class CreateSessionRequest(HarnessRequest):
    agent_id: Optional[str] = None  # reference a registered agent (overrides inline harness)
    title: Optional[str] = None


# The agent PATCH body: every harness field, all optional, so ``exclude_unset`` can tell
# "omitted" from an explicit null (which means clear). DERIVED from HarnessRequest, never
# re-typed — a hand-kept second copy of the field list drifts, and a field missing here is
# accepted by the API and silently discarded rather than rejected.
UpdateAgentRequest = create_model(
    "UpdateAgentRequest",
    __config__=ConfigDict(extra="forbid"),
    # Bound explicitly — create_model doesn't inherit validators from a field source.
    __validators__={"_check_agents": field_validator("agents")(_agents_have_required_keys)},
    # Identity fields live on the AgentSpec, not the harness.
    name=(Optional[str], None),
    memory_scope=(Optional[str], None),
    **{
        fname: (Optional[f.annotation], None)
        for fname, f in HarnessRequest.model_fields.items()
        # client_tools' handlers run in the SDK client's process, so they can only ever
        # arrive per-session; an agent has nowhere to run them.
        if fname not in SESSION_SCOPED_HARNESS_FIELDS
    },
)


class MessageRequest(BaseModel):
    text: str = ""
    # a list of Anthropic content blocks (text + image) for a vision/computer-use user turn;
    # takes precedence over `text` when present.
    content: Union[str, list, None] = None


class AnswerRequest(BaseModel):
    question_id: str
    answers: dict[str, Any]         # question text → chosen label (or list of labels / free text)


class DecisionRequest(BaseModel):
    request_id: str
    decision: str                   # allow | always | deny


class ClientToolResultRequest(BaseModel):
    call_id: str
    # a string OR a list of Anthropic content blocks (so a client tool can return an image)
    content: Union[str, list] = ""
    is_error: bool = False


class ReconfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Optional[str] = None                    # switch the model live (set_model)
    permission_mode: Optional[PermissionMode] = None  # switch the CLI permission mode live (set_permission_mode)


class RewindRequest(BaseModel):
    message_id: str                 # an SDK user-message uuid (from a rewind_point event)
    mode: str = "files"             # files | conversation | both


def _log_detail(ev: dict[str, Any]) -> str:
    """A short human summary of a session event for the unified Logs view."""
    t = ev.get("type")
    if t in ("user", "assistant_text", "thinking"):
        return str(ev.get("text", ""))[:300]
    if t == "tool_use":
        return f'{ev.get("name", "tool")} {json.dumps(ev.get("input", {}), default=str)[:200]}'
    if t == "tool_result":
        return ("error: " if ev.get("is_error") else "") + str(ev.get("content", ""))[:200]
    if t == "result":
        u = ev.get("usage") or {}
        # Show terminal_reason only when it ISN'T a clean finish — "completed" on every row
        # is noise, but "aborted_tools" is the whole story of that turn.
        why = ev.get("terminal_reason")
        suffix = f' · {why}' if why and why != "completed" else ""
        return f'${ev.get("total_cost_usd", 0)} · {u.get("output_tokens", 0)} out tok{suffix}'
    if t == "system":
        return str(ev.get("subtype", ""))
    if t == "error":
        return str(ev.get("message", ""))[:200]
    if t == "rewound":
        return f'rewound {ev.get("mode", "")}'
    return ""


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config()

    def _read_all_audits(limit: int) -> list[dict[str, Any]]:
        """Tail every session's audit file. Each read is O(limit), not O(file size), so a
        hostile agent flooding denials can't make this expensive (see read_audit)."""
        rows: list[dict[str, Any]] = []
        audit_dir = config.egress_dir / "audit"
        if audit_dir.is_dir():
            for f in sorted(audit_dir.glob("*.jsonl")):
                for row in read_audit(f, limit):
                    row.setdefault("session_id", f.stem)
                    rows.append(row)
        return rows

    # Fail closed: when no token is set, the auth model collapses to open-admin
    # (anonymous callers get the admin principal). That's fine on loopback (dev),
    # but a non-loopback bind would expose an unauthenticated admin API. Refuse to
    # start unless the operator explicitly opts in.
    _host = (config.host or "").lower()
    _loopback = _host in ("127.0.0.1", "localhost", "::1", "") or _host.startswith("127.")
    if not config.auth_token and not config.allow_no_auth and not _loopback:
        raise RuntimeError(
            f"refusing to start: TERRA_HOST={config.host!r} is non-loopback but no TERRA_TOKEN is set — "
            "the API would be an unauthenticated admin surface. Set TERRA_TOKEN, or set "
            "TERRA_ALLOW_NO_AUTH=1 to bind an open API anyway (dangerous)."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = SessionManager(config)
        app.state.tokens = TokenStore(config.runtime_dir / "tokens.json")
        # Egress policy store: the single source of truth Warden reads (per-session)
        # and the console Egress tab edits. Seeds policy.json from the allow-list.
        app.state.egress = EgressPolicyStore(config.egress_dir / "policy.json", seed_allow=config.gateway_allow)
        app.state.profiles = EgressProfileStore(config.egress_profiles_path)
        # Environments: named {secrets, egress profile} bundles an agent attaches to.
        # Egress is expressed ONLY through environments now (no per-agent egress pin).
        app.state.environments = EnvironmentStore(config.environments_path)
        # Migrate any legacy per-agent egress_profile pin → an attached environment BEFORE
        # AgentStore loads (from_dict would drop the now-removed field). Idempotent.
        # Freeze pre-existing agents on the OLD memory default before the new one ("synced")
        # would silently point them at an empty snapshot store. Must run before any session starts.
        n_pinned = migrate_pin_memory_mode(config.agents_path)
        if n_pinned:
            logging.getLogger("terrarium").info(
                "pinned %d pre-existing agent(s) to memory_mode=volume (new agents default to synced)", n_pinned)
        n_migrated = migrate_agent_egress_pins(config.agents_path, app.state.environments, app.state.profiles)
        if n_migrated:
            logging.getLogger("terrarium").info("migrated %d agent egress pin(s) to environments", n_migrated)
        app.state.agents = AgentStore(config.agents_path)
        # The manager resolves each session's effective policy (environments, else global).
        app.state.manager.egress_store = app.state.egress
        app.state.manager.profile_store = app.state.profiles
        app.state.manager.environment_store = app.state.environments
        # Operator injection secrets (the general bearer-swap): values sealed in a vault,
        # injected by Warden at the boundary — never in the sandbox. Needs a KEK (TERRA_KEK,
        # else the TERRA_TOKEN fallback); without one, the Secrets API is unavailable.
        app.state.secrets = None
        if config.effective_kek:
            from .secret_store import SecretStore
            from .secrets import UserSecretStore
            _vault = SecretStore(config.runtime_dir / "secrets-vault.json", config.effective_kek)
            app.state.secrets = UserSecretStore(config.runtime_dir / "secrets-index.json", _vault)
            app.state.manager.secret_store = app.state.secrets
            # H1: warn in ALL auth modes (not just subscription) when the vault holds secrets
            # sealed under the TERRA_TOKEN fallback. Unlike the credential store, operator
            # secrets have NO seed to re-derive from, so rotating TERRA_TOKEN PERMANENTLY loses
            # them. Only warn when secrets actually exist, to avoid startup noise otherwise.
            if not config.warden_kek and app.state.secrets.list():
                logging.getLogger("terrarium").warning(
                    "operator-secret vault is sealed under the TERRA_TOKEN fallback (no TERRA_KEK "
                    "set) — rotating the API token will make every stored operator secret "
                    "permanently undecryptable (there is no seed to recover them); set TERRA_KEK"
                )
        app.state.creds = None
        app.state.ready = False
        # Reattach to sandbox containers/Pods that survived this orchestrator's
        # restart (docker + k8s) ...
        kept = await app.state.manager.rehydrate()
        if config.runner == "k8s":
            from .k8s_runner import cleanup_orphans

            # ... then reap only the true orphans (spare the reattached survivors).
            await asyncio.to_thread(cleanup_orphans, config, kept)

        # Subscription credential manager: keeps the OAuth token alive and hands
        # each sandbox a freshly-refreshed credentials.json. Always on in k8s
        # subscription mode — the credential may be seeded from the secret mount
        # OR set via the API/console (POST /v1/credentials).
        # Run the refresh/rotation chain for ANY runner in subscription mode (was
        # k8s-only, so a long Docker+Warden session 401'd when its token expired).
        if not config.api_key:
            from .credentials import CredentialManager

            # F24: surface the KEK fallback once — without TERRA_KEK the store is sealed
            # under the API token, so rotating TERRA_TOKEN re-keys it (set TERRA_KEK to decouple).
            if not config.warden_kek and config.effective_kek:
                logging.getLogger("terrarium").warning(
                    "credential store KEK falls back to TERRA_TOKEN (no TERRA_KEK set) — "
                    "rotating the API token will re-key the store; set TERRA_KEK to decouple"
                )
            seed = config.creds_path if (config.creds_path and config.creds_path.exists()) else None
            mgr = CredentialManager(seed_path=seed, store_path=config.runtime_dir / "credentials.json",
                                    kek=config.effective_kek)
            # Sessions get the decrypted credential from RAM (no plaintext copy read off
            # the PVC); with TERRA_KEK set the durable store is sealed, else plaintext.
            config.creds_provider = mgr.current_creds
            # when the credential rotates/changes, push it to running sessions' Warden
            mgr.on_change = app.state.manager.propagate_credentials
            app.state.creds = mgr
            try:
                await mgr.start()
            except Exception as e:  # never block startup on creds
                logging.getLogger("terrarium").error("credential manager start: %s", e)
            logging.getLogger("terrarium").info("credential manager active (present=%s)", mgr.status().get("present"))
        # recurring-agent scheduler (cron-driven sessions)
        app.state.schedules = ScheduleStore(config.schedules_path)
        app.state.scheduler = Scheduler(
            store=app.state.schedules, manager=app.state.manager, agents=app.state.agents
        )
        app.state.scheduler.start()
        # Periodic sweeps: the runtime backstop (hard-kills a turn wedged with NO events, which
        # the event-driven budget check can't catch) and the egress-audit drain (mirrors each
        # live session's Warden audit onto our volume so the receipt chain outlives the sandbox).
        app.state.manager.start_background()
        # M4: egress policy is NOT part of a session's persisted harness_json, so a
        # reattached session's Warden still runs whatever policy was live before the
        # restart. Re-push the current effective policy to every reattached session so a
        # kill-switch flip / allow-list tightening made while the orchestrator was DOWN
        # actually reaches them (otherwise it waits for the next live edit).
        try:
            await app.state.manager.propagate_egress_policy()
        except Exception as e:  # never block startup on propagation
            logging.getLogger("terrarium").warning("post-rehydrate policy propagation: %s", e)
        # Fail readiness on a missing sandbox image rather than letting the first session
        # discover it. A deploy that reports success and then cannot launch an agent is the
        # worst of both: shunt goes green, and the error names Warden.
        from .runners import preflight_image
        app.state.image_error = await preflight_image(config)
        if app.state.image_error:
            logging.getLogger("terrarium").error("%s", app.state.image_error)
        app.state.ready = True
        try:
            yield
        finally:
            await app.state.scheduler.stop()
            if app.state.creds:
                await app.state.creds.stop()
            await app.state.manager.shutdown()

    app = FastAPI(title="terrarium orchestrator", version=_orchestrator_version(), lifespan=lifespan)
    # CORS: wildcard only in open-localhost dev (no token). Once a token is set
    # (i.e. the API may be bound off-loopback) default to NO cross-origin access
    # so a leaked bearer in some other site's JS can't drive the API; an operator
    # can allow-list explicit origins via TERRA_CORS_ORIGINS (comma-separated).
    cors_origins = config.cors_origins if config.auth_token else ["*"]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def _stamp_protocol(request: Request, call_next):
        # Reject an oversized upload on its declared Content-Length HERE — before Starlette parses
        # (and spools to disk) the multipart body when the endpoint's file param resolves. Doing it
        # in the handler is too late (the spool already happened), so a run-scoped caller could
        # force an unbounded spool on the shared volume. The handler still enforces the exact 25 MiB
        # cap on the decoded bytes; multipart framing adds a little overhead, hence the small slack.
        if request.url.path.endswith("/files/upload"):
            clen = request.headers.get("content-length")
            if clen and clen.isdigit() and int(clen) > UPLOAD_MAX_BYTES + (1 << 16):
                return JSONResponse({"detail": "file too large (max 25 MiB)"}, status_code=413)
        # Advertise the wire protocol on every response so a client (the SDK) can detect
        # version skew and warn, instead of failing on a shape mismatch downstream.
        resp = await call_next(request)
        resp.headers["X-Terrarium-Protocol"] = str(PROTOCOL_VERSION)
        return resp

    def _principal(request: Request, authorization: str | None) -> Principal:
        # no root token configured → open localhost dev: full admin
        if not config.auth_token:
            return Principal("local", {"admin"})
        raw = authorization[7:] if (authorization and authorization.startswith("Bearer ")) else None
        if raw and hmac.compare_digest(raw, config.auth_token):  # timing-safe — root is the strongest cred
            return Principal("root", {"admin"})
        if raw:
            store = getattr(request.app.state, "tokens", None)
            p = store.verify(raw) if store else None
            if p is not None:
                return p
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    def require(*scopes: str):
        async def dep(request: Request, authorization: str | None = Header(default=None)) -> Principal:
            p = _principal(request, authorization)
            if not all(p.has(s) for s in scopes):
                raise HTTPException(status_code=403, detail=f"requires scope: {' '.join(scopes)}")
            return p
        return dep

    def manager(request: Request) -> SessionManager:
        return request.app.state.manager

    def agents(request: Request) -> AgentStore:
        return request.app.state.agents

    def _validate_environment_refs(request: Request, env_ids: list[str] | None) -> None:
        missing = [eid for eid in (env_ids or []) if request.app.state.environments.get(eid) is None]
        if missing:
            raise HTTPException(status_code=422, detail=f"unknown environment id(s): {missing}")

    def _get(request: Request, sid: str):
        s = manager(request).get(sid)
        if not s:
            raise HTTPException(status_code=404, detail="session not found")
        return s

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        # liveness — always 200 while the process is up
        return {
            "ok": True,
            "ready": getattr(request.app.state, "ready", True),
            "runner": config.runner,
            "image": config.image,
        }

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, Any]:
        # readiness — 503 until rehydrate + orphan-reap finish, so traffic isn't
        # routed mid-recovery (the chart points its readinessProbe here).
        if not getattr(request.app.state, "ready", False):
            raise HTTPException(status_code=503, detail="not ready (recovering sessions)")
        err = getattr(request.app.state, "image_error", None)
        if err:
            raise HTTPException(status_code=503, detail=err)
        return {"ready": True}

    @app.get("/v1/me")
    async def current_principal(
        request: Request, authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        p = _principal(request, authorization)
        return {
            "name": p.name,
            "scopes": sorted(p.scopes),
            "can": {scope: p.has(scope) for scope in ("read", "run", "admin")},
        }

    @app.get("/metrics")
    async def metrics_endpoint(request: Request, authorization: str | None = Header(default=None)) -> PlainTextResponse:
        # Exposes cumulative spend/usage — require a valid token once auth is on,
        # unless TERRA_METRICS_PUBLIC=1 (for an unauthenticated Prometheus scrape).
        if config.auth_token and not config.metrics_public:
            _principal(request, authorization)
        return PlainTextResponse(
            metrics.render(request.app.state.manager.registry), media_type=metrics.CONTENT_TYPE
        )

    @app.get("/v1/fleet", dependencies=[Depends(require("read"))])
    async def fleet(request: Request) -> dict[str, Any]:
        mgr = manager(request)
        rows = mgr.registry.list()
        creds = getattr(request.app.state, "creds", None)
        return {
            "running": sum(1 for r in rows if r.get("status") in LIVE_STATUSES),
            "total": len(rows),
            "spend_usd": round(sum(float(r.get("total_cost_usd") or 0) for r in rows), 6),
            "ready": getattr(request.app.state, "ready", True),
            "runner": config.runner,
            "credential": creds.status() if creds else {"managed": False},
        }

    # ---- subscription credential (set/rotate the Claude token from the console) ----
    @app.get("/v1/credentials/status", dependencies=[Depends(require("read"))])
    async def credentials_status(request: Request) -> dict[str, Any]:
        mgr = getattr(request.app.state, "creds", None)
        if mgr is None:
            return {"managed": False}
        return {"managed": True, **mgr.status()}

    @app.post("/v1/credentials", dependencies=[Depends(require("admin"))])
    async def set_credentials(body: dict[str, Any], request: Request) -> dict[str, Any]:
        mgr = getattr(request.app.state, "creds", None)
        if mgr is None:
            raise HTTPException(status_code=400, detail="credential manager not active (not k8s subscription mode)")
        raw = body.get("credentials")
        if raw in (None, ""):
            raise HTTPException(status_code=400, detail="missing 'credentials' (paste your ~/.claude/.credentials.json)")
        try:
            st = await mgr.set_credentials(raw)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid credentials: {e}")
        return {"ok": True, **st}

    @app.delete("/v1/credentials", dependencies=[Depends(require("admin"))])
    async def clear_credentials(request: Request) -> dict[str, Any]:
        mgr = getattr(request.app.state, "creds", None)
        if mgr is not None:
            await mgr.clear()
            return {"ok": True, **mgr.status()}
        return {"ok": True}

    # ---- scoped API tokens (admin only) ----
    @app.post("/v1/tokens", dependencies=[Depends(require("admin"))])
    async def create_token(body: dict[str, Any], request: Request) -> dict[str, Any]:
        name = (body.get("name") or "").strip()
        scopes = body.get("scopes") or ["run"]
        if not name:
            raise HTTPException(status_code=400, detail="missing 'name'")
        rec, raw = request.app.state.tokens.create(name, scopes)
        # raw token returned ONCE — never stored or shown again
        return {**rec.public(), "token": raw}

    @app.get("/v1/tokens", dependencies=[Depends(require("admin"))])
    async def list_tokens(request: Request) -> dict[str, Any]:
        return {"tokens": request.app.state.tokens.list()}

    @app.delete("/v1/tokens/{tid}", dependencies=[Depends(require("admin"))])
    async def delete_token(tid: str, request: Request) -> dict[str, Any]:
        if not request.app.state.tokens.delete(tid):
            raise HTTPException(status_code=404, detail="token not found")
        return {"ok": True}

    # ---- operator injection secrets (admin only): host-scoped header credentials Warden
    #      injects at the egress boundary; the value never enters the sandbox ----
    def _secrets(request: Request):
        st = getattr(request.app.state, "secrets", None)
        if st is None:
            raise HTTPException(status_code=503, detail="secret store unavailable (set TERRA_KEK or TERRA_TOKEN)")
        return st

    @app.get("/v1/secrets", dependencies=[Depends(require("admin"))])
    async def list_secrets(request: Request) -> dict[str, Any]:
        return {"secrets": _secrets(request).list()}

    @app.post("/v1/secrets", dependencies=[Depends(require("admin"))])
    async def upsert_secret(body: dict[str, Any], request: Request) -> dict[str, Any]:
        # upsert by name — value is required to create, optional to edit (keeps the existing)
        try:
            rec = _secrets(request).put(
                body.get("name") or "",
                scopes=body.get("scopes") or [],
                header=body.get("header") or "Authorization",
                template=body.get("template") or "Bearer {value}",
                value=body.get("value"),
                enabled=bool(body.get("enabled", True)),
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        await request.app.state.manager.propagate_secrets()
        return rec

    @app.delete("/v1/secrets/{name}", dependencies=[Depends(require("admin"))])
    async def delete_secret(name: str, request: Request) -> dict[str, Any]:
        if not _secrets(request).delete(name):
            raise HTTPException(status_code=404, detail="secret not found")
        await request.app.state.manager.propagate_secrets()
        return {"deleted": name}

    # ---- egress policy (Warden's allow/deny list + audit) ----
    @app.get("/v1/egress/policy", dependencies=[Depends(require("read"))])
    async def get_egress_policy(request: Request) -> dict[str, Any]:
        return {**request.app.state.egress.get(), "warden_port": config.warden_port}

    @app.put("/v1/egress/policy", dependencies=[Depends(require("admin"))])
    async def set_egress_policy(body: dict[str, Any], request: Request) -> dict[str, Any]:
        try:
            updated = request.app.state.egress.set(
                mode=body.get("mode"), rules=body.get("rules"), hosts=body.get("hosts"),
                kill=body.get("kill"), allow_metadata=body.get("allow_metadata"),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # propagate to RUNNING sessions' Warden (k8s ConfigMaps) so the change isn't
        # limited to new sessions — Warden hot-reloads the mounted policy within ~60s.
        # (re-resolves every session: the global kill switch must reach profiled ones too)
        await manager(request).propagate_egress_policy()
        return {**updated, "warden_port": config.warden_port}

    # ---- egress profiles (named allow/deny/inspect bundles, assignable per agent) ----
    @app.get("/v1/egress/presets", dependencies=[Depends(require("read"))])
    async def list_egress_presets() -> dict[str, Any]:
        from .egress_profiles import list_presets
        return {"presets": list_presets()}

    @app.get("/v1/egress/profiles", dependencies=[Depends(require("read"))])
    async def list_egress_profiles(request: Request) -> dict[str, Any]:
        return {"profiles": request.app.state.profiles.list()}

    @app.post("/v1/egress/profiles", dependencies=[Depends(require("admin"))])
    async def create_egress_profile(body: dict[str, Any], request: Request) -> dict[str, Any]:
        # `preset` instantiates a built-in bundle (name/rules may still be overridden in body).
        preset = body.get("preset")
        if preset:
            try:
                return request.app.state.profiles.create_preset(preset, name=(body.get("name") or "").strip() or None)
            except KeyError as e:
                raise HTTPException(status_code=400, detail=str(e))
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="missing 'name'")
        return request.app.state.profiles.create(
            name=name, mode=body.get("mode", "enforce"), rules=body.get("rules"), hosts=body.get("hosts"))

    @app.patch("/v1/egress/profiles/{pid}", dependencies=[Depends(require("admin"))])
    async def update_egress_profile(pid: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
        prof = request.app.state.profiles.update(
            pid, name=body.get("name"), mode=body.get("mode"), rules=body.get("rules"), hosts=body.get("hosts"))
        if not prof:
            raise HTTPException(status_code=404, detail="profile not found")
        # push the edited rules to RUNNING sessions pinned to THIS profile
        await manager(request).propagate_egress_policy(profile_id=pid)
        return prof

    @app.delete("/v1/egress/profiles/{pid}", dependencies=[Depends(require("admin"))])
    async def delete_egress_profile(pid: str, request: Request) -> dict[str, Any]:
        refs = [e["id"] for e in request.app.state.environments.list()
                if e.get("egress_profile") == pid]
        if refs:
            raise HTTPException(
                status_code=409,
                detail=f"profile is referenced by environment(s): {refs}; detach or replace it first",
            )
        if not request.app.state.profiles.delete(pid):
            raise HTTPException(status_code=404, detail="profile not found")
        return {"ok": True}

    # ---- environments (named {secrets, egress profile} bundles, attachable per agent) ----
    @app.get("/v1/environments", dependencies=[Depends(require("read"))])
    async def list_environments(request: Request) -> dict[str, Any]:
        return {"environments": request.app.state.environments.list()}

    @app.post("/v1/environments", dependencies=[Depends(require("admin"))])
    async def create_environment(body: dict[str, Any], request: Request) -> dict[str, Any]:
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="missing 'name'")
        profile_id = body.get("egress_profile")
        if profile_id and request.app.state.profiles.get(profile_id) is None:
            raise HTTPException(status_code=422, detail=f"unknown egress profile: {profile_id}")
        return request.app.state.environments.create(
            name=name, description=body.get("description") or "",
            secrets=body.get("secrets"), egress_profile=body.get("egress_profile"))

    @app.patch("/v1/environments/{eid}", dependencies=[Depends(require("admin"))])
    async def update_environment(eid: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
        from .environments import _UNSET
        profile_id = body.get("egress_profile")
        if profile_id and request.app.state.profiles.get(profile_id) is None:
            raise HTTPException(status_code=422, detail=f"unknown egress profile: {profile_id}")
        # egress_profile uses the sentinel so an explicit null (detach) differs from "omitted".
        env = request.app.state.environments.update(
            eid, name=body.get("name"), description=body.get("description"),
            secrets=body.get("secrets"),
            egress_profile=(body["egress_profile"] if "egress_profile" in body else _UNSET))
        if not env:
            raise HTTPException(status_code=404, detail="environment not found")
        # re-resolve + push secrets/policy to RUNNING sessions (an attached agent's map changed)
        await manager(request).propagate_environments()
        return env

    @app.delete("/v1/environments/{eid}", dependencies=[Depends(require("admin"))])
    async def delete_environment(eid: str, request: Request) -> dict[str, Any]:
        refs = [a.id for a in agents(request).list()
                if eid in (a.harness.environments or [])]
        if refs:
            raise HTTPException(
                status_code=409,
                detail=f"environment is attached to agent(s): {refs}; detach it first",
            )
        if not request.app.state.environments.delete(eid):
            raise HTTPException(status_code=404, detail="environment not found")
        return {"ok": True}

    @app.get("/v1/egress/audit", dependencies=[Depends(require("read"))])
    async def get_egress_audit(request: Request, limit: int = 200) -> dict[str, Any]:
        """Fleet-wide recent egress decisions, aggregated from the per-session audit files
        on our own volume (a single shared file would interleave sessions' HMAC chains).

        Runner-independent: the k8s Warden's in-Pod audit is mirrored here by the
        manager's drain sweep, so this is a local file read on every runner — no pod-exec
        fanout on a 6s console poll, and terminated sessions still show up."""
        n = min(max(limit, 1), 1000)
        rows = await asyncio.to_thread(_read_all_audits, n)
        agent_by_session = {
            row["id"]: row.get("agent_id") for row in manager(request).list_metadata()
        }
        for row in rows:
            row["agent_id"] = agent_by_session.get(str(row.get("session_id")))
        rows.sort(key=lambda r: str(r.get("ts") or ""))
        return {"decisions": rows[-n:]}

    @app.get("/v1/sessions/{sid}/egress/verify", dependencies=[Depends(require("read"))])
    async def verify_egress(sid: str, request: Request) -> dict[str, Any]:
        """Recompute Warden's tamper-evident audit hash-chain for a session and
        report the first break or seq gap. Turns the receipt machinery into a
        control the operator can actually run (the key is persisted per session).

        Works for every runner, live or long-finished: the audit is persisted to our own
        volume (the k8s Warden's in-Pod audit is drained there), so the chain stays
        verifiable after the Pod is reaped — which is exactly when a session is worth
        reviewing."""
        from .receipts import load_receipt_key, verify_file

        key = load_receipt_key(config, sid)
        if not key:
            raise HTTPException(status_code=404,
                                detail="no receipt key for this session (receipts disabled, or pre-receipt session)")
        # Flush the live tail first so verifying a RUNNING session covers everything up to now
        # (the periodic sweep is up to one interval behind).
        live = manager(request).get(sid)
        if live is not None:
            await live.runner.drain_audit()
        path = session_audit_path(config, sid)
        if not path.exists():
            raise HTTPException(status_code=404, detail="no audit found for this session")
        result = await asyncio.to_thread(verify_file, path, key)
        return {"session_id": sid, **result}

    # ---- unified logs (session events + egress decisions) with filtering ----
    @app.get("/v1/logs", dependencies=[Depends(require("read"))])
    async def get_logs(
        request: Request, agent_id: str | None = None, session_id: str | None = None,
        source: str | None = None, type: str | None = None, q: str | None = None,
        since: str | None = None, limit: int = 500,
    ) -> dict[str, Any]:
        mgr = manager(request)
        # Selecting/faceting logs needs only registry metadata. Folding every
        # session's complete event history here defeated the scan cap below.
        all_sessions = mgr.list_metadata()
        matched = [s for s in all_sessions
                   if (not session_id or s["id"] == session_id)
                   and (not agent_id or s.get("agent_id") == agent_id)]
        scan = matched[:LOG_SCAN_SESSIONS]  # newest-first, so this keeps the most recent
        ql = (q or "").lower()
        n = min(max(int(limit), 1), 2000)
        keep_ts = lambda ts: (not since) or (ts >= since)  # noqa: E731
        out: list[dict[str, Any]] = []
        # Every place this view silently stops looking. It reported a partial answer that
        # looked complete, so an operator searching the fleet could reasonably conclude
        # something never happened when it simply wasn't scanned.
        capped_sessions = len(matched) - len(scan)
        capped_events = False

        for s in scan:
            sid, aid = s["id"], s.get("agent_id")
            if source in (None, "", "event"):
                try:
                    evs, was_capped = await asyncio.to_thread(
                        tail_events, config.logs_dir / f"{sid}.jsonl", LOG_EVENTS_PER_SESSION,
                    )
                except Exception:  # noqa: BLE001
                    evs, was_capped = [], False
                capped_events = capped_events or was_capped
                for ev in evs:
                    t = ev.get("type")
                    if t in ("rewind_point", "status"):
                        continue
                    if (type and t != type) or not keep_ts(str(ev.get("ts", ""))):
                        continue
                    detail = _log_detail(ev)
                    if ql and ql not in detail.lower() and ql not in str(t).lower():
                        continue
                    out.append({"ts": str(ev.get("ts", "")), "source": "event", "session_id": sid,
                                "agent_id": aid, "type": t, "detail": detail})
            if source in (None, "", "egress"):
                # This session's own persisted audit file — attributed, works after stop, and
                # identical on every runner (the k8s in-Pod audit is drained here). Previously
                # this exec'd into each live Pod, so one /v1/logs request cost up to 40
                # serialized pod-execs — on a 6s poll, per open tab.
                rows = await asyncio.to_thread(
                    read_audit, session_audit_path(config, sid), LOG_AUDIT_PER_SESSION)
                capped_events = capped_events or len(rows) == LOG_AUDIT_PER_SESSION
                for d in rows:
                    if d.get("kind") != "egress":
                        continue
                    dec, host = d.get("decision"), str(d.get("host", ""))
                    if (type and dec != type) or not keep_ts(str(d.get("ts", ""))):
                        continue
                    if ql and ql not in host.lower() and ql not in str(dec).lower():
                        continue
                    out.append({"ts": str(d.get("ts", "")), "source": "egress", "session_id": sid,
                                "agent_id": aid, "type": dec, "host": host, "port": d.get("port"),
                                "reason": d.get("reason"), "detail": f"{dec} {host}".strip()})

        out.sort(key=lambda e: e.get("ts", ""), reverse=True)
        return {
            "logs": out[:n],
            # What was left out, so the UI can say so. `sessions` is how many matching
            # sessions were never opened; `rows` means at least one session had more
            # history than the per-session window; `limit` means the merged result was
            # longer than the requested page.
            "truncated": {
                "sessions": capped_sessions,
                "rows": capped_events,
                "limit": len(out) > n,
                "scan_limit": LOG_SCAN_SESSIONS,
            },
            "facets": {
                "agents": sorted({s.get("agent_id") for s in all_sessions if s.get("agent_id")}),
                "sessions": [{"id": s["id"], "title": s.get("title"), "agent_id": s.get("agent_id"),
                              "status": s.get("status")} for s in all_sessions],
                "types": sorted({str(e["type"]) for e in out if e.get("type")}),
            },
        }

    # ---- agent registry ----
    @app.post("/v1/agents", dependencies=[Depends(require("run"))])
    async def create_agent(body: CreateAgentRequest, request: Request) -> dict[str, Any]:
        if body.template:
            t = templates.get(body.template)
            if not t:
                raise HTTPException(status_code=400, detail=f"unknown template: {body.template}")
            # template defaults; explicit request fields override
            base = json.loads(t.harness.to_json())
            overlay = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in HARNESS_FIELDS}
            harness = Harness.from_dict({**base, **overlay})
        else:
            harness = body.to_harness(config.default_model)
        _validate_environment_refs(request, harness.environments)
        spec = agents(request).create(name=body.name, harness=harness, memory_scope=body.memory_scope or "")
        return spec.to_dict()

    @app.get("/v1/agents", dependencies=[Depends(require("read"))])
    async def list_agents(request: Request) -> dict[str, Any]:
        return {"agents": [a.to_dict() for a in agents(request).list()]}

    @app.get("/v1/agents/{aid}", dependencies=[Depends(require("read"))])
    async def get_agent(aid: str, request: Request) -> dict[str, Any]:
        spec = agents(request).get(aid)
        if not spec:
            raise HTTPException(status_code=404, detail="agent not found")
        return spec.to_dict()

    @app.get("/v1/agents/{aid}/spend", dependencies=[Depends(require("read"))])
    async def agent_spend(aid: str, request: Request) -> dict[str, Any]:
        """Cumulative budget ledger for an agent — total spend across ALL its sessions (durable,
        survives restarts via the registry), all-time plus 24h / 30d windows. A supervisor polls
        this to enforce a cumulative cap, not just a per-session one."""
        if not agents(request).get(aid):
            raise HTTPException(status_code=404, detail="agent not found")
        reg = manager(request).registry
        now = datetime.now(timezone.utc)
        iso = lambda dt: dt.isoformat().replace("+00:00", "Z")  # noqa: E731
        return {
            "agent_id": aid,
            "all_time": reg.spend(aid),
            "last_24h": reg.spend(aid, iso(now - timedelta(days=1))),
            "last_30d": reg.spend(aid, iso(now - timedelta(days=30))),
        }

    @app.get("/v1/usage", dependencies=[Depends(require("read"))])
    async def usage(request: Request, days: int = 30) -> dict[str, Any]:
        """Fleet spend over a time window: a daily series plus a per-agent breakdown.

        Cost comes from the durable spend ledger, NOT the session list, and the distinction
        is the point: the ledger outlives session deletion, so this reports what was actually
        spent rather than what is still lying around. Tokens and tool calls have no ledger —
        they are folded from the logs, so those DO vanish with a deleted session."""
        n = min(max(int(days), 1), 365)
        reg = manager(request).registry
        since = (datetime.now(timezone.utc) - timedelta(days=n)).isoformat().replace("+00:00", "Z")
        daily = reg.spend_series(since)
        by_agent = reg.spend_by_agent(since)
        # Tokens/tool-calls/cost-by-model fold the session logs; cost folds the ledger. Both
        # belong here rather than in the console, which now pages the session list and would
        # otherwise report whatever happened to be on page one.
        folded = manager(request).usage_totals(since)
        return {
            "window_days": n,
            "since": since,
            "daily": daily,
            "by_agent": by_agent,
            **folded,
            "totals": {
                "sessions": sum(d["sessions"] for d in daily),
                "total_cost_usd": round(sum(d["total_cost_usd"] for d in daily), 6),
            },
            # All-time is a separate roll-up so the UI can show "of $X ever" next to the window.
            "all_time": {
                "sessions": sum(a["sessions"] for a in reg.spend_by_agent()),
                "total_cost_usd": round(sum(a["total_cost_usd"] for a in reg.spend_by_agent()), 6),
            },
        }

    @app.patch("/v1/agents/{aid}", dependencies=[Depends(require("run"))])
    async def update_agent(aid: str, body: UpdateAgentRequest, request: Request) -> dict[str, Any]:
        data = body.model_dump(exclude_unset=True)
        if "environments" in data:
            _validate_environment_refs(request, data["environments"])
        # Pass name/memory_scope only when the client actually sent them, so an explicit
        # null (clear) is distinguished from "not in this PATCH" by the store's sentinel.
        identity = {k: data.pop(k) for k in ("name", "memory_scope") if k in data}
        spec = agents(request).update(aid, harness_updates=data, **identity)
        if not spec:
            raise HTTPException(status_code=404, detail="agent not found")
        # Most fields apply only to NEW sessions (baked into the sandbox at launch); the
        # budget cap + egress profile can be applied to this agent's RUNNING sessions live.
        applied = await manager(request).propagate_agent_harness(aid, data)
        return {**spec.to_dict(), "applied_to_running": applied}

    @app.delete("/v1/agents/{aid}", dependencies=[Depends(require("run"))])
    async def delete_agent(aid: str, request: Request, purge_memory: bool = False) -> dict[str, Any]:
        spec = agents(request).get(aid)
        if not agents(request).delete(aid):
            raise HTTPException(status_code=404, detail="agent not found")
        purged = False
        if purge_memory and spec:
            if config.runner == "k8s":
                from .k8s_runner import delete_memory_pvc

                await asyncio.to_thread(delete_memory_pvc, config, spec.memory_volume())
            else:
                from .runners import _run

                await _run(["docker", "volume", "rm", "-f", spec.memory_volume()])
            # memory_mode="synced" keeps memory as a tarball on the ORCHESTRATOR's volume,
            # not in the PVC/volume purged above — so without this the agent's real memory
            # survived its own deletion. Runner-independent on purpose: an agent that ran
            # under k8s and is deleted while the config says docker would otherwise strand
            # its snapshot forever.
            from .k8s_runner import delete_memory_snapshots

            await asyncio.to_thread(delete_memory_snapshots, config, spec.memory_volume())
            purged = True
        return {"ok": True, "memory_purged": purged}

    @app.post("/v1/sessions", dependencies=[Depends(require("run"))])
    async def create_session(body: CreateSessionRequest, request: Request) -> dict[str, Any]:
        if body.agent_id:
            # An agent owns its durable harness; only session-scoped fields (client_tools) may
            # be layered on. A caller who ALSO sets, say, model= alongside agent_id would have
            # it silently dropped — 422 instead so the mismatch is loud, not surprising.
            stray = set(body.model_fields_set) - {"agent_id", "title", *SESSION_SCOPED_HARNESS_FIELDS}
            if stray:
                raise HTTPException(status_code=422, detail=(
                    f"fields owned by the agent can't be overridden per-session: {sorted(stray)} — "
                    "edit the agent, or omit agent_id to launch an inline harness"))
            spec = agents(request).get(body.agent_id)
            if not spec:
                raise HTTPException(status_code=404, detail="agent not found")
            sess = apply_session_overlay(SessionConfig.from_agent(spec, title=body.title), body)
        else:
            sess = SessionConfig(harness=body.to_harness(config.default_model), title=body.title)
        try:
            session = await manager(request).create(sess)
        except CapacityError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except RuntimeError as exc:
            # The manager has already rolled back runner resources and its
            # in-memory reservation. Return a typed operational failure instead
            # of an opaque 500/ghost session.
            raise HTTPException(status_code=503, detail=f"session start failed: {exc}") from exc
        return {"id": session.id, "status": session.status, "agent_id": sess.agent_id}

    @app.get("/v1/sessions", dependencies=[Depends(require("read"))])
    async def list_sessions(request: Request, limit: int = 100, before: str | None = None) -> dict[str, Any]:
        """Sessions newest-first, one page at a time.

        Sessions are durable and accumulate for the life of the deployment, so an unbounded
        listing grows without limit — every row summarized, serialized and rendered on a 5s
        poll. Pass ``next_cursor`` back as ``before`` for the next page; ``total`` and
        ``running`` are fleet-wide, not page-wide, so a live badge doesn't shrink as you page.
        """
        return manager(request).list_page(limit=min(max(int(limit), 1), 500), before=before)

    @app.get("/v1/sessions/{sid}", dependencies=[Depends(require("read"))])
    async def get_session(sid: str, request: Request) -> dict[str, Any]:
        summary = manager(request).summary_of(sid)  # live OR read-only from the log
        if summary is None:
            raise HTTPException(status_code=404, detail="session not found")
        return summary

    @app.delete("/v1/sessions/{sid}", dependencies=[Depends(require("run"))])
    async def delete_session(sid: str, request: Request) -> dict[str, Any]:
        ok = await manager(request).delete(sid)
        if not ok:
            raise HTTPException(status_code=404, detail="session not found")
        return {"ok": True}

    @app.post("/v1/sessions/{sid}/recover", dependencies=[Depends(require("run"))])
    async def recover_session(sid: str, request: Request) -> dict[str, Any]:
        """Reattach to a session marked terminated whose sandbox is still running.

        The orchestrator can lose its event stream while the sandbox is perfectly alive — the
        stream is a client of the sandbox, not the sandbox itself, and a Docker daemon restart
        or a host suspend drops it. The pump now reattaches on its own, so this is the manual
        counterpart for sessions stranded before that fix, or after the reattach budget ran out.

        Refuses unless the sandbox is genuinely running: recovery must be a fact about the
        container, never an assumption. 409 tells you the sandbox is gone and the transcript
        is all that is left."""
        mgr = manager(request)
        # Presence in the manager's dict does NOT mean live: a terminated session stays there
        # so its summary can still be served from memory. Ask for the status.
        live = mgr.get(sid)
        if live is not None and live.status in LIVE_STATUSES:
            return {"ok": True, "already_live": True, "session_id": sid, "status": live.status}
        try:
            recovered = await mgr.recover(sid)
        except LookupError:
            raise HTTPException(status_code=404, detail="session not found")
        if not recovered:
            raise HTTPException(
                status_code=409,
                detail="sandbox is not running — this session cannot be resumed; its transcript "
                       "is still readable and downloadable")
        return {"ok": True, "session_id": sid, "status": recovered}

    @app.post("/v1/sessions/{sid}/messages", dependencies=[Depends(require("run"))])
    async def send_message(sid: str, body: MessageRequest, request: Request) -> dict[str, Any]:
        await _get(request, sid).send_message(body.content if body.content is not None else body.text)
        return {"ok": True}

    @app.post("/v1/sessions/{sid}/interrupt", dependencies=[Depends(require("run"))])
    async def interrupt(sid: str, request: Request) -> dict[str, Any]:
        await _get(request, sid).interrupt()
        return {"ok": True}

    @app.post("/v1/sessions/{sid}/answer", dependencies=[Depends(require("run"))])
    async def answer(sid: str, body: AnswerRequest, request: Request) -> dict[str, Any]:
        await _get(request, sid).answer(body.question_id, body.answers)
        return {"ok": True, "question_id": body.question_id}

    @app.post("/v1/sessions/{sid}/permission", dependencies=[Depends(require("run"))])
    async def permission(sid: str, body: DecisionRequest, request: Request) -> dict[str, Any]:
        await _get(request, sid).decide(body.request_id, body.decision)
        return {"ok": True, "request_id": body.request_id}

    @app.post("/v1/sessions/{sid}/tool_result", dependencies=[Depends(require("run"))])
    async def client_tool_result(sid: str, body: ClientToolResultRequest, request: Request) -> dict[str, Any]:
        await _get(request, sid).client_tool_result(body.call_id, body.content, body.is_error)
        return {"ok": True, "call_id": body.call_id}

    @app.post("/v1/sessions/{sid}/config", dependencies=[Depends(require("run"))])
    async def reconfigure(sid: str, body: ReconfigRequest, request: Request) -> dict[str, Any]:
        await _get(request, sid).reconfigure(model=body.model, permission_mode=body.permission_mode)
        return {"ok": True, "model": body.model, "permission_mode": body.permission_mode}

    @app.post("/v1/sessions/{sid}/rewind", dependencies=[Depends(require("run"))])
    async def rewind(sid: str, body: RewindRequest, request: Request) -> dict[str, Any]:
        await _get(request, sid).rewind(body.message_id, body.mode)
        return {"ok": True, "message_id": body.message_id, "mode": body.mode}

    @app.get("/v1/sessions/{sid}/events", dependencies=[Depends(require("read"))])
    async def events(
        sid: str, request: Request, after: int = -1, tail: int | None = None,
    ) -> StreamingResponse:
        mgr = manager(request)
        live = mgr.get(sid)
        if live is None and mgr.summary_of(sid) is None:
            raise HTTPException(status_code=404, detail="session not found")
        if tail is not None and not 1 <= tail <= 5000:
            raise HTTPException(status_code=422, detail="tail must be between 1 and 5000")

        async def gen():
            source = (
                live.stream(after, replay_limit=tail)
                if live is not None
                else mgr.read_only_stream(sid, after, replay_limit=tail)
            )
            async for ev in source:
                # idle keepalive → an SSE COMMENT (clients ignore `:`-prefixed lines),
                # not a data event, so the SDK/console don't see a phantom event.
                if ev.get("type") == "_heartbeat":
                    yield ": keepalive\n\n"
                else:
                    yield f"data: {json.dumps(ev, default=str)}\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/sessions/{sid}/events/export", dependencies=[Depends(require("read"))])
    async def export_events(sid: str, request: Request) -> FileResponse:
        mgr = manager(request)
        if mgr.summary_of(sid) is None:
            raise HTTPException(status_code=404, detail="session not found")
        path = config.logs_dir / f"{sid}.jsonl"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="session event log not found")
        return FileResponse(
            path,
            media_type="application/x-ndjson",
            filename=f"terrarium-{sid}.jsonl",
        )

    @app.post("/v1/sessions/{sid}/files/upload", dependencies=[Depends(require("run"))])
    async def upload_file(sid: str, request: Request,
                          file: UploadFile = File(...), name: str | None = Form(None)) -> dict[str, Any]:
        """Upload file content straight into the session's /workspace (no host path).
        Works on every runner (docker cp / k8s tar-exec / local write)."""
        data = await file.read()
        if len(data) > UPLOAD_MAX_BYTES:  # exact cap on decoded bytes (middleware pre-rejects on Content-Length)
            raise HTTPException(status_code=413, detail="file too large (max 25 MiB)")
        runner = _get(request, sid).runner
        if not hasattr(runner, "copy_in_bytes"):
            raise HTTPException(status_code=400, detail="this runner does not support upload")
        try:
            written = await runner.copy_in_bytes(name or file.filename or "upload.bin", data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "name": written, "size": len(data)}

    @app.get("/v1/sessions/{sid}/files/{name}", dependencies=[Depends(require("read"))])
    async def download_file(sid: str, name: str, request: Request) -> Response:
        """Download one file from the session's /workspace.

        Runner-independent (docker cp / k8s exec / local read) — an agent's output artifacts
        are the direction of the file bridge that matters once a run has finished, so this
        must not be conditional on which runner the deployment uses."""
        # Resolve the session OUTSIDE the try: _get raises HTTPException(404), and the broad
        # except below would otherwise re-wrap "session not found" as a 400.
        runner = _get(request, sid).runner
        try:
            data = await runner.copy_out_bytes(name)
        except NotImplementedError:
            raise HTTPException(status_code=400, detail="this runner does not support download")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        # attachment + a quoted filename so a browser saves it under the agent's name
        # instead of rendering it (an agent-authored .html must never run same-origin
        # against the console's session).
        return Response(
            content=data, media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filebridge.sanitize_name(name)}"'},
        )

    # ---- templates (one-click agent presets) ----
    @app.get("/v1/templates", dependencies=[Depends(require("read"))])
    async def list_templates() -> dict[str, Any]:
        return {"templates": templates.list_templates()}

    # ---- tool + skill catalog ----
    @app.get("/v1/tools", dependencies=[Depends(require("read"))])
    async def list_tools() -> dict[str, Any]:
        """Built-in tools (grouped), the availability presets, the default auto-approve set,
        and the known skills. The console renders THIS rather than keeping its own copy, for
        the same reason as the model catalog: the CLI's tool set changes underneath us, and a
        second list in another language has nothing to keep it honest."""
        return toolset.catalog()

    # ---- model catalog ----
    @app.get("/v1/models", dependencies=[Depends(require("read"))])
    async def list_models() -> dict[str, Any]:
        """The models this orchestrator offers, plus the default. The console renders this
        rather than keeping a list of its own — every picker (agent form, new session, live
        switcher) reads the same catalog, so none of them can offer a different set."""
        return {"models": models.catalog(), "default": config.default_model}

    # ---- schedules (recurring agents) ----
    def schedules(request: Request) -> ScheduleStore:
        return request.app.state.schedules

    @app.post("/v1/schedules", dependencies=[Depends(require("run"))])
    async def create_schedule(body: ScheduleRequest, request: Request) -> dict[str, Any]:
        if not agents(request).get(body.agent_id):
            raise HTTPException(status_code=404, detail="agent not found")
        try:
            s = schedules(request).create(
                name=body.name, agent_id=body.agent_id, prompt=body.prompt,
                cron=body.cron, enabled=body.enabled, max_budget_usd=body.max_budget_usd,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return s.to_dict()

    @app.get("/v1/schedules", dependencies=[Depends(require("read"))])
    async def list_schedules(request: Request) -> dict[str, Any]:
        return {"schedules": [s.to_dict() for s in schedules(request).list()]}

    @app.patch("/v1/schedules/{sid}", dependencies=[Depends(require("run"))])
    async def update_schedule(sid: str, body: ScheduleUpdate, request: Request) -> dict[str, Any]:
        if body.agent_id is not None and not agents(request).get(body.agent_id):
            raise HTTPException(status_code=404, detail="agent not found")
        try:
            s = schedules(request).update(sid, **body.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not s:
            raise HTTPException(status_code=404, detail="schedule not found")
        return s.to_dict()

    @app.delete("/v1/schedules/{sid}", dependencies=[Depends(require("run"))])
    async def delete_schedule(sid: str, request: Request) -> dict[str, Any]:
        if not schedules(request).delete(sid):
            raise HTTPException(status_code=404, detail="schedule not found")
        return {"ok": True}

    @app.post("/v1/schedules/{sid}/run", dependencies=[Depends(require("run"))])
    async def run_schedule(sid: str, request: Request) -> dict[str, Any]:
        session_id = await request.app.state.scheduler.fire(sid)
        if not session_id:
            raise HTTPException(status_code=400, detail="schedule could not run (missing agent?)")
        return {"ok": True, "session_id": session_id}

    return app
