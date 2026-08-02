"""Mediated file bridge — the only host↔container file movement.

Uses `docker cp` (no host bind mounts). Bytes in, bytes out: the orchestrator
never grants the sandbox a host path, and copy-out reads one named file from the
session workspace, refusing symlinks and traversal.

Caps exist on both directions. Upload is bounded at the API edge (the request
body); download is bounded here, because the size is chosen by the *agent* —
without a cap, an untrusted sandbox could answer a download with an arbitrarily
large file and exhaust the single control plane's memory.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

#: Largest artifact the orchestrator will pull out of a sandbox, in bytes. Mirrors the
#: upload cap; both are held wholly in RAM on the way through.
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024


def _safe_name(name: str) -> str:
    if not _SAFE_NAME.match(name) or name in (".", ".."):
        raise ValueError(f"unsafe file name: {name!r}")
    return name


def sanitize_name(name: str) -> str:
    """Lenient name for uploads: strip any path, replace unsafe chars — so traversal
    is impossible but an ordinary filename (spaces, etc.) still works."""
    base = name.replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")
    return (base or "upload.bin")[:200]


async def _cp(src: str, dst: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker", "cp", src, dst,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"docker cp failed: {out.decode(errors='replace').strip()}")


async def copy_in_bytes(container: str, data: bytes, dest_name: str) -> str:
    """Write uploaded bytes into the session's /workspace (no host path needed)."""
    name = sanitize_name(dest_name)
    tmp = Path(tempfile.mkdtemp(prefix="ccfb-"))
    try:
        (tmp / name).write_bytes(data)
        await _cp(str(tmp / name), f"{container}:/workspace/{name}")
        return name
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def copy_out(container: str, name: str) -> bytes:
    """Read a named file from the session's /workspace, rejecting symlinks."""
    name = _safe_name(name)
    tmp = Path(tempfile.mkdtemp(prefix="ccfb-"))
    try:
        await _cp(f"{container}:/workspace/{name}", str(tmp / name))
        target = tmp / name
        # is_symlink FIRST: is_file() follows the link, so a symlink to /etc/passwd
        # would pass it. docker cp copies a symlink as a symlink, so this is the check
        # that keeps the agent from naming a file outside its workspace.
        if target.is_symlink() or not target.is_file():
            raise ValueError("refusing non-regular file")
        size = target.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"file too large ({size} bytes, max {MAX_DOWNLOAD_BYTES})")
        return target.read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
