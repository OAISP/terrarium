"""Operator-defined injection secrets — the general form of the Anthropic bearer-swap.

A secret is { name, host scopes, header, value-template, value }. Warden injects the
templated value into the named header on every MITM'd request to a scoped host, so the
real value lives only in Warden (and here, encrypted) — never in the sandbox.

Split storage, mirroring the existing credential design:
  - the VALUE is encrypted at rest in the shared ``SecretStore`` (envelope AES-GCM,
    name-bound), keyed ``usersecret:<name>``;
  - the METADATA (scopes, header, template) is a plain JSON index — not secret.

The Anthropic credential stays a separate MANAGED secret (it has OAuth refresh/rotation);
these are static operator secrets that ride the same Warden injection map.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re

from .egress import clean_hosts
from .secret_store import SecretStore
from .store import JsonStore

_PREFIX = "usersecret:"
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class SecretMeta:
    name: str
    scopes: list[str] = field(default_factory=list)  # hosts (exact match, egress hygiene)
    header: str = "Authorization"                     # header to set on matching requests
    template: str = "Bearer {value}"                  # value template; {value} = the secret
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


class UserSecretStore(JsonStore):
    def __init__(self, index_path: Path, store: SecretStore) -> None:
        super().__init__(index_path)
        self.store = store
        self._meta: dict[str, SecretMeta] = {
            d["name"]: SecretMeta(**{k: v for k, v in d.items() if k in SecretMeta.__annotations__})
            for d in (self._read([]) or [])
        }

    def _save(self) -> None:
        self._write([asdict(m) for m in self._meta.values()])

    def list(self) -> list[dict]:
        """Metadata only — the value is NEVER returned (it only ever leaves via Warden)."""
        return [{**asdict(m), "has_value": self.store.get(_PREFIX + m.name) is not None}
                for m in self._meta.values()]

    def put(self, name: str, *, scopes, header: str, template: str = "Bearer {value}",
            value: str | None = None, enabled: bool = True) -> dict:
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        if not isinstance(header, str):
            raise ValueError("header must be a string")
        header = (header or "").strip()
        if not _HEADER_TOKEN.fullmatch(header):
            raise ValueError("header must be a valid HTTP field name")
        if not isinstance(template, str):
            raise ValueError("template must be a string")
        template = template or "{value}"
        if "{value}" not in template:
            raise ValueError("template must contain {value}")
        if "\r" in template or "\n" in template:
            raise ValueError("template must not contain line breaks")
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("value must be a string")
            if "\r" in value or "\n" in value:
                raise ValueError("value must not contain line breaks")
        hosts = clean_hosts(scopes)
        with self.lock:
            prev = self._meta.get(name)
            if value is None and prev is None:
                raise ValueError("value is required when creating a secret")
            m = SecretMeta(
                name=name, scopes=hosts, header=header, template=template, enabled=enabled,
                created_at=(prev.created_at if prev else _now()), updated_at=_now(),
            )
            self._meta[name] = m
            self._save()
            if value is not None:  # omit on edit to keep the existing value
                self.store.put(_PREFIX + name, value)
        return asdict(m)

    def delete(self, name: str) -> bool:
        with self.lock:
            if name not in self._meta:
                return False
            del self._meta[name]
            self._save()
        self.store.delete(_PREFIX + name)
        return True

    def warden_payload(self, names: "set[str] | None" = None) -> dict:
        """The WARDEN_SECRETS file content: {"secrets": [{hosts, header, value}]} with the
        template applied. Values are decrypted here (orchestrator RAM) and travel only to
        Warden's mounted file — never to the sandbox.

        ``names`` scopes the payload to a specific set of secret names (an agent's attached
        environments); ``None`` is reserved for trusted internal callers that explicitly
        request every enabled secret."""
        rules = []
        for m in self._meta.values():
            if not m.enabled or not m.scopes:
                continue
            if names is not None and m.name not in names:
                continue
            val = self.store.get(_PREFIX + m.name)
            if not val:
                continue
            rules.append({"hosts": m.scopes, "header": m.header, "value": m.template.replace("{value}", val)})
        return {"secrets": rules}

    def scope_hosts(self, names: "set[str] | None" = None) -> list[str]:
        """Every host the selected enabled secrets target — these must be MITM'd so injection
        can happen, so the manager folds them into each session's Warden policy. ``names``
        scopes to a secret set (an agent's environments); ``None`` = all enabled secrets.

        Mirrors ``warden_payload``: a secret with no value yet injects nothing, so don't force
        MITM of its hosts for it (an unset secret shouldn't silently start intercepting hosts)."""
        hosts: list[str] = []
        for m in self._meta.values():
            if not (m.enabled and (names is None or m.name in names)):
                continue
            if not self.store.get(_PREFIX + m.name):  # value-less → nothing to inject
                continue
            hosts.extend(m.scopes)
        return sorted(set(hosts))
