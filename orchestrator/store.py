"""One durable JSON document on the runtime volume.

Every persistent store in the orchestrator — agents, schedules, tokens, egress
profiles, environments, the secret index and the sealed vault — is the same thing:
a JSON file behind a lock, rewritten whole via a temp file and an atomic rename so
a reader (or Warden) never sees a partial document.

The 0600 mode lives here rather than in each store because it is a property of
"durable orchestrator state", not of any one document — and the consequences of
missing it are uneven enough to be easy to overlook. ``agents.json`` persists
``harness.env`` (Session._safe_harness_json strips exactly that field from the
registry BECAUSE it carries secrets); ``tokens.json`` holds API-token hashes.

Serialization stays with each store: some key by id, some nest under a key, one is
a bare list. That difference is real and not worth abstracting away.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("terrarium.store")


class StoreCorruptionError(RuntimeError):
    """Durable control-plane state could not be decoded or recovered."""


class JsonStore:
    """Base for a lock-guarded JSON file. Subclasses own the document's shape.

    Use ``self.lock`` around read-modify-write sequences; ``_read``/``_write`` do
    not take it themselves, so a subclass can hold it across both halves.
    """

    #: Written on every save. 0600 by default — this is credential-adjacent state.
    mode: int = 0o600

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _read(self, default: Any = None) -> Any:
        """Parse the document, recovering from the previous fsynced generation.

        Missing state is normal on first boot. Corrupt policy/identity state is
        never silently treated as an empty configuration: use the backup and log
        loudly, or fail startup with the exact path.
        """
        if not self.path.exists():
            return default
        try:
            raw = self.path.read_text()
            if not raw.strip():
                raise ValueError("empty document")
            data = json.loads(raw)
            return data if data is not None else default
        except Exception as exc:
            backup = self.path.with_name(self.path.name + ".bak")
            try:
                raw = backup.read_text()
                if not raw.strip():
                    raise ValueError("empty backup")
                data = json.loads(raw)
            except Exception as backup_exc:
                raise StoreCorruptionError(
                    f"cannot read durable store {self.path}; backup recovery failed"
                ) from backup_exc
            log.error("recovered corrupt store %s from %s: %s", self.path, backup, exc)
            return data if data is not None else default

    def _write(self, data: Any) -> None:
        encoded = json.dumps(data, indent=2).encode()
        tmp = self.path.with_name(self.path.name + ".tmp")
        backup = self.path.with_name(self.path.name + ".bak")

        # Preserve only a known-good prior generation. Never replace a usable
        # backup with already-corrupt primary bytes.
        if self.path.exists():
            try:
                previous = self.path.read_bytes()
                json.loads(previous)
            except Exception:
                previous = b""
            if previous:
                backup_tmp = backup.with_name(backup.name + ".tmp")
                with backup_tmp.open("wb") as fh:
                    fh.write(previous)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(backup_tmp, self.mode)
                backup_tmp.replace(backup)

        with tmp.open("wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, self.mode)
        tmp.replace(self.path)  # atomic — a reader never sees a half-written file
        try:
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
