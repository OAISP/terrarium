"""Worker-side concealment: scrub the orchestrator/Warden tells from the
process's own ``/proc/<pid>/environ`` so a same-uid agent can't fingerprint the
sandbox by reading its parent's environment.

Why a re-exec and not ``os.environ.pop``:
    The agent runs under the SAME uid as the worker, so it can open
    ``/proc/<worker-pid>/environ``. The kernel serves that file from the
    process's *original* exec-time stack region (``mm->env_start..env_end``),
    which libc ``unsetenv()`` / ``os.environ.pop`` never rewrites — so popping
    ``TERRA_HARNESS`` (which carries the full agent config) leaves it fully
    readable there. The only robust userspace fix is to re-exec once with a
    sanitized stack environment.

Mechanism (one extra exec on first launch, ~tens of ms):
    1. Unsealed launch: capture the tell vars (``TERRA_*`` / ``WARDEN_*`` /
       ``KUBERNETES_*``), stash them in an inherited ``memfd`` (fd 3, magic
       prefix), and ``execve`` ourselves with those keys stripped from the env.
    2. Sealed launch: read the memfd and re-inject the values with
       ``os.environ[k] = v`` — which lands on the *heap* (post-exec), invisible
       to ``/proc/environ`` — so the rest of the worker reads ``os.environ``
       exactly as before.

The functional proxy/CA vars (``HTTPS_PROXY``, ``NODE_EXTRA_CA_CERTS``,
``SSL_CERT_FILE`` …) are deliberately left in place: the deception target is
"a normal TLS-inspecting corporate host", not "no proxy present".
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SEAL_FD = 3
SEAL_MAGIC = b"\x00tseal1\x00"
TELL_PREFIXES = ("TERRA_", "WARDEN_", "KUBERNETES_")

# System CA bundles by distro (the sandbox image is Debian → first path).
SYSTEM_CA_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Debian/Ubuntu/Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL/Fedora
)
CA_BUNDLE_PATH = "/tmp/ca-bundle.pem"


def prepare_ca_bundle(wait_s: float = 5.0) -> str | None:
    """Concealment + correctness (F8/F9): present a COMBINED trust store
    (hundreds of real roots + the one session proxy CA) instead of replacing the
    trust store with a single self-signed cert.

    The single-cert store is both a loud MITM tell (no real machine trusts
    exactly one self-signed CA) AND a functional break: OpenSSL/requests/curl/git
    use SSL_CERT_FILE/REQUESTS_CA_BUNDLE as the WHOLE store, so TLS to allow-listed
    *tunnel* hosts (pip, MCP servers presenting their real cert) fails. A combined
    bundle reads exactly like a normal corporate TLS-inspection host and lets
    tunneled TLS validate against real roots. Node keeps NODE_EXTRA_CA_CERTS
    (which APPENDS) pointing at the session CA only.

    Returns the bundle path if one was built, else None (no MITM, or no CA/roots).
    """
    ca = os.environ.get("NODE_EXTRA_CA_CERTS") or os.environ.get("SSL_CERT_FILE")
    if not ca:
        return None  # session CA unavailable → leave the default system store
    # Warden writes the per-session CA right at startup; tolerate a small race.
    deadline = time.monotonic() + wait_s
    while not (os.path.exists(ca) and os.path.getsize(ca) > 0):
        if time.monotonic() >= deadline:
            return None  # CA never appeared — leave SSL_CERT_FILE as-is (fail like today)
        time.sleep(0.05)
    system_ca = next((p for p in SYSTEM_CA_CANDIDATES if os.path.exists(p)), None)
    if not system_ca:
        return None
    try:
        bundle = Path(system_ca).read_text() + "\n" + Path(ca).read_text()
        Path(CA_BUNDLE_PATH).write_text(bundle)
        os.chmod(CA_BUNDLE_PATH, 0o644)
    except OSError:
        return None  # best-effort; on failure keep the single-CA behavior
    # OpenSSL (python/requests), curl, and git all read the WHOLE store from these —
    # point them at the combined bundle so real roots AND the proxy CA both validate.
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
        os.environ[var] = CA_BUNDLE_PATH
    # NODE_EXTRA_CA_CERTS stays = session CA (Node appends it to its built-in roots).
    return CA_BUNDLE_PATH


def write_decoy_creds() -> str | None:
    """Subscription-path decoy: write ``TERRA_DECOY_OAUTH`` (a stub
    ~/.claude/.credentials.json handed in by the k8s runner) so the CLI emits an
    OAuth-shaped request for Warden to inject the real subscription token into.

    Runs AFTER ``conceal_env`` (which re-injects ``TERRA_*`` onto the heap), so the
    value is readable here but is NOT in /proc/environ and is stripped from the
    child CLI's env. Returns the path written, or None if no decoy was provided
    (API-key mode, or the Docker runner which mounts the stub instead).
    """
    blob = os.environ.get("TERRA_DECOY_OAUTH")
    if not blob:
        return None
    home = os.environ.get("HOME", "/home/agent")
    dest = Path(home) / ".claude" / ".credentials.json"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(blob)
        os.chmod(dest, 0o600)
    except OSError:
        return None
    return str(dest)


def _read_all(fd: int) -> bytes:
    out = b""
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        out += chunk
    return out


def _carrier_fd(data: bytes) -> int:
    """An inheritable, rewound fd holding ``data`` with no filesystem name.

    Prefers ``memfd_create`` (anonymous, RAM-backed); falls back to an
    immediately-unlinked temp file for Python builds/kernels without memfd
    (e.g. portable interpreters linked against older glibc). No size limit and
    no blocking either way — unlike a pipe."""
    if hasattr(os, "memfd_create"):
        fd = os.memfd_create("w", 0)
    else:
        import tempfile
        fd, path = tempfile.mkstemp(prefix=".", dir=os.environ.get("TMPDIR", "/tmp"))
        os.unlink(path)  # anonymous inode — reachable only via this fd
    os.write(fd, data)
    os.lseek(fd, 0, os.SEEK_SET)
    os.set_inheritable(fd, True)
    return fd


def conceal_env() -> None:
    """Idempotent: re-exec once to sanitize the stack env, else re-inject + return.

    Honors ``TERRA_NO_RESEAL=1`` as a test/debug escape hatch (skips the re-exec).
    """
    if os.environ.get("TERRA_NO_RESEAL") == "1":
        return

    # Already sealed? fd 3 is the inherited memfd carrying the captured tells.
    try:
        os.fstat(SEAL_FD)
        blob = _read_all(SEAL_FD)
        os.close(SEAL_FD)
        if blob.startswith(SEAL_MAGIC):
            for k, v in json.loads(blob[len(SEAL_MAGIC):]).items():
                os.environ[k] = v  # heap (post-exec) → NOT in /proc/environ
            return
    except OSError:
        pass  # fd 3 not open / not ours → unsealed, fall through

    # Unsealed: capture the tells, re-exec with a clean stack environment.
    captured = {k: v for k, v in os.environ.items() if k.startswith(TELL_PREFIXES)}
    fd = _carrier_fd(SEAL_MAGIC + json.dumps(captured).encode())
    clean = {k: v for k, v in os.environ.items() if not k.startswith(TELL_PREFIXES)}
    if fd != SEAL_FD:
        os.dup2(fd, SEAL_FD)
        os.close(fd)
    os.execve(sys.executable, [sys.executable, *sys.argv], clean)  # fds 0/1/2 (stdio) preserved
