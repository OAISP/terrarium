"""Encrypted secret store — credentials at rest live ONLY here, encrypted.

Envelope encryption: each secret gets a fresh DEK (AES-256-GCM); the DEK is wrapped
by a KEK (``TERRA_KEK``, however your deployment supplies it). Plaintext exists only
transiently in orchestrator
RAM when provisioning a session's Warden credential. The store file sits on the PVC;
without the KEK it is opaque. The secret name is bound as AEAD associated-data, so a
record can't be silently moved to another name.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .store import JsonStore


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.b64decode(s)


def _derive_kek(raw: str) -> bytes:
    """Accept a 32-byte key (base64), else derive one from a passphrase via SHA-256."""
    try:
        b = base64.b64decode(raw)
        if len(b) == 32:
            return b
    except Exception:
        pass
    return hashlib.sha256(raw.encode()).digest()


class SecretStore(JsonStore):
    def __init__(self, path: Path, kek: str | None = None) -> None:
        super().__init__(path)
        kek = kek or os.environ.get("TERRA_KEK")
        if not kek:
            raise RuntimeError("TERRA_KEK not set — the secret store needs a key-encryption key")
        self._kek = AESGCM(_derive_kek(kek))
        self._data: dict[str, Any] = self._read({}) or {}

    def _save(self) -> None:
        self._write(self._data)

    def put(self, name: str, value: str) -> None:
        dek = AESGCM.generate_key(bit_length=256)
        v_nonce = os.urandom(12)
        ct = AESGCM(dek).encrypt(v_nonce, value.encode(), name.encode())  # name bound as AAD
        d_nonce = os.urandom(12)
        wrapped = self._kek.encrypt(d_nonce, dek, name.encode())
        with self.lock:
            self._data[name] = {
                "v_nonce": _b64e(v_nonce), "ct": _b64e(ct),
                "d_nonce": _b64e(d_nonce), "wrapped_dek": _b64e(wrapped),
            }
            self._save()

    def get(self, name: str) -> str | None:
        rec = self._data.get(name)
        if not rec:
            return None
        try:
            dek = self._kek.decrypt(_b64d(rec["d_nonce"]), _b64d(rec["wrapped_dek"]), name.encode())
            pt = AESGCM(dek).decrypt(_b64d(rec["v_nonce"]), _b64d(rec["ct"]), name.encode())
            return pt.decode()
        except Exception:
            return None  # wrong KEK / tampered / moved record → fail closed

    def list(self) -> list[str]:
        return list(self._data.keys())

    def delete(self, name: str) -> bool:
        with self.lock:
            if name in self._data:
                del self._data[name]
                self._save()
                return True
            return False
