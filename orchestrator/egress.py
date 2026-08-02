"""Egress policy: the destination-rule model, its store, and the audit reader.

Warden is the policy brain; this is the orchestrator's view of the state it reads
and writes. The orchestrator owns ``policy.json`` (which Warden hot-reloads) and
reads back ``audit/<session>.jsonl`` (which Warden appends, HMAC-chained).

A policy is a flat list of rules; each rule is::

    {"action": "allow"|"deny"|"inspect", "dest": "<domain|ip|cidr>",
     "ports": [443, 5432] | None, "enabled": True, "note": ""}

``dest`` is an exact domain, an IP literal, or a CIDR — never a wildcard. Warden
matches IP/CIDR against the *resolved* address, so a private CIDR here is what
lifts Warden's private-range floor for that destination. ``ports`` (allow/inspect
only) lifts the default 80/443 port wall; ``deny`` is dest-only.

Rules are canonical. A pre-rules record's ``allow``/``deny``/``inspect`` lists are
migrated forward once on read so nothing stored under the old shape is lost.
"""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from typing import Any

from .store import JsonStore

# Always allowed — the credential mediator must reach Anthropic regardless of policy.
ANTHROPIC_HOSTS = ("api.anthropic.com", "platform.claude.com")

VALID_MODES = ("enforce", "monitor")
ACTIONS = ("allow", "deny", "inspect")


# ── destination hygiene ──────────────────────────────────────────────────────

def _clean_ip_or_cidr(s: str) -> str | None:
    """The canonical form if ``s`` is an IP literal or CIDR, else None (→ treat as a domain)."""
    try:
        return str(ipaddress.ip_network(s, strict=False)) if "/" in s else str(ipaddress.ip_address(s))
    except ValueError:
        return None


def clean_hosts(items: Any) -> list[str]:
    """Normalize a destination list — each entry a domain, an IP literal, or a CIDR.
    Domains: drop port, lowercase, strip trailing dot, clamp to DNS length, drop wildcards.
    IP/CIDR: canonicalized (so 10.20.0.5/16 → 10.20.0.0/16). De-duplicated, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in items or []:
        s = str(raw).strip()
        if not s:
            continue
        canon = _clean_ip_or_cidr(s)
        if canon is None:  # a domain (a "/" here means a malformed CIDR, not a host → drop)
            if "/" in s:
                continue
            canon = s.lower().split(":")[0].rstrip(".")[:253]
            if not canon or "*" in canon:
                continue
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def clean_host_overrides(raw: Any) -> list[dict[str, str]]:
    """Static host→ip resolve overrides ``[{host, ip}]``: Warden resolves these names to the
    given address instead of asking DNS — how an internal name reaches its private IP when the
    sandbox's resolver can't see internal DNS. host canonicalized; ip must parse."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for h in raw or []:
        if not isinstance(h, dict):
            continue
        hosts = clean_hosts([h.get("host", "")])
        if not hosts:
            continue
        host = hosts[0]
        try:
            ip = str(ipaddress.ip_address(str(h.get("ip", "")).strip()))
        except ValueError:
            continue
        if host in seen:
            continue
        seen.add(host)
        out.append({"host": host, "ip": ip})
    return out


# ── rules ────────────────────────────────────────────────────────────────────

def _clean_ports(raw: Any) -> list[int] | None:
    """Sorted, de-duped, in-range ports; None (or empty) → the Warden default (80/443)."""
    if not raw:
        return None
    out: list[int] = []
    for p in raw:
        try:
            n = int(p)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 65535 and n not in out:
            out.append(n)
    return sorted(out) or None


def clean_rule(raw: Any) -> dict[str, Any] | None:
    """Validate + canonicalize one rule, or None if the destination is unusable."""
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action", "allow")).strip().lower()
    if action not in ACTIONS:
        action = "allow"
    dests = clean_hosts([raw.get("dest", "")])
    if not dests:
        return None
    return {
        "action": action,
        "dest": dests[0],
        "ports": None if action == "deny" else _clean_ports(raw.get("ports")),
        "enabled": bool(raw.get("enabled", True)),
        "note": str(raw.get("note", "") or "").strip()[:200],
    }


def clean_rules(raw: Any) -> list[dict[str, Any]]:
    """Clean a rule list, dropping invalid rows and de-duping by (action, dest, ports)."""
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for r in raw or []:
        c = clean_rule(r)
        if not c:
            continue
        key = (c["action"], c["dest"], tuple(c["ports"] or ()))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _legacy_to_rules(allow: Any = None, deny: Any = None, inspect: Any = None) -> list[dict[str, Any]]:
    """One-time migration of a pre-rules record's three lists into rules (default ports, enabled)."""
    raw = ([{"action": "allow", "dest": d} for d in (allow or [])]
           + [{"action": "deny", "dest": d} for d in (deny or [])]
           + [{"action": "inspect", "dest": d} for d in (inspect or [])])
    return clean_rules(raw)


def record_rules(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The rules of a stored policy/profile record: its ``rules`` if present, else a one-time
    migration from a pre-rules record's allow/deny/inspect fields (so nothing is lost on upgrade)."""
    if isinstance(record.get("rules"), list):
        return clean_rules(record["rules"])
    return _legacy_to_rules(record.get("allow"), record.get("deny"), record.get("inspect"))


# ── the global policy store ──────────────────────────────────────────────────

class EgressPolicyStore(JsonStore):
    """The global default policy — what a session gets when its agent's environments
    carry no egress profile. Warden reads the rendered form; the console edits this."""

    def __init__(self, path: Path, seed_allow: tuple[str, ...] = ()) -> None:
        super().__init__(path)
        if not self.path.exists():
            seed = clean_rules([{"action": "allow", "dest": h} for h in seed_allow])
            self._write({"mode": "enforce", "rules": seed, "kill": False, "allow_metadata": False})

    def get(self) -> dict[str, Any]:
        data = self._read({}) or {}
        mode = data.get("mode")
        return {
            "mode": mode if mode in VALID_MODES else "enforce",
            "rules": record_rules(data),                       # migrates a pre-rules file
            "hosts": clean_host_overrides(data.get("hosts")),  # static host→ip resolve overrides
            "kill": bool(data.get("kill", False)),             # kill switch — Warden denies ALL egress
            # Explicit, dangerous opt-in to reach the cloud-metadata IP (169.254.169.254). Off by
            # default; a broad allow CIDR does NOT reopen it — only this flag (Warden enforces).
            "allow_metadata": bool(data.get("allow_metadata", False)),
            "always_allow": list(ANTHROPIC_HOSTS),             # Claude hosts, never removable
        }

    def set(self, *, mode: str | None = None, rules: Any = None, hosts: Any = None,
            kill: bool | None = None, allow_metadata: bool | None = None) -> dict[str, Any]:
        with self.lock:
            cur = self.get()
            if mode is not None:
                if mode not in VALID_MODES:
                    raise ValueError(f"mode must be one of {VALID_MODES}")
                cur["mode"] = mode
            if rules is not None:
                cur["rules"] = clean_rules(rules)
            if hosts is not None:
                cur["hosts"] = clean_host_overrides(hosts)
            if kill is not None:
                cur["kill"] = bool(kill)
            if allow_metadata is not None:
                cur["allow_metadata"] = bool(allow_metadata)
            self._write({"mode": cur["mode"], "rules": cur["rules"], "hosts": cur["hosts"],
                         "kill": cur["kill"], "allow_metadata": cur["allow_metadata"]})
            return self.get()


# ── audit ────────────────────────────────────────────────────────────────────

def session_audit_path(config: Any, session_id: str) -> Path:
    """Where THIS session's egress audit lives on the orchestrator's runtime volume.

    Runner-independent on purpose. Docker's Warden appends here directly (shared mount);
    the k8s Warden writes to an emptyDir inside its own container and ``K8sRunner`` drains
    it here. Either way the chain outlives the sandbox, so ``verify-egress`` and the Logs
    view are one code path that still works after the Pod is reaped."""
    return config.egress_dir / "audit" / f"{session_id}.jsonl"


def read_audit(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Tail Warden's append-only audit JSONL (newest last).

    Reads only the tail (seek from the end), never the whole file — a hostile agent can
    flood denials without bound, so the console's few-second poll must stay O(limit)."""
    if limit <= 0 or not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            want = min(size, limit * 512 + 1024)  # ~512 bytes/line budget
            f.seek(size - want)
            chunk = f.read(want)
    except Exception:
        return []
    lines = chunk.decode("utf-8", "replace").splitlines()
    if want < size and lines:
        lines = lines[1:]  # drop the partial first line we sliced into
    out: list[dict[str, Any]] = []
    for ln in lines[-limit:]:
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out
