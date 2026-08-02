"""Per-session Warden sidecar — provisions the session CA + scoped credential,
runs the Rust Warden (MITM egress gateway) on the agent network, and resolves its
endpoint so DockerRunner can point the worker's HTTPS_PROXY at it.

Per-session (unlike the shared gateway): each session gets its own ephemeral CA
and only its own credential. The credential lives plaintext only in the cred file
(mounted read-only into Warden) and in Warden's RAM — never in the worker.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from .config import Config
from .receipts import persist_receipt_key


class WardenController:
    # Paths INSIDE the Warden container. Nothing here is a host path: the bootstrap tree is
    # copied in and the audit is copied out, so the orchestrator's own filesystem namespace
    # never has to agree with the daemon's. That agreement is what broke every named-volume
    # deployment, and it is unobtainable under Docker Desktop or a remote DOCKER_HOST.
    C_CRED = "/wstate/cred.json"
    C_SECRETS = "/wstate/secrets.json"
    C_POLICY = "/egress/policy.json"
    C_CA_PEM = "/ca/session-ca.pem"

    def __init__(self, config: Config, session_id: str, cred: dict | None,
                 policy_json: str | None = None, network: str | None = None,
                 secrets_json: str | None = None) -> None:
        self.config = config
        self.sid = session_id
        self.cred = cred  # {"type": "bearer"|"apikey", "value": ...} or None
        self.policy_json = policy_json  # per-session resolved policy (profile/global), or None
        self.secrets_json = secrets_json  # operator injection secrets ({"secrets":[…]}), or None
        # Per-session network (isolates this credential-injecting proxy so no other
        # container can route through it); falls back to the shared net for callers
        # that only reap (stop() doesn't touch the network).
        self.network = network or config.network
        self.name = "terrarium-warden-" + session_id.replace("_", "-")[:24]
        self.egress_dir = config.egress_dir
        # Per-session audit file (under the durable egress_dir, NOT the session dir that
        # stop() reaps) so concurrent sessions get independent, individually-verifiable
        # HMAC chains AND the trail persists for post-mortem `verify-egress`.
        self.audit_dir = config.egress_dir / "audit"
        self.C_AUDIT = f"/egress/audit/{session_id}.jsonl"
        self.ca_pem: bytes | None = None   # copied out of the container once Warden mints it
        self.audit_file = self.audit_dir / f"{session_id}.jsonl"
        # Persist (don't discard) the key so the chain can be verified later.
        self.receipt_key = persist_receipt_key(config, session_id)
        self.endpoint: tuple[str, int] | None = None

    async def _docker(self, *args: str) -> tuple[int, str]:
        p = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await p.communicate()
        return p.returncode or 0, out.decode(errors="replace")

    async def _why_dead(self) -> str:
        """Why the container is not up, from the container itself.

        `could not resolve IP` is a symptom: a container that exited has no address. The cause
        is in its logs and exit code, and the old code removed the container before reading
        either — so a missing cred file, a read-only mount and an unreachable registry all
        produced the same unhelpful line."""
        rc, state = await self._docker("inspect", "-f", "{{.State.Status}} exit={{.State.ExitCode}}",
                                       self.name)
        rc2, logs = await self._docker("logs", "--tail", "5", self.name)
        bits = []
        if rc == 0 and state.strip():
            bits.append(state.strip())
        if rc2 == 0 and logs.strip():
            bits.append("log: " + " | ".join(logs.strip().splitlines()[-3:]))
        return "; ".join(bits) or "no diagnostics available"

    async def _ip(self) -> str | None:
        for _ in range(20):
            rc, out = await self._docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", self.name)
            if rc == 0:
                try:
                    for net in json.loads(out.strip()).values():
                        if net.get("IPAddress"):
                            return net["IPAddress"]
                except Exception:
                    pass
            await asyncio.sleep(0.3)
        return None

    async def _cp_in(self, src: Path, dest: str) -> bool:
        """`docker cp` into this container. Works on a created-but-not-started container and
        on a running one; a missing source is an error rather than a silently created dir."""
        rc, out = await self._docker("cp", str(src), f"{self.name}:{dest}")
        if rc != 0:
            print(f"[warden] cp {src.name} → {dest} failed: {out.strip()}", file=sys.stderr)
        return rc == 0

    async def _push_file(self, name: str, dest: str, data: str, mode: int = 0o600) -> bool:
        """Stage one file in a private temp dir and copy it in. The staging dir is the only
        place the plaintext credential ever touches this filesystem, and it is removed
        immediately — the old design left it in the session dir for the session's lifetime."""
        with tempfile.TemporaryDirectory(prefix="wstage-") as td:
            p = Path(td) / name
            p.write_text(data)
            os.chmod(p, mode)
            return await self._cp_in(p, dest)

    async def start(self) -> tuple[str, int] | None:
        c = self.config
        # The audit is drained onto the orchestrator's own volume; only that dir is ours.
        self.audit_dir.mkdir(parents=True, exist_ok=True)

        rc, _ = await self._docker("network", "inspect", self.network)
        if rc != 0:
            await self._docker("network", "create", self.network)
        await self._docker("rm", "-f", self.name)

        # `create`, not `run`: the bootstrap files are copied in before the process exists, so
        # Warden can never observe a missing cred.json. That race was previously avoided only
        # because the mount was populated first.
        args = [
            "create", "--name", self.name, "--network", self.network,
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--restart", "no",
            # Bound resources to match the k8s sidecar (which already sets these), so a
            # worker-controlled SNI flood (each new host mints + caches a leaf) can't
            # exhaust the host via the Warden container.
            "--memory=256m", "--cpus=0.5", "--pids-limit=128",
            # Run as the orchestrator's uid: `docker cp` preserves source ownership, so the
            # files land owned by this uid and Warden reads them without CAP_DAC_OVERRIDE
            # (which cap-drop removes).
            "--user", f"{os.getuid()}:{os.getgid()}",
            # 0.0.0.0 (the sandbox reaches Warden by container IP across the bridge,
            # so loopback won't do) — safe because the bridge is per-session: only
            # this session's sandbox is on it.
            "-e", f"WARDEN_LISTEN=0.0.0.0:{c.warden_port}",
            "-e", "WARDEN_CA_DIR=/ca",
            # One policy path now. The old build chose between a per-session mount and the
            # shared one; with a copy there is nothing to share, so the per-session content
            # (profile-resolved, else a snapshot of the global file) always lands here.
            "-e", f"WARDEN_POLICY={self.C_POLICY}",
            "-e", f"WARDEN_AUDIT={self.C_AUDIT}",
            "-e", f"WARDEN_RECEIPT_KEY={self.receipt_key}",
            "-e", f"WARDEN_CRED={self.C_CRED}",
            "-e", f"WARDEN_SECRETS={self.C_SECRETS}",
            "--entrypoint", "/opt/runtime/zstunnel", c.image,
        ]
        rc, out = await self._docker(*args)
        if rc != 0:
            print(f"[warden] failed to create: {out.strip()}", file=sys.stderr)
            return None

        if not await self._seed():
            await self._docker("rm", "-f", self.name)
            return None

        rc, out = await self._docker("start", self.name)
        if rc != 0:
            print(f"[warden] failed to start: {out.strip()}", file=sys.stderr)
            await self._docker("rm", "-f", self.name)
            return None

        ip = await self._ip()
        if not ip:
            # Read the diagnostics BEFORE removing the container, or the reason is lost.
            print(f"[warden] no IP — sidecar is not running: {await self._why_dead()}",
                  file=sys.stderr)
            await self._docker("rm", "-f", self.name)
            return None
        # The worker must trust the CA before it starts, and the CA now lives inside the
        # container, so copy it out rather than watching a shared directory.
        self.ca_pem = await self._await_ca()
        if not self.ca_pem:
            print(f"[warden] CA never appeared: {await self._why_dead()}", file=sys.stderr)
            await self._docker("rm", "-f", self.name)
            return None
        self.endpoint = (ip, c.warden_port)
        print(f"[warden] {ip}:{c.warden_port} for session {self.sid}", file=sys.stderr)
        return self.endpoint

    async def _seed(self) -> bool:
        """Copy the bootstrap tree in before the process starts. Directories are copied whole
        so `docker cp` creates them; the image is not required to carry empty mount points."""
        policy = self.policy_json
        if policy is None:  # no resolved profile → snapshot the global file at start time
            try:
                policy = (self.egress_dir / "policy.json").read_text()
            except OSError:
                policy = json.dumps({"mode": "enforce", "rules": []})
            self.policy_json = policy
        with tempfile.TemporaryDirectory(prefix="wboot-") as td:
            root = Path(td)
            (root / "wstate").mkdir()
            (root / "wstate" / "cred.json").write_text(json.dumps(self.cred or {"disabled": True}))
            (root / "wstate" / "secrets.json").write_text(
                self.secrets_json or json.dumps({"secrets": []}))
            os.chmod(root / "wstate" / "cred.json", 0o600)
            os.chmod(root / "wstate" / "secrets.json", 0o600)
            (root / "ca").mkdir()
            (root / "egress" / "audit").mkdir(parents=True)
            (root / "egress" / "policy.json").write_text(policy)
            for d in ("wstate", "ca", "egress"):
                if not await self._cp_in(root / d, "/"):
                    return False
        return True

    async def _await_ca(self) -> bytes | None:
        """Poll the CA out of the container. Warden mints it at startup, so this is the
        readiness signal as well as the artifact the sandbox needs."""
        with tempfile.TemporaryDirectory(prefix="wca-") as td:
            dst = Path(td) / "session-ca.pem"
            for _ in range(50):
                rc, _ = await self._docker("cp", f"{self.name}:{self.C_CA_PEM}", str(dst))
                if rc == 0 and dst.exists() and dst.stat().st_size > 0:
                    return dst.read_bytes()
                await asyncio.sleep(0.1)
        return None

    async def drain_audit(self) -> None:
        """Mirror the in-container audit chain onto the orchestrator's own volume, so
        `verify-egress` still works after the sandbox is gone. Previously the shared bind
        mount made this free; a copy is the price of not needing host paths."""
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        rc, out = await self._docker("cp", f"{self.name}:{self.C_AUDIT}", str(self.audit_file))
        if rc != 0 and "No such container" not in out and "Could not find" not in out:
            print(f"[warden] audit drain failed: {out.strip()}", file=sys.stderr)

    async def update_policy(self, policy_json: str) -> None:
        """Push the policy into the running container. `docker cp` rewrites in place — it
        bumps mtime and preserves the inode — so Warden's mtime hot-reload fires and there is
        no bind-mount inode pinning to work around."""
        self.policy_json = policy_json
        await self._push_file("policy.json", self.C_POLICY, policy_json, 0o644)

    async def update_cred(self, cred: dict | None) -> None:
        """Push a refreshed credential into the running session, so a long Docker+subscription
        session does not 401 when its start-time token expires."""
        self.cred = cred
        await self._push_file("cred.json", self.C_CRED, json.dumps(cred or {"disabled": True}))

    async def update_secrets(self, secrets_json: str | None) -> None:
        """Push edited operator injection secrets — same channel as the credential."""
        self.secrets_json = secrets_json or json.dumps({"secrets": []})
        await self._push_file("secrets.json", self.C_SECRETS, self.secrets_json)

    async def stop(self) -> None:
        await self.drain_audit()   # last write wins; the container is about to go
        await self._docker("rm", "-f", self.name)
