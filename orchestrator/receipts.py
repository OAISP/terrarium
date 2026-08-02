"""Egress-audit receipt verification — the orchestrator side of Warden's
tamper-evident audit.

Warden signs each audit line with an HMAC-SHA256 *hash chain* (see
``warden/src/audit.rs``):

    receipt_n = HMAC(key, receipt_{n-1} || "\\n" || canonical(record_n))

where ``canonical`` is the compact, key-sorted JSON of the record MINUS its own
``receipt`` field, and ``key`` is the raw bytes of the ``WARDEN_RECEIPT_KEY`` hex
string (NOT the decoded bytes — Warden uses ``s.into_bytes()``). Genesis prev = "".

For the chain to be checkable later, the orchestrator must KEEP the per-session
key: a receipt nobody retained the key for is not evidence. We persist it to a
0600 file on the runtime PVC and recompute the chain here on demand.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from .config import Config


# ---- per-session key custody -------------------------------------------------
def _key_path(config: Config, sid: str) -> Path:
    # Receipt keys are evidence-retention data, not live session scratch. Docker
    # reaps runtime_dir/sessions/<sid> on stop, while the corresponding audit is
    # intentionally retained for post-mortem verification.
    return config.runtime_dir / "receipt-keys" / f"{sid}.key"


def _legacy_key_path(config: Config, sid: str) -> Path:
    return config.runtime_dir / "sessions" / sid / "receipt.key"


def persist_receipt_key(config: Config, sid: str) -> str:
    """Generate (or reuse) this session's receipt key and persist it 0600 so the
    chain can be verified after the session ends. Returns the hex key."""
    p = _key_path(config, sid)
    if p.exists():
        existing = p.read_text().strip()
        if existing:
            return existing
    legacy = _legacy_key_path(config, sid)
    if legacy.exists():
        existing = legacy.read_text().strip()
        if existing:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(existing)
            os.chmod(p, 0o600)
            return existing
    key = secrets.token_hex(16)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key)
    os.chmod(p, 0o600)
    return key


def load_receipt_key(config: Config, sid: str) -> str | None:
    p = _key_path(config, sid)
    if p.exists():
        return p.read_text().strip() or None
    # Read old sessions created before receipt keys moved out of session scratch.
    legacy = _legacy_key_path(config, sid)
    return legacy.read_text().strip() or None if legacy.exists() else None


# ---- chain math (must match warden/src/audit.rs byte-for-byte) ---------------
def canonical(record: dict[str, Any]) -> str:
    """Compact, key-sorted JSON of the record minus ``receipt`` — mirrors serde_json
    serializing a BTreeMap (sorted keys, no spaces, UTF-8 / no ASCII-escaping)."""
    obj = {k: v for k, v in record.items() if k != "receipt"}
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sign(key: str, prev: str, canon: str) -> str:
    mac = hmac.new(key.encode(), prev.encode() + b"\n" + canon.encode(), hashlib.sha256)
    return mac.hexdigest()


def verify_chain(lines: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Recompute the hash chain over ``lines`` in its physical record order.

    Returns ``{ok, checked, first_break_seq, gap_before_seq, reason}``. A broken
    receipt localizes an edited line; a seq gap reveals a deleted/dropped line
    (e.g. a silently-dropped ``deny``). Verification is intentionally anchored at
    genesis: a suffix whose first line is not seq 0 is incomplete and cannot be
    called tamper-evident because its first retained receipt is uncheckable."""
    signed = [ln for ln in lines if "receipt" in ln and "seq" in ln]
    if len(signed) != len(lines):
        return {"ok": False, "checked": 0, "first_break_seq": None,
                "gap_before_seq": None, "reason": "audit contains an unsigned record"}
    if not signed:
        return {"ok": False, "checked": 0, "first_break_seq": None,
                "gap_before_seq": None, "reason": "no signed audit lines (receipts disabled?)"}
    first_seq = int(signed[0]["seq"])
    if first_seq != 0:
        return {"ok": False, "checked": 0, "first_break_seq": None,
                "gap_before_seq": first_seq,
                "reason": f"audit prefix missing (first available seq is {first_seq}, expected 0)"}

    prev = ""  # genesis
    last_seq: int | None = None
    for ln in signed:
        seq = int(ln["seq"])
        if last_seq is not None and seq != last_seq + 1:
            return {"ok": False, "checked": seq - int(signed[0]["seq"]),
                    "first_break_seq": None, "gap_before_seq": seq,
                    "reason": f"seq gap before {seq} (a line was deleted/dropped)"}
        expected = sign(key, prev, canonical(ln))
        if not hmac.compare_digest(expected, str(ln.get("receipt"))):
            return {"ok": False, "checked": seq,
                    "first_break_seq": seq, "gap_before_seq": None,
                    "reason": f"receipt mismatch at seq {seq} (line tampered)"}
        prev = str(ln["receipt"])
        last_seq = seq
    return {"ok": True, "checked": len(signed), "first_break_seq": None,
            "gap_before_seq": None, "reason": f"chain intact from genesis: {len(signed)} lines"}


def verify_file(path: Path, key: str) -> dict[str, Any]:
    """Verify a complete JSONL audit in one streaming pass.

    This keeps memory constant for large/flooded audit files and, unlike a tail read,
    never reports an intact chain without seeing its genesis record.
    """
    checked = 0
    prev = ""
    last_seq: int | None = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, 1):
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                    seq = int(record["seq"])
                    receipt = str(record["receipt"])
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    return {"ok": False, "checked": checked, "first_break_seq": None,
                            "gap_before_seq": None,
                            "reason": f"malformed or unsigned audit record at line {line_no}"}
                expected_seq = 0 if last_seq is None else last_seq + 1
                if seq != expected_seq:
                    return {"ok": False, "checked": checked, "first_break_seq": None,
                            "gap_before_seq": seq,
                            "reason": f"seq gap before {seq} (expected {expected_seq})"}
                expected = sign(key, prev, canonical(record))
                if not hmac.compare_digest(expected, receipt):
                    return {"ok": False, "checked": checked, "first_break_seq": seq,
                            "gap_before_seq": None,
                            "reason": f"receipt mismatch at seq {seq} (line tampered)"}
                checked += 1
                last_seq = seq
                prev = receipt
    except OSError as exc:
        return {"ok": False, "checked": checked, "first_break_seq": None,
                "gap_before_seq": None, "reason": f"audit read failed: {exc}"}
    if checked == 0:
        return {"ok": False, "checked": 0, "first_break_seq": None,
                "gap_before_seq": None, "reason": "no signed audit lines"}
    return {"ok": True, "checked": checked, "first_break_seq": None,
            "gap_before_seq": None, "reason": f"chain intact from genesis: {checked} lines"}
