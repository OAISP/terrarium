"""Runners — how a session's agent worker actually executes.

`DockerRunner` launches the hardened sandbox image (the production path).
`LocalRunner` runs the worker directly on the host with NO isolation (dev only,
behind an explicit unsafe gate). Both speak the same stdio JSON-lines protocol,
so the SessionManager doesn't care which it gets.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator

from terracore import protocol as P
from terracore.harness import Harness

from .agents import AgentSpec
from .config import (AGENT_CA_DIR, AGENT_CA_FILE, REPO_ROOT, Config, decoy_api_key,
                     decoy_oauth_stub, managed_creds)

WORKER_PATH = REPO_ROOT / "sandbox" / "worker.py"

# Raw cap on a single line of (untrusted) worker stdout before we drop it. The
# parsed event is further bounded by protocol.MAX_EVENT_BYTES.
_MAX_STDOUT_LINE = int(os.environ.get("TERRA_MAX_STDOUT_LINE", str(4 << 20)))  # 4 MiB
_MAX_STDERR_LINE = int(os.environ.get("TERRA_MAX_STDERR_LINE", str(256 << 10)))  # 256 KiB


async def _bounded_lines(
    reader: asyncio.StreamReader, limit: int,
) -> AsyncIterator[tuple[bytes | None, int]]:
    """Yield bounded lines from an untrusted stream.

    ``StreamReader.readline`` and its async iterator raise ``LimitOverrunError`` on
    a long line. Reading chunks keeps memory bounded; an oversized line is discarded
    through its next newline and represented as ``(None, byte_count)``.
    """
    buf = bytearray()
    dropping = 0
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        if dropping:
            nl = chunk.find(b"\n")
            if nl < 0:
                dropping += len(chunk)
                continue
            dropping += nl
            yield None, dropping
            dropping = 0
            chunk = chunk[nl + 1:]
        buf.extend(chunk)
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                if len(buf) > limit:
                    dropping = len(buf)
                    buf.clear()
                break
            raw = bytes(buf[:nl])
            del buf[: nl + 1]
            if len(raw) > limit:
                yield None, len(raw)
            else:
                yield raw, 0
    if dropping:
        yield None, dropping
    elif buf:
        if len(buf) > limit:
            yield None, len(buf)
        else:
            yield bytes(buf), 0


@dataclass
class SessionConfig:
    harness: Harness = field(default_factory=Harness)
    title: str | None = None
    memory_volume: str = "terrarium-memory"
    agent_id: str | None = None
    memory_isolated: bool = False  # this session got its own per-session memory (concurrent run)
    egress_policy_json: str | None = None  # resolved per-session Warden policy (profile or global)
    warden_secrets_json: str | None = None  # operator-defined injection secrets for Warden ({"secrets":[…]})

    @property
    def model(self) -> str:
        return self.harness.model

    @property
    def system_mode(self) -> str:
        return self.harness.system_mode

    @classmethod
    def from_agent(cls, spec: AgentSpec, title: str | None = None) -> "SessionConfig":
        # Snapshot the harness (don't share the AgentSpec's object): a session is an
        # immutable config snapshot taken at launch, so a later agent edit can't silently
        # mutate a running session's recorded config. Hot-appliable fields are propagated
        # explicitly + intentionally via SessionManager.propagate_agent_harness.
        return cls(
            harness=replace(spec.harness),
            title=title,
            memory_volume=spec.memory_volume(),
            agent_id=spec.id,
        )


def hardened_flags(network: str, runtime: str | None = None) -> list[str]:
    """Security flags shared by the launcher and the red-team test.

    The agent ends up non-root with zero effective caps; NET_ADMIN/SETUID/SETGID
    are consumed by the root entrypoint (firewall + privilege drop) only.
    """
    flags = [
        "--network", network,
        "--cap-drop=ALL",
        "--cap-add=NET_ADMIN", "--cap-add=SETUID", "--cap-add=SETGID",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
        "--tmpfs", "/home/agent:rw,nosuid,nodev,uid=1001,gid=1001,size=256m",
        "--pids-limit=512",
        "--memory=2g",
        "--cpus=2",
    ]
    if runtime:
        flags += ["--runtime", runtime]
    return flags


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def ensure_network(network: str) -> None:
    rc, _ = await _run(["docker", "network", "inspect", network])
    if rc != 0:
        await _run(["docker", "network", "create", network])


def session_network(config: Config, session_id: str) -> str:
    """A per-session bridge shared ONLY by this session's sandbox + its Warden.

    The Warden's proxy authenticates nothing and injects the REAL credential, so it
    must not sit on a network any other container can reach. The sandbox's own egress
    is already pinned to the Warden pinhole by the firewall; a dedicated per-session
    network closes the same hole for everything else (another session's sandbox, or any
    co-located container). Reaped in ``DockerRunner.stop`` (name is deterministic so a
    reattached runner reaps it too)."""
    sid = session_id.replace("_", "-")[:32]
    return f"{config.network}-{sid}"


async def ensure_volume(name: str) -> None:
    rc, _ = await _run(["docker", "volume", "inspect", name])
    if rc != 0:
        await _run(["docker", "volume", "create", name])


class Runner(ABC):
    # True only for runners whose worker survives an orchestrator restart and can
    # be reattached (K8sRunner). Non-durable runners are torn down on shutdown so
    # their session is written terminal, never left dangling as "running".
    durable: bool = False

    # Every runner sets both in __init__; declared here because the shared audit
    # helpers below need them.
    session_id: str
    config: Config

    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def send(self, cmd: dict[str, Any]) -> None: ...
    @abstractmethod
    def events(self) -> AsyncIterator[dict[str, Any]]: ...
    @abstractmethod
    async def stop(self) -> None: ...

    async def detach(self) -> None:
        """Release the worker on graceful shutdown WITHOUT preserving it.

        Default is a full ``stop``; ``K8sRunner`` overrides this to leave the
        sandbox Pod running so a restarted orchestrator can reattach to it.
        """
        await self.stop()

    async def update_warden_policy(self, policy_json: str) -> None:
        """Push a changed egress policy to this running session's Warden. No-op by
        default — the Docker Warden mounts the shared policy.json and hot-reloads it
        itself; ``K8sRunner`` overrides this to patch the per-session ConfigMap."""
        return

    async def snapshot_memory(self) -> None:
        """Persist /memory out of the sandbox. No-op by default — a mounted volume IS the
        persistence, so only ``K8sRunner`` in memory_mode="synced" (which mounts nothing) has
        anything to do here."""
        return

    async def read_egress_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        """This session's Warden egress decisions, read from the orchestrator's own
        runtime volume — never from the sandbox. Runner-independent: the Docker Warden
        appends there directly, and ``K8sRunner.drain_audit`` mirrors its in-Pod audit
        into the same file. So this works identically for a live session and a
        post-mortem one (the file outlives the sandbox)."""
        from .egress import read_audit, session_audit_path

        return await asyncio.to_thread(read_audit, session_audit_path(self.config, self.session_id), limit)

    async def drain_audit(self) -> None:
        """Mirror this session's Warden audit onto the orchestrator's runtime volume.
        No-op by default — the Docker Warden already appends straight to the shared
        mount, so only ``K8sRunner`` (whose audit is trapped in the Warden container's
        emptyDir) has anything to copy."""
        return

    async def update_warden_cred(self) -> None:
        """Refresh this running session's Warden credential. No-op by default;
        ``K8sRunner`` overrides it to patch the per-session cred Secret (Warden
        hot-reloads it), so a token refresh/re-paste reaches a running session."""
        return

    async def update_warden_secrets(self, secrets_json: str) -> None:
        """Push edited operator injection secrets to this running session's Warden.
        No-op by default; the Docker/K8s runners override to rewrite the mounted file /
        patch the Secret (Warden hot-reloads by mtime)."""
        return

    async def copy_out_bytes(self, name: str) -> bytes:
        """Read one file out of the session's /workspace. Every runner implements this —
        an agent's output artifacts are the point of running it, and for a long time the
        only way to retrieve one was a docker-only endpoint nothing called.

        Implementations must reject symlinks (the sandbox is untrusted, so the name and
        the link target are both agent-chosen) and cap the size."""
        raise NotImplementedError


class _PipeRunner(Runner):
    """Shared stdio plumbing: merge stdout (events) + stderr (logs) into one
    async event stream, sentinel-terminated."""

    proc: asyncio.subprocess.Process | None = None

    def _init_pipes(self) -> None:
        self._q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._readers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw, dropped in _bounded_lines(self.proc.stdout, _MAX_STDOUT_LINE):
                if raw is None:
                    await self._q.put({"type": "error", "subtype": "oversized_stdout",
                                       "detail": f"dropped {dropped}-byte line"})
                    continue
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    await self._q.put(json.loads(line))
                except Exception:
                    await self._q.put({"type": "system", "subtype": "stdout",
                                       "data": {"line": line[:8192]}})
        except Exception as exc:  # noqa: BLE001 — a reader failure must still close the merged stream
            await self._q.put({"type": "error", "subtype": "stdout_reader",
                               "detail": str(exc)[:1024]})
        finally:
            self._q.put_nowait(None)

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            async for raw, dropped in _bounded_lines(self.proc.stderr, _MAX_STDERR_LINE):
                if raw is None:
                    await self._q.put({"type": "error", "subtype": "oversized_stderr",
                                       "detail": f"dropped {dropped}-byte line"})
                    continue
                line = raw.decode(errors="replace").rstrip()
                if line:
                    await self._q.put({"type": "system", "subtype": "stderr",
                                       "data": {"line": line[:8192]}})
        except Exception as exc:  # noqa: BLE001 — a reader failure must not wedge events()
            await self._q.put({"type": "error", "subtype": "stderr_reader",
                               "detail": str(exc)[:1024]})
        finally:
            self._q.put_nowait(None)

    async def send(self, cmd: dict[str, Any]) -> None:
        if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.write((json.dumps(cmd) + "\n").encode())
            await self.proc.stdin.drain()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        done = 0
        while done < 2:
            item = await self._q.get()
            if item is None:
                done += 1
                continue
            yield item


# Sentinel for "the CA Warden just minted" — bytes in memory rather than a path.
_CA_PEM = object()


class DockerRunner(_PipeRunner):
    durable = True  # detached container survives an orchestrator restart → reattachable

    def __init__(self, *, session_id: str, config: Config, sess: SessionConfig) -> None:
        self.session_id = session_id
        self.config = config
        self.sess = sess
        self.container_name = f"terrarium-session-{session_id}"
        self.workspace_volume = f"terrarium-ws-{session_id}"
        self.network = session_network(config, session_id)  # isolated sandbox↔Warden bridge
        self._creds_dir: Path | None = None
        self._warden = None  # WardenController, set in start()
        self._warden_endpoint: tuple[str, int] | None = None
        self._warden_ca_pem: bytes | None = None
        # (source, container dest) copied in between `create` and `start`. Seeding a
        # created-but-unstarted container gives the sandbox its CA and decoy before the
        # worker runs, without either side needing a host path.
        self._seed: list[tuple[object, str]] = []

    async def update_warden_cred(self) -> None:
        """Refresh this Docker session's Warden credential on a rotation/re-paste —
        rewrites the mounted cred file (Warden hot-reloads by mtime)."""
        if self._warden is not None:
            await self._warden.update_cred(self._warden_cred())

    async def update_warden_policy(self, policy_json: str) -> None:
        """Push a re-resolved egress policy (profile/global edit) to this running
        Docker session's per-session Warden policy file."""
        if self._warden is not None and getattr(self._warden, "policy_json", None) is not None:
            await self._warden.update_policy(policy_json)

    async def update_warden_secrets(self, secrets_json: str) -> None:
        """Push edited operator secrets to this running Docker session's Warden file."""
        if self._warden is not None:
            await self._warden.update_secrets(secrets_json)

    # read_egress_audit is inherited: the Docker Warden appends straight to
    # egress_dir/audit/<sid>.jsonl, which is exactly where the base impl reads.

    def _warden_cred(self) -> dict | None:
        """The real credential Warden injects (never enters the sandbox). API key if
        set, else the subscription access token — served from RAM (decrypted), never
        read as plaintext off the PVC."""
        c = self.config
        if c.api_key:
            return {"type": "apikey", "value": c.api_key}
        try:
            tok = (managed_creds(c) or {})["claudeAiOauth"]["accessToken"]
            return {"type": "bearer", "value": tok}
        except Exception:
            return None

    def build_command(self) -> list[str]:
        c, s = self.config, self.sess
        # The whole harness travels as one JSON env var the worker deserializes.
        env = ["-e", f"TERRA_HARNESS={s.harness.to_json()}"]

        mounts = ["-v", f"{self.workspace_volume}:/workspace"]
        # memory_mode exists for k8s, where mounting the RWO PVC costs ~11s of Longhorn attach per
        # launch. A local docker volume attaches in ~0ms, so "synced" has nothing to buy here and
        # simply keeps the durable volume — same observable behaviour, no snapshot window. Only
        # "none" differs: no mount at all, so /memory is container-local scratch (matches k8s).
        if getattr(s.harness, "memory_mode", "volume") != "none":
            mounts += ["-v", f"{s.memory_volume}:/memory"]  # per-agent scope (default = agent id)
        if s.harness.skills and c.skills_dir.exists():
            # read-only — devs curate it; the agent can load but not modify skills
            # Operator-curated and read-only, so a snapshot at start equals a bind — and
            # stops this being a host path the daemon has to agree with.
            v = self._seed_vol("skills")
            self._seed.append((c.skills_dir, v))
            mounts += ["-v", f"{v}:/workspace/.claude/skills:ro"]
        if self._warden_endpoint:
            # Warden MITM: the real credential lives ONLY in Warden. The sandbox gets a
            # credential-shaped DUMMY (matching the real cred's TYPE) so the CLI emits
            # an INJECTABLE request of the right shape; Warden strips the dummy and
            # injects the real one. All egress goes through Warden (HTTPS_PROXY); the
            # firewall default-drops to the Warden pinhole; the worker trusts only the
            # per-session CA.
            wip, wport = self._warden_endpoint
            proxy = f"http://{wip}:{wport}"
            cred = self._warden_cred()
            if cred and cred.get("type") == "apikey":
                # api-key mode: a realistic DECOY key → CLI emits x-api-key (Warden
                # strips it and injects the real one). The agent can't tell it's fake.
                env += ["-e", f"ANTHROPIC_API_KEY={decoy_api_key()}"]
            else:
                # subscription mode: a STUB ~/.claude so the CLI emits the OAuth-shaped
                # request (Authorization: Bearer + anthropic-beta oauth) that Warden
                # injects the real subscription token into. Real token never here; the
                # decoy tokens look real so the agent can't fingerprint the stub.
                import json as _json
                stub = c.runtime_dir / "sessions" / self.session_id / "claude-stub"
                stub.mkdir(parents=True, exist_ok=True)
                os.chmod(stub.parent, 0o700)
                (stub / ".credentials.json").write_text(_json.dumps(decoy_oauth_stub()))
                os.chmod(stub, 0o777)
                os.chmod(stub / ".credentials.json", 0o666)
                v = self._seed_vol("stub")
                self._seed.append((stub, v))
                mounts += ["-v", f"{v}:/home/agent/.claude"]
            env += [
                "-e", f"HTTP_PROXY={proxy}", "-e", f"HTTPS_PROXY={proxy}",
                "-e", f"http_proxy={proxy}", "-e", f"https_proxy={proxy}",
                "-e", "NODE_USE_ENV_PROXY=1",
                "-e", f"NODE_EXTRA_CA_CERTS={AGENT_CA_FILE}",
                "-e", f"SSL_CERT_FILE={AGENT_CA_FILE}",
                "-e", f"REQUESTS_CA_BUNDLE={AGENT_CA_FILE}",
                "-e", f"TERRA_EGRESS_ALLOW_IP={wip}", "-e", f"TERRA_EGRESS_ALLOW_PORT={wport}",
                "-e", "TERRA_EGRESS_DEFAULT_DROP=1",
            ]
            if self._warden_ca_pem:
                v = self._seed_vol("ca")
                self._seed.append((_CA_PEM, v))
                mounts += ["-v", f"{v}:{AGENT_CA_DIR}:ro"]
        else:
            # The sidecar is started before build_command(), so the endpoint is always
            # set by the time we get here. Reaching this branch means bring-up was
            # skipped — fail loudly rather than silently launch a worker with the real
            # credential inside the sandbox.
            raise RuntimeError("warden endpoint missing — refusing to start an unmediated sandbox")

        return [
            # detached + stdin held open: the container outlives an orchestrator
            # restart and can be reattached. NOT --rm — we remove it explicitly.
            "docker", "create", "-i",
            "--name", self.container_name,
            *hardened_flags(self.network, c.docker_runtime),
            *mounts, *env,
            c.image,
        ]

    def _seed_vol(self, key: str) -> str:
        return f"terrarium-seed-{self.session_id}-{key}"[:63]

    async def _seed_container(self) -> None:
        """Populate the sandbox's bootstrap volumes.

        Not `docker cp` into the container: the sandbox runs with a READ-ONLY rootfs, so a
        copy into /etc or /home fails with "container rootfs is marked read-only". A volume is
        a separate mount, so it stays writable while the rootfs does not — and it can still be
        attached `:ro`, which keeps the guarantee the CA bind mount used to provide.

        Each volume is filled through a throwaway container: `docker cp` into a created (never
        started) container writes through to its attached volume. Volume NAMES are resolved by
        the daemon against its own store, so unlike a bind mount none of this depends on the
        orchestrator's filesystem namespace matching the host's."""
        if not self._seed:
            return
        with tempfile.TemporaryDirectory(prefix="sbseed-") as td:
            for src, vol in self._seed:
                stage = Path(td) / vol
                stage.mkdir(parents=True, exist_ok=True)
                if src is _CA_PEM:
                    if not self._warden_ca_pem:
                        raise RuntimeError("warden CA missing — refusing to start the sandbox")
                    (stage / "session-ca.pem").write_bytes(self._warden_ca_pem)
                else:
                    sp = Path(str(src))
                    if not sp.exists():
                        continue  # an absent optional source (no skills dir) is not an error
                    shutil.copytree(sp, stage, dirs_exist_ok=True, symlinks=True)
                await self._fill_volume(vol, stage)

    async def _fill_volume(self, vol: str, stage: Path) -> None:
        await _run(["docker", "volume", "create", vol])
        helper = f"{vol}-seed"[:63]
        await _run(["docker", "rm", "-f", helper])
        rc, out = await _run(["docker", "create", "--name", helper, "-v", f"{vol}:/seed",
                              self.config.image, "true"])
        if rc != 0:
            raise RuntimeError(f"seed helper for {vol} failed: {out.strip()}")
        try:
            rc, out = await _run(["docker", "cp", f"{stage}/.", f"{helper}:/seed"])
            if rc != 0:
                raise RuntimeError(f"seeding {vol} failed: {out.strip()}")
        finally:
            await _run(["docker", "rm", "-f", helper])

    async def drain_audit(self) -> None:
        """The audit lives inside the Warden container now, so mirror it onto our own volume;
        `verify-egress` must keep working after the sandbox is gone."""
        if self._warden:
            await self._warden.drain_audit()

    async def _attach(self) -> None:
        # Stream the detached container's stdio. --sig-proxy=false so that killing
        # this attach on detach() never forwards a signal to the container.
        self.proc = await asyncio.create_subprocess_exec(
            "docker", "attach", "--sig-proxy=false", self.container_name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._init_pipes()

    async def start(self) -> None:
        await ensure_network(self.network)
        await ensure_volume(self.sess.memory_volume)
        # Bring up the per-session Warden sidecar FIRST so its CA + endpoint are ready
        # before the worker (which trusts the CA and proxies through it).
        from .warden import WardenController
        self._warden = WardenController(self.config, self.session_id, self._warden_cred(),
                                        policy_json=self.sess.egress_policy_json,
                                        secrets_json=self.sess.warden_secrets_json,
                                        network=self.network)
        self._warden_endpoint = await self._warden.start()
        self._warden_ca_pem = self._warden.ca_pem
        if not self._warden_endpoint:
            raise RuntimeError("warden sidecar failed to start")
        rc, out = await _run(self.build_command())
        if rc != 0:
            raise RuntimeError(f"docker create failed: {out.strip()}")
        await self._seed_container()
        rc, out = await _run(["docker", "start", self.container_name])
        if rc != 0:
            raise RuntimeError(f"docker start failed: {out.strip()}")
        await self._attach()

    async def probe_state(self) -> str:
        """'running' (reattach) · 'gone' (reap) · 'unknown' (transient — retry)."""
        rc, out = await _run(["docker", "inspect", "-f", "{{.State.Status}}", self.container_name])
        if rc != 0:
            return "gone"  # no such container
        status = out.strip()
        if status == "running":
            return "running"
        if status in ("exited", "dead", "removing"):
            return "gone"
        return "unknown"  # created/paused/restarting — keep, retry next boot

    async def reattach(self) -> None:
        # Rebuild the deterministic controller handle so post-restart credential,
        # secret, and policy updates still reach this session's Warden.
        from .warden import WardenController
        self._warden = WardenController(
            self.config, self.session_id, self._warden_cred(),
            policy_json=self.sess.egress_policy_json,
            secrets_json=self.sess.warden_secrets_json,
            network=self.network,
        )
        self._warden_ca_pem = self._warden.ca_pem
        await self._attach()

    async def detach(self) -> None:
        # Leave the container running; just sever our stdio stream so the next
        # orchestrator boot can reattach. The worker's stdin stays open (-i).
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass

    async def stop(self) -> None:
        try:
            await self.send(P.shutdown_cmd())
            if self.proc and self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        await _run(["docker", "rm", "-f", self.container_name])
        await _run(["docker", "volume", "rm", "-f", self.workspace_volume])
        # Seed volumes are per-session and would otherwise accumulate one set per run. Reap by
        # name rather than from self._seed, which is empty on a stop that follows a reattach.
        for key in ("ca", "stub", "skills"):
            await _run(["docker", "volume", "rm", "-f", self._seed_vol(key)])
        from .warden import WardenController
        await WardenController(self.config, self.session_id, None).stop()  # reap sidecar + cred
        # reap the per-session network now that both containers are gone (best-effort —
        # fails harmlessly if a container is still attached or it was never created)
        await _run(["docker", "network", "rm", self.network])
        # deterministic per-session creds dir (set on the original; recomputed here
        # so a reattached runner cleans up too)
        shutil.rmtree(self.config.runtime_dir / "sessions" / self.session_id, ignore_errors=True)

    async def copy_in_bytes(self, name: str, data: bytes) -> str:
        from . import filebridge
        return await filebridge.copy_in_bytes(self.container_name, data, name)

    async def copy_out_bytes(self, name: str) -> bytes:
        from . import filebridge
        return await filebridge.copy_out(self.container_name, name)


class LocalRunner(_PipeRunner):
    """UNSANDBOXED host process — dev only. Gated behind TERRA_ALLOW_UNSAFE=1."""

    def __init__(self, *, session_id: str, config: Config, sess: SessionConfig) -> None:
        self.session_id = session_id
        self.config = config
        self.sess = sess
        self.workspace = config.runtime_dir / "local" / session_id / "workspace"

    async def start(self) -> None:
        if os.environ.get("TERRA_ALLOW_UNSAFE") != "1":
            raise RuntimeError(
                "LocalRunner runs the agent with NO sandbox on the host. "
                "Set TERRA_ALLOW_UNSAFE=1 to use it for local dev only."
            )
        self.workspace.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT),
            "TERRA_HARNESS": self.sess.harness.to_json(),
            "TERRA_WORKSPACE": str(self.workspace),  # the SDK cwd (worker reads this)
        }
        if self.sess.harness.skills and self.config.skills_dir.exists():
            dst = self.workspace / ".claude" / "skills"
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(self.config.skills_dir, dst)
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, str(WORKER_PATH),
            cwd=str(self.workspace), env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._init_pipes()

    async def copy_in_bytes(self, name: str, data: bytes) -> str:
        from .filebridge import sanitize_name
        safe = sanitize_name(name)
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / safe).write_bytes(data)
        return safe

    async def copy_out_bytes(self, name: str) -> bytes:
        from .filebridge import MAX_DOWNLOAD_BYTES, _safe_name
        target = self.workspace / _safe_name(name)
        # Same symlink rule as the container runners: this workspace is written by an
        # unsandboxed dev-mode agent, so the link target is still attacker-chosen.
        if target.is_symlink() or not target.is_file():
            raise ValueError("refusing non-regular file")
        if target.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"file too large (max {MAX_DOWNLOAD_BYTES} bytes)")
        return target.read_bytes()

    async def stop(self) -> None:
        try:
            await self.send(P.shutdown_cmd())
            if self.proc and self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass


def make_runner(config: Config, session_id: str, sess: SessionConfig) -> Runner:
    if config.runner == "local":
        return LocalRunner(session_id=session_id, config=config, sess=sess)
    if config.runner == "k8s":
        from .k8s_runner import K8sRunner  # lazy: only needs the kubernetes client when used

        return K8sRunner(session_id=session_id, config=config, sess=sess)
    if config.runner == "docker":
        return DockerRunner(session_id=session_id, config=config, sess=sess)
    raise ValueError(f"unknown runner {config.runner!r}; expected docker, k8s, or local")
