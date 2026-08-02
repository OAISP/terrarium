"""Scoped API tokens — capability tokens layered over the root ``TERRA_TOKEN``.

The root token (``config.auth_token``) is the admin: all scopes. This store holds
additional *named* tokens, each with a scope set, so a CI/cron caller can run
agents without read/rotate access to the Claude credential or the power to mint
new tokens. Tokens are stored **hashed**; the raw value is shown once at create.

Scopes (``admin`` implies all):
  • ``read``  — read-only (list/get).
  • ``run``   — drive sessions/agents/schedules.
  • ``admin`` — everything, incl. credentials + token management.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from terracore.events import now_iso

from .store import JsonStore

SCOPES = ("read", "run", "admin")
# Ordered privilege ladder: a higher scope satisfies every lower one (run can
# read, admin can do anything). Without this, a "run" token would be rejected by
# read-gated GETs and least-privilege couldn't be expressed.
_SCOPE_RANK = {"read": 0, "run": 1, "admin": 2}


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class Principal:
    name: str
    scopes: set[str]

    def has(self, scope: str) -> bool:
        want = _SCOPE_RANK.get(scope, 99)
        return any(_SCOPE_RANK.get(s, -1) >= want for s in self.scopes)


@dataclass
class TokenRecord:
    id: str
    name: str
    token_hash: str
    scopes: list[str]
    created_at: str = field(default_factory=now_iso)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "scopes": self.scopes, "created_at": self.created_at}


class TokenStore(JsonStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._items: dict[str, TokenRecord] = {}
        self._by_hash: dict[str, TokenRecord] = {}
        for tid, raw in (self._read({}) or {}).items():
            r = TokenRecord(id=tid, name=raw["name"], token_hash=raw["token_hash"],
                            scopes=raw.get("scopes", []), created_at=raw.get("created_at", now_iso()))
            self._items[tid] = r
            self._by_hash[r.token_hash] = r

    def _save(self) -> None:
        self._write({tid: {"name": r.name, "token_hash": r.token_hash,
                           "scopes": r.scopes, "created_at": r.created_at}
                     for tid, r in self._items.items()})

    def create(self, name: str, scopes: list[str]) -> tuple[TokenRecord, str]:
        """Returns (record, raw_token). The raw token is shown ONCE."""
        raw = "terra_" + secrets.token_urlsafe(32)
        scopes = [s for s in scopes if s in SCOPES] or ["read"]
        with self.lock:
            r = TokenRecord(id="tok_" + uuid.uuid4().hex[:8], name=name,
                            token_hash=hash_token(raw), scopes=scopes)
            self._items[r.id] = r
            self._by_hash[r.token_hash] = r
            self._save()
            return r, raw

    def verify(self, raw: str) -> Principal | None:
        r = self._by_hash.get(hash_token(raw))
        return Principal(name=r.name, scopes=set(r.scopes)) if r else None

    def list(self) -> list[dict[str, Any]]:
        return [r.public() for r in self._items.values()]

    def delete(self, tid: str) -> bool:
        with self.lock:
            r = self._items.pop(tid, None)
            if not r:
                return False
            self._by_hash.pop(r.token_hash, None)
            self._save()
            return True
