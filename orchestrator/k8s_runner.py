"""Kubernetes-native runner: each session is a sandbox **Pod**.

Mirrors the Docker runner's hardening as a Pod spec (cap-drop + NET_ADMIN/SETUID/
SETGID for the firewall entrypoint, no-privilege-escalation, read-only rootfs,
no service-account token, public DNS so the firewall can block cluster DNS), and
drives the worker over the Pod's attach stdio using the same JSON-lines protocol.

The orchestrator must run in-cluster with RBAC for pods, pods/attach,
persistentvolumeclaims, and secrets in its namespace.

Pod-spec construction (`build_pod_manifest` / `build_pvc_manifest`) is pure and
unit-tested; the API calls require a live cluster.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import json as _json
import logging
import re
import secrets
import threading
import time
from typing import Any, AsyncIterator

from terracore import protocol as P

from .config import (AGENT_CA_DIR, AGENT_CA_FILE, Config, decoy_api_key, decoy_oauth_stub,
                     managed_creds)
from .receipts import persist_receipt_key
from .runners import _MAX_STDOUT_LINE, Runner, SessionConfig

CREDS_MOUNT = "/var/run/terrarium/creds"
WARDEN_UID = 1002  # distinct from the worker's agent uid (1001) for shared-netns firewall by-uid accept
# Where Warden appends its audit INSIDE the sidecar (an emptyDir the worker never mounts).
# K8sRunner.drain_audit mirrors it out to the orchestrator's volume.
AUDIT_IN_POD = "/audit/audit.jsonl"

_log = logging.getLogger("terrarium.k8s")


def dns_name(value: str) -> str:
    """Sanitize to a DNS-1123 label (lowercase alphanumeric + '-', <=63 chars)."""
    v = re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")
    return v[:63].strip("-") or "x"


def cleanup_orphans(config: Config, keep_sids: set[str] | None = None) -> None:
    """Reap leftover sandbox Pods **and per-session credential resources** from a
    previous orchestrator instance, except those for sessions in ``keep_sids`` —
    the ones a restarted orchestrator just reattached to (or is retrying) via
    ``rehydrate``.

    Single-instance orchestrator (Recreate), so anything not kept is a true
    orphan. Reaping credentials matters: only ``K8sRunner.stop()`` deleted them
    before, which the restart path never calls — so OAuth tokens would otherwise
    accumulate at rest in the namespace across every restart. This covers both
    the non-Warden ``terrarium-creds`` Secret and, in the default hardened mode,
    the ``warden-cred`` Secret (where the real token actually lives) plus the
    ``warden-policy`` ConfigMap.
    """
    keep_sids = keep_sids or set()
    keep_pods = {dns_name(f"terrarium-session-{sid}") for sid in keep_sids}
    keep_creds = {dns_name(f"terrarium-creds-{sid}") for sid in keep_sids}
    # Under Warden (the default), the REAL credential lives in the warden-cred
    # Secret, not terrarium-creds — reap those too, plus the policy ConfigMaps,
    # or live OAuth tokens accumulate at rest across every session and restart.
    keep_warden = {dns_name(f"warden-cred-{sid}") for sid in keep_sids}
    keep_warden |= {dns_name(f"warden-policy-{sid}") for sid in keep_sids}
    from kubernetes import client, config as kconfig

    try:
        kconfig.load_incluster_config()
    except Exception:
        try:
            kconfig.load_kube_config()
        except Exception:
            return
    ns = config.k8s_namespace
    api = client.CoreV1Api()
    try:
        for pod in api.list_namespaced_pod(ns, label_selector="app=terrarium-sandbox").items:
            if pod.metadata.name in keep_pods:
                continue
            try:
                api.delete_namespaced_pod(pod.metadata.name, ns, grace_period_seconds=5)
            except Exception:
                pass
    except Exception:
        pass
    try:
        for sec in api.list_namespaced_secret(ns, label_selector="app=terrarium-creds").items:
            if sec.metadata.name in keep_creds:
                continue
            try:
                api.delete_namespaced_secret(sec.metadata.name, ns)
            except Exception:
                pass
    except Exception:
        pass
    try:
        for sec in api.list_namespaced_secret(ns, label_selector="app=terrarium-warden").items:
            if sec.metadata.name in keep_warden:
                continue
            try:
                api.delete_namespaced_secret(sec.metadata.name, ns)
            except Exception as e:
                _log.warning("orphan reap: failed to delete warden secret %s: %s", sec.metadata.name, e)
    except Exception as e:
        # e.g. missing RBAC list verb — must NOT be silent: it means live OAuth
        # tokens are accumulating at rest unreaped.
        _log.error("orphan reap: cannot list warden secrets (tokens may leak at rest): %s", e)
    try:
        for cm in api.list_namespaced_config_map(ns, label_selector="app=terrarium-warden").items:
            if cm.metadata.name in keep_warden:
                continue
            try:
                api.delete_namespaced_config_map(cm.metadata.name, ns)
            except Exception as e:
                _log.warning("orphan reap: failed to delete warden configmap %s: %s", cm.metadata.name, e)
    except Exception as e:
        _log.error("orphan reap: cannot list warden configmaps: %s", e)
    # Isolated (per-session, concurrent-run) memory clones whose session is gone.
    # Belt-and-braces for K8sRunner.stop()'s delete — a crash between pod-reap and
    # PVC-reap, or a pre-label orchestrator, leaves 1Gi clones behind forever.
    # Only label-selected clones are touched; agents' durable base volumes carry
    # no terrarium-isolated label and are never listed here.
    keep_pvcs = {dns_name(sid) for sid in keep_sids}
    try:
        for pvc in api.list_namespaced_persistent_volume_claim(
                ns, label_selector="app=terrarium-memory,terrarium-isolated=true").items:
            if (pvc.metadata.labels or {}).get("terrarium-session") in keep_pvcs:
                continue
            try:
                api.delete_namespaced_persistent_volume_claim(pvc.metadata.name, ns)
            except Exception as e:
                _log.warning("orphan reap: failed to delete isolated memory pvc %s: %s", pvc.metadata.name, e)
    except Exception as e:
        _log.error("orphan reap: cannot list isolated memory pvcs (clones may leak): %s", e)


def delete_memory_pvc(config: Config, memory_volume: str) -> None:
    """Delete an agent's memory PVC (purge_memory in the k8s runner), plus any of its
    labeled per-session isolated clones still lingering (crashed concurrent runs)."""
    from kubernetes import client, config as kconfig

    try:
        kconfig.load_incluster_config()
    except Exception:
        try:
            kconfig.load_kube_config()
        except Exception:
            return
    api = client.CoreV1Api()
    ns = config.k8s_namespace
    base = dns_name(memory_volume)
    try:
        api.delete_namespaced_persistent_volume_claim(base, ns)
    except Exception:
        pass
    try:
        for pvc in api.list_namespaced_persistent_volume_claim(
                ns, label_selector=f"terrarium-isolated=true,terrarium-base={base}").items:
            try:
                api.delete_namespaced_persistent_volume_claim(pvc.metadata.name, ns)
            except Exception:
                pass
    except Exception:
        pass


def snapshot_dir(config: Config):
    """Where memory_mode="synced" keeps its tarballs (one per memory SCOPE)."""
    return config.runtime_dir / "memory_snapshots"


def delete_memory_snapshots(config: Config, memory_volume: str) -> int:
    """Purge the synced-mode snapshots for a memory scope, returning how many were removed.

    In memory_mode="synced" an agent's memory lives as a tarball on the orchestrator's
    volume, NOT in a PVC or docker volume. Purging only the PVC therefore left the real
    memory sitting on disk, outliving the agent it belonged to — a privacy problem, since
    "delete this agent and its memory" visibly did not.

    Covers the base scope plus any per-session isolated clones (`<base>-<session>`)."""
    d = snapshot_dir(config)
    base = dns_name(memory_volume)
    removed = 0
    for p in [*d.glob(f"{base}.tar.gz"), *d.glob(f"{base}-*.tar.gz")]:
        try:
            p.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — a stuck file must not fail the whole delete
            _log.warning("could not delete memory snapshot %s", p, exc_info=True)
    return removed


def build_pvc_manifest(name: str, size: str, storage_class: str | None,
                       labels: dict[str, str] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": size}},
    }
    if storage_class:
        spec["storageClassName"] = storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "labels": {"app": "terrarium-memory", **(labels or {})}},
        "spec": spec,
    }


# The worker blocks on this until the orchestrator finishes restoring a "synced" /memory, so the
# agent can never read a half-unpacked snapshot. Excluded from the snapshot it gates.
MEMORY_SENTINEL_NAME = ".terra-memory-restored"
MEMORY_SENTINEL = f"/memory/{MEMORY_SENTINEL_NAME}"

def build_pod_manifest(
    *,
    name: str,
    image: str,
    harness_json: str,
    memory_pvc: str,
    memory_mode: str = "volume",
    api_key: str | None = None,
    resources: dict[str, Any] | None = None,
    warden_port: int = 8888,
    warden_cred_secret: str | None = None,
    warden_policy_cm: str | None = None,
    warden_receipt_key: str | None = None,
    workspace_size: str = "2Gi",
    memory_emptydir_size: str = "256Mi",
    audit_size: str = "256Mi",
    ephemeral_storage_limit: str = "4Gi",
) -> dict[str, Any]:
    env = [{"name": "TERRA_HARNESS", "value": harness_json}]
    if memory_mode == "synced":
        # Tells the worker to wait for the snapshot we unpack once the pod is Running (see
        # worker._await_memory). Only this runner restores, so only this runner promises it.
        env.append({"name": "TERRA_MEMORY_RESTORE", "value": "1"})
    # NOTE: the real credential (api_key or subscription token) is NEVER placed in the
    # worker container — it goes only to the Warden sidecar's cred Secret. Warden is
    # mandatory; a manifest built without it gets no credential at all (fail-safe), not
    # the real key.

    volumes: list[dict[str, Any]] = [
        {"name": "workspace", "emptyDir": {"sizeLimit": workspace_size}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}},
        {"name": "home", "emptyDir": {"sizeLimit": "256Mi"}},
        # Mounting the per-agent RWO PVC is ~11s of Longhorn attach on every launch (measured:
        # 1.6s pod start without it vs 11.4s with) — about half of a ~23s session start. Only
        # "volume" pays it; "synced"/"none" get an emptyDir, which costs nothing.
        {"name": "memory", "persistentVolumeClaim": {"claimName": memory_pvc}}
        if memory_mode == "volume"
        else {"name": "memory", "emptyDir": {"sizeLimit": memory_emptydir_size}},
    ]
    mounts: list[dict[str, Any]] = [
        {"name": "workspace", "mountPath": "/workspace"},
        {"name": "tmp", "mountPath": "/tmp"},
        {"name": "home", "mountPath": "/home/agent"},
        {"name": "memory", "mountPath": "/memory"},
    ]
    # Warden sidecar: all containers in a Pod share the netns, so the worker reaches
    # Warden at 127.0.0.1 (loopback — the kill/admin port is unreachable off-Pod).
    init_containers: list[dict[str, Any]] = []
    proxy = f"http://127.0.0.1:{warden_port}"
    volumes.append({"name": "warden-ca", "emptyDir": {"sizeLimit": "16Mi"}})
    # the agent sees this as a generic corporate proxy CA path, not /warden-ca
    mounts.append({"name": "warden-ca", "mountPath": AGENT_CA_DIR, "readOnly": True})
    # Decoy must MATCH the real credential's SHAPE so the CLI emits a request
    # Warden can inject into. api-key real cred → api-key decoy (x-api-key); a
    # subscription (OAuth) real cred → an OAuth ~/.claude stub (Bearer + the
    # anthropic-beta oauth header). An api-key decoy in front of a subscription
    # token makes the CLI omit that beta header → the injected token is rejected.
    if api_key:
        env.append({"name": "ANTHROPIC_API_KEY", "value": decoy_api_key()})  # Warden injects the real one
    else:
        # The worker writes this to ~/.claude/.credentials.json at startup (the
        # value is scrubbed from /proc/environ by the conceal re-exec).
        env.append({"name": "TERRA_DECOY_OAUTH", "value": _json.dumps(decoy_oauth_stub())})
    env += [
        {"name": "HTTP_PROXY", "value": proxy}, {"name": "HTTPS_PROXY", "value": proxy},
        {"name": "NODE_USE_ENV_PROXY", "value": "1"},
        {"name": "NODE_EXTRA_CA_CERTS", "value": AGENT_CA_FILE},
        {"name": "SSL_CERT_FILE", "value": AGENT_CA_FILE},
        {"name": "REQUESTS_CA_BUNDLE", "value": AGENT_CA_FILE},
        {"name": "TERRA_EGRESS_ALLOW_IP", "value": "127.0.0.1"},
        {"name": "TERRA_EGRESS_ALLOW_PORT", "value": str(warden_port)},
        {"name": "TERRA_EGRESS_DEFAULT_DROP", "value": "1"},
        # shared netns: the firewall must let Warden's OWN egress (DNS + upstream)
        # through by uid while confining the worker to the loopback proxy
        {"name": "WARDEN_UID", "value": str(WARDEN_UID)},
    ]
    w_mounts = [{"name": "warden-ca", "mountPath": "/ca"}]
    w_env = [
        {"name": "WARDEN_LISTEN", "value": f"127.0.0.1:{warden_port}"},
        {"name": "WARDEN_CA_DIR", "value": "/ca"},
        {"name": "WARDEN_AUDIT", "value": AUDIT_IN_POD},
        # Per-session key enables the tamper-evident audit (HMAC hash chain). The
        # caller persists this same key (receipts.persist_receipt_key) so the chain
        # is verifiable after the session ends; lives only in the Warden container's
        # env (the agent runs as a different uid).
        {"name": "WARDEN_RECEIPT_KEY", "value": warden_receipt_key or secrets.token_hex(16)},
    ]
    # audit lives on the WARDEN container only (NOT the worker) — the agent must not
    # see its own egress decisions (would reveal the mediation). The orchestrator
    # mirrors it out to its own volume (K8sRunner.drain_audit) so the chain survives
    # the Pod; nothing ever reads it from the worker side.
    volumes.append({"name": "warden-audit", "emptyDir": {"sizeLimit": audit_size}})
    w_mounts.append({"name": "warden-audit", "mountPath": "/audit"})
    if warden_cred_secret:
        volumes.append({"name": "wcred", "secret": {"secretName": warden_cred_secret, "optional": True}})
        w_mounts.append({"name": "wcred", "mountPath": "/wcred", "readOnly": True})
        w_env.append({"name": "WARDEN_CRED", "value": "/wcred/cred.json"})
        # Operator injection secrets ride the same Secret (secrets.json key). Always
        # point Warden at it so a secret ADDED to a running session syncs in + reloads;
        # the file is simply absent (→ no rules) when there are none.
        w_env.append({"name": "WARDEN_SECRETS", "value": "/wcred/secrets.json"})
    if warden_policy_cm:
        volumes.append({"name": "wpolicy", "configMap": {"name": warden_policy_cm}})
        w_mounts.append({"name": "wpolicy", "mountPath": "/wpolicy", "readOnly": True})
        w_env.append({"name": "WARDEN_POLICY", "value": "/wpolicy/policy.json"})
    # native sidecar (initContainer + restartPolicy:Always) → starts and writes
    # the CA BEFORE the worker, and stays running for the session
    init_containers.append({
        "name": "warden",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "restartPolicy": "Always",
        "command": ["/opt/runtime/zstunnel"],
        "env": w_env,
        "volumeMounts": w_mounts,
        "securityContext": {
            # a DISTINCT uid from the worker (group = fsGroup) so the shared-netns
            # firewall can allow Warden's egress by uid; gid 1001 = fsGroup → writes
            # the shared CA emptyDir
            "runAsNonRoot": True, "runAsUser": WARDEN_UID, "runAsGroup": 1001,
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},  # NO NET_ADMIN/CHOWN — Warden needs none
        },
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi", "ephemeral-storage": "32Mi"},
            "limits": {
                "cpu": "500m", "memory": "256Mi",
                "ephemeral-storage": ephemeral_storage_limit,
            },
        },
    })

    container = {
        "name": "worker",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "stdin": True,
        "stdinOnce": False,
        "tty": False,
        "env": env,
        "volumeMounts": mounts,
        "securityContext": {
            "runAsUser": 0,            # entrypoint needs root to set the firewall
            "runAsNonRoot": False,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            # NET_ADMIN: firewall · SETUID/SETGID: gosu drop · CHOWN: own the
            # copied creds. The agent process (post-gosu) holds zero effective caps.
            "capabilities": {"drop": ["ALL"], "add": ["NET_ADMIN", "SETUID", "SETGID", "CHOWN"]},
        },
        "resources": resources
        or {
            "requests": {"cpu": "250m", "memory": "512Mi", "ephemeral-storage": "256Mi"},
            "limits": {
                "cpu": "2", "memory": "2Gi",
                "ephemeral-storage": ephemeral_storage_limit,
            },
        },
    }

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {"app": "terrarium-sandbox", "terrarium/session": name}},
        "spec": {
            "restartPolicy": "Never",
            # emptyDir/PVC volumes are root-owned in k8s (unlike docker volumes);
            # fsGroup makes them writable by the agent's group (gid 1001).
            # seccompProfile: block the dangerous syscall surface (defense-in-depth
            # on top of non-root + dropped caps); RuntimeDefault still permits the
            # netlink/setuid syscalls the firewall + gosu need.
            "securityContext": {"fsGroup": 1001, "seccompProfile": {"type": "RuntimeDefault"}},
            # the sandbox must NOT get a k8s API token or cluster service env vars
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            # public DNS — the firewall blocks cluster DNS (RFC1918), so don't depend on it
            "dnsPolicy": "None",
            "dnsConfig": {"nameservers": ["1.1.1.1", "1.0.0.1"]},
            "terminationGracePeriodSeconds": 5,
            **({"initContainers": init_containers} if init_containers else {}),
            "containers": [container],
            "volumes": volumes,
        },
    }


class K8sRunner(Runner):
    durable = True  # the sandbox Pod survives an orchestrator restart → reattachable

    # Did this session's /memory restore complete? False means the pod's /memory is NOT this
    # agent's memory, so snapshotting it back out would destroy the real one. True by default
    # so "volume"/"none" modes (which never restore) snapshot normally.
    _memory_restored = True

    def __init__(self, *, session_id: str, config: Config, sess: SessionConfig) -> None:
        self.session_id = session_id
        self.config = config
        self.sess = sess
        self.namespace = config.k8s_namespace
        self.pod_name = dns_name(f"terrarium-session-{session_id}")
        self.memory_pvc = dns_name(sess.memory_volume)
        self.memory_mode = getattr(sess.harness, "memory_mode", "volume") or "volume"
        # The real credential NEVER lives in the sandbox: it goes solely to the
        # per-session warden-cred Secret (mounted into the Warden sidecar, not the
        # worker). There is no worker-side creds Secret in any mode.
        self.warden_cred_secret = dns_name(f"warden-cred-{session_id}")
        self.warden_policy_cm = dns_name(f"warden-policy-{session_id}")
        # Persist the receipt key so the tamper-evident chain is verifiable later.
        self.warden_receipt_key = persist_receipt_key(config, session_id)
        self._core = None
        self._ws = None
        self._q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: threading.Thread | None = None
        # Bytes of the in-Pod audit already mirrored to egress_dir/audit/<sid>.jsonl.
        # Seeded from what's on disk so a REATTACHED session (orchestrator restarted
        # mid-session) resumes where the drain left off instead of duplicating the
        # whole chain — the byte offsets line up because Warden only ever appends.
        self._audit_offset = self._persisted_audit_size()

    def _persisted_audit_size(self) -> int:
        from .egress import session_audit_path

        try:
            return session_audit_path(self.config, self.session_id).stat().st_size
        except OSError:
            return 0

    # ---- k8s plumbing (sync, run via to_thread) ----
    def _load(self) -> None:
        from kubernetes import client, config as kconfig

        try:
            kconfig.load_incluster_config()
        except Exception:
            kconfig.load_kube_config()
        self._core = client.CoreV1Api()

    def _ensure_pvc(self) -> None:
        from kubernetes import client

        # A per-session ISOLATED memory clone (minted when the agent's RWO base volume
        # is busy with a concurrent run) is scratch: nothing merges back, and no other
        # session may bind it. Label it so stop()/cleanup_orphans/purge can reap it; an
        # unlabeled clone leaks one 1Gi PVC per concurrent run, forever.
        labels = None
        if self.sess.memory_isolated:
            labels = {"terrarium-isolated": "true",
                      "terrarium-session": dns_name(self.session_id),
                      "terrarium-base": dns_name(self.sess.memory_volume.rsplit("-", 1)[0])}
        try:
            self._core.read_namespaced_persistent_volume_claim(self.memory_pvc, self.namespace)
        except client.ApiException as exc:
            if exc.status == 404:
                self._core.create_namespaced_persistent_volume_claim(
                    self.namespace,
                    build_pvc_manifest(self.memory_pvc, self.config.k8s_memory_size,
                                       self.config.k8s_storage_class, labels=labels),
                )
            else:
                raise

    def _create_pod(self) -> None:
        manifest = build_pod_manifest(
            name=self.pod_name,
            image=self.config.image,
            harness_json=self.sess.harness.to_json(),
            memory_pvc=self.memory_pvc,
            memory_mode=self.memory_mode,
            api_key=self.config.api_key,
            warden_port=self.config.warden_port,
            warden_cred_secret=self.warden_cred_secret,
            warden_policy_cm=self.warden_policy_cm,
            warden_receipt_key=self.warden_receipt_key,
            workspace_size=self.config.k8s_workspace_size,
            memory_emptydir_size=self.config.k8s_memory_emptydir_size,
            audit_size=self.config.k8s_audit_size,
            ephemeral_storage_limit=self.config.k8s_ephemeral_storage_limit,
        )
        self._core.create_namespaced_pod(self.namespace, manifest)

    def _ensure_warden(self) -> None:
        """Per-session Warden cred Secret (the real credential) + policy ConfigMap.
        The worker only ever holds the dummy; Warden injects from this Secret."""
        import base64
        import json as _json

        from kubernetes import client

        cred = None
        if self.config.api_key:
            cred = {"type": "apikey", "value": self.config.api_key}
        else:
            try:
                tok = (managed_creds(self.config) or {})["claudeAiOauth"]["accessToken"]
                cred = {"type": "bearer", "value": tok}
            except Exception:
                cred = None
        sec_data: dict[str, str] = {
            "cred.json": base64.b64encode(
                _json.dumps(cred or {"disabled": True}).encode()).decode(),
            "secrets.json": base64.b64encode(
                (self.sess.warden_secrets_json or _json.dumps({"secrets": []})).encode()).decode(),
        }
        # H2: always create the Secret, even when empty. A subscription session created before
        # any credential exists has no cred yet, but the console can POST /v1/credentials later —
        # which patches THIS Secret. If it were never created, that patch 404s (and the failure
        # is swallowed), leaving the session's Warden credential-less forever.
        sec = {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": self.warden_cred_secret, "labels": {"app": "terrarium-warden"}},
            "data": sec_data,
        }
        try:
            self._core.create_namespaced_secret(self.namespace, sec)
        except client.ApiException as exc:
            if exc.status != 409:
                raise
        # Policy: the per-session RESOLVED policy (the agent's egress profile if it pins
        # one, else the global console-managed policy) — set by the manager at create.
        # Falls back to the global file, then the static gateway_allow seed.
        policy = self.sess.egress_policy_json
        if not policy:
            try:
                policy = (self.config.egress_dir / "policy.json").read_text()
                _json.loads(policy)  # ensure it parses before shipping it to Warden
            except Exception:
                policy = _json.dumps({"mode": "enforce", "allow": list(self.config.gateway_allow)})
        cm = {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": self.warden_policy_cm, "labels": {"app": "terrarium-warden"}},
            "data": {"policy.json": policy},
        }
        try:
            self._core.create_namespaced_config_map(self.namespace, cm)
        except client.ApiException as exc:
            if exc.status != 409:
                raise

    @staticmethod
    def _worker_started(pod) -> bool:
        """True once the ``worker`` container itself is running.

        Pod phase is NOT a proxy for this. Warden is a native sidecar (an initContainer with
        ``restartPolicy: Always``), so the pod reports ``Running`` the moment the SIDECAR
        starts — while ``worker`` can still be waiting to be created. Both ``exec`` and
        ``attach`` target the worker container and fail against one that hasn't started, so
        returning on phase alone opens a race: in ``synced`` memory mode the restore would
        exec too early, never write the sentinel, and leave the worker blocked until its
        20s gate expired ("memory restore timed out — continuing with empty /memory")."""
        for cs in (pod.status.container_statuses or ()):
            if cs.name == "worker":
                return bool(cs.state and cs.state.running)
        return False

    def _wait_running(self, timeout: float = 150) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pod = self._core.read_namespaced_pod(self.pod_name, self.namespace)
            phase = pod.status.phase
            if phase in ("Failed", "Succeeded"):
                raise RuntimeError(f"sandbox pod entered {phase}")
            if phase == "Running" and self._worker_started(pod):
                return
            time.sleep(0.5)
        raise TimeoutError("sandbox pod did not reach Running")

    def _attach(self) -> None:
        from kubernetes.stream import stream

        self._ws = stream(
            self._core.connect_get_namespaced_pod_attach,
            self.pod_name,
            self.namespace,
            container="worker",
            stderr=True,
            stdin=True,
            stdout=True,
            tty=False,
            _preload_content=False,
        )

    async def copy_in_bytes(self, name: str, data: bytes) -> str:
        import io
        import tarfile

        from .filebridge import sanitize_name
        safe = sanitize_name(name)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=safe)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        await asyncio.to_thread(self._tar_into_pod, buf.getvalue())  # blocking WS exec
        return safe

    async def copy_out_bytes(self, name: str) -> bytes:
        return await asyncio.to_thread(self._tar_out_of_pod, name)

    def _tar_out_of_pod(self, name: str) -> bytes:
        """Read one workspace file out of the Pod, base64 over the exec channel.

        Same transport as the /memory snapshot: the k8s WS exec decodes to text, so raw
        bytes would be mangled. base64 costs 33%, which the size cap already accounts for.

        The guards are the interesting part, because the sandbox is untrusted and BOTH the
        name and what it points at are agent-chosen:
          - `_safe_name` rejects traversal and anything but [A-Za-z0-9._-]
          - `test -f` is preceded by `test -L`, so a symlink to /etc/passwd (or to the
            Warden CA) is refused rather than followed
          - the size is checked in-Pod, before the bytes are read, so an oversized file
            costs one stat instead of streaming 4 GB into the control plane's RAM
        """
        from .filebridge import MAX_DOWNLOAD_BYTES, _safe_name

        safe = _safe_name(name)
        path = f"/workspace/{safe}"
        script = (
            f'set -e; test ! -L "{path}" || {{ echo TERRA_SYMLINK >&2; exit 1; }}; '
            f'test -f "{path}" || {{ echo TERRA_NOTFOUND >&2; exit 1; }}; '
            f'sz=$(wc -c < "{path}"); '
            f'[ "$sz" -le {MAX_DOWNLOAD_BYTES} ] || {{ echo TERRA_TOOBIG >&2; exit 1; }}; '
            f'base64 -w0 < "{path}"'
        )
        out = self._exec_capture(["sh", "-c", script], container="worker")
        blob = (out or "").strip()
        if not blob:
            # The exec channel folds stderr away, so distinguish the cases by what the
            # file system says now rather than returning an empty file as a success.
            raise ValueError(f"could not read {safe!r} from the workspace "
                             f"(missing, a symlink, or larger than {MAX_DOWNLOAD_BYTES} bytes)")
        return base64.b64decode(blob)

    def _tar_into_pod(self, tarbytes: bytes, dest: str = "/workspace", gzip: bool = False) -> None:
        # kubectl-cp mechanism: stream a tar to `tar x` in the worker. `dest` is parameterised so
        # the same channel restores a gzipped /memory snapshot, not just file uploads.
        from kubernetes.stream import stream

        resp = stream(
            self._core.connect_get_namespaced_pod_exec, self.pod_name, self.namespace,
            container="worker", command=["tar", "xzmf" if gzip else "xmf", "-", "-C", dest],
            stdin=True, stdout=True, stderr=True, _preload_content=False,
        )
        chunks = [tarbytes[i:i + 256 * 1024] for i in range(0, len(tarbytes), 256 * 1024)] or [b""]
        errbuf = ""
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stderr():
                errbuf += resp.read_stderr()
            if chunks:
                resp.write_stdin(chunks.pop(0))
            else:
                break  # stdin drained → close() signals EOF so tar extracts
        resp.close()
        if errbuf.strip():
            raise RuntimeError(f"tar into pod failed: {errbuf.strip()[:200]}")

    async def update_warden_policy(self, policy_json: str) -> None:
        if not self.warden_policy_cm:
            return  # partially constructed/tearing-down session
        await asyncio.to_thread(self._patch_policy_cm, policy_json)

    def _patch_policy_cm(self, policy_json: str) -> None:
        # patch the per-session policy ConfigMap; k8s syncs it into the mounted file
        # and Warden hot-reloads (mtime), so a console policy change reaches RUNNING sessions.
        from kubernetes import client
        try:
            self._core.patch_namespaced_config_map(
                self.warden_policy_cm, self.namespace, {"data": {"policy.json": policy_json}},
            )
        except client.ApiException:
            pass  # CM already gone (session ending) — nothing to update

    async def update_warden_cred(self) -> None:
        """Push the CURRENT credential to this running session's Warden cred Secret;
        k8s syncs it into the mounted file and Warden hot-reloads (mtime), so a refresh
        or console re-paste reaches a running session without a restart."""
        if not self.warden_cred_secret:
            return
        if self.config.api_key:
            cred = {"type": "apikey", "value": self.config.api_key}
        else:
            try:
                tok = (managed_creds(self.config) or {})["claudeAiOauth"]["accessToken"]
            except Exception:  # noqa: BLE001
                cred = {"disabled": True}
            else:
                cred = {"type": "bearer", "value": tok}
        await asyncio.to_thread(self._patch_cred_secret, cred)

    def _patch_cred_secret(self, cred: dict[str, Any]) -> None:
        from kubernetes import client
        body = {"data": {"cred.json": base64.b64encode(_json.dumps(cred).encode()).decode()}}
        try:
            self._core.patch_namespaced_secret(self.warden_cred_secret, self.namespace, body)
        except client.ApiException as exc:
            if exc.status == 404:
                return  # secret gone (session ending) — benign
            # anything else means this session's Warden keeps a stale/absent credential and
            # will 401 — surface it rather than swallow (H2).
            _log.warning("cred push to warden secret %s failed (session will 401): %s",
                         self.warden_cred_secret, exc)

    async def update_warden_secrets(self, secrets_json: str) -> None:
        """Push edited operator secrets to this running session's warden Secret (secrets.json
        key); k8s syncs it into the mount and Warden hot-reloads by mtime."""
        if not self.warden_cred_secret:
            return
        await asyncio.to_thread(self._patch_secrets_secret, secrets_json)

    def _patch_secrets_secret(self, secrets_json: str) -> None:
        from kubernetes import client
        body = {"data": {"secrets.json": base64.b64encode(secrets_json.encode()).decode()}}
        try:
            self._core.patch_namespaced_secret(self.warden_cred_secret, self.namespace, body)
        except client.ApiException as exc:
            if exc.status == 404:
                return  # secret gone (session ending) — benign
            _log.warning("operator-secret push to warden secret %s failed: %s",
                         self.warden_cred_secret, exc)

    # --- audit drain: mirror the in-Pod Warden audit onto the orchestrator's volume ---
    #
    # The audit is an emptyDir in the WARDEN container (never the worker — the agent must
    # not see its own egress decisions), so it died with the Pod: `verify-egress` had to
    # 409 with "audit not retained", and every console poll paid one pod-exec PER SESSION.
    # Draining it to egress_dir/audit/<sid>.jsonl fixes both — the chain outlives the
    # sandbox, and reads become local file reads for every runner.
    #
    # Incremental and byte-exact: we track the byte offset already copied and append only
    # the delta, base64'd over the exec channel (the k8s WS exec decodes to text, so raw
    # bytes would be mangled — the same reason _snapshot_memory encodes). Byte offsets
    # keep the HMAC chain verifiable: a partial trailing line is completed by the next
    # drain rather than duplicated or dropped.
    #
    # read_egress_audit is INHERITED (reads the drained file) — deliberately not an exec,
    # so a 6s console poll costs nothing on the cluster.

    async def drain_audit(self) -> None:
        if self._core is None:
            return
        try:
            await asyncio.to_thread(self._drain_audit)
        except Exception:  # noqa: BLE001 — pod gone / warden not up yet / RBAC
            # Debug, not warning: this fires benignly on every drain attempt against a
            # Pod that is still starting or already reaped, and the periodic sweep retries.
            _log.debug("audit drain failed for session %s", self.session_id, exc_info=True)

    def _drain_audit(self) -> None:
        from .egress import session_audit_path

        size = int((self._exec_capture(["stat", "-c", "%s", AUDIT_IN_POD]) or "0").strip() or 0)
        if size <= self._audit_offset:
            if size < self._audit_offset:
                # Warden never rotates this signed stream. Shrinkage therefore means
                # corruption/replacement; do not silently resync and append an
                # unanchored suffix to the durable evidence.
                _log.error("session %s audit shrank (%d < %d) — refusing to drain an incomplete chain",
                           self.session_id, size, self._audit_offset)
            return
        b64 = self._exec_capture(
            ["sh", "-c", f"tail -c +{self._audit_offset + 1} {AUDIT_IN_POD} | base64 -w0"])
        delta = base64.b64decode((b64 or "").strip() or "")
        if not delta:
            return
        path = session_audit_path(self.config, self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as f:
            f.write(delta)
        self._audit_offset += len(delta)

    # --- "synced" memory: snapshot in/out around a pod that mounts no PVC -------------------
    #
    # The snapshot lives on the ORCHESTRATOR's runtime volume (itself replicated), not on the
    # sandbox's critical path — that's the whole point: the pod starts in ~1.6s instead of ~11.4s,
    # and durability moves from "RWO volume attached per launch" to "tarball written at each turn
    # end and on stop". The trade-off is a window: writes since the last snapshot are lost if the
    # pod dies abruptly. "volume" mode remains for agents that can't accept that.

    async def snapshot_memory(self) -> None:
        """Turn-end persistence for "synced" mode — this is what bounds the loss window to a
        single turn instead of a whole session."""
        if self.memory_mode != "synced":
            return
        if self.sess.memory_isolated:
            # A concurrent run gets a throwaway per-session scope. Its snapshot is keyed by a
            # name no later session will ever ask for, so writing one produces a file that can
            # only accumulate — never be restored. Isolation means "this run is deliberately
            # not the agent's memory", so there is nothing to persist.
            return
        try:
            await asyncio.to_thread(self._snapshot_memory)
        except Exception:  # noqa: BLE001 — never let a snapshot failure break the turn
            _log.warning("memory snapshot failed for session %s", self.session_id, exc_info=True)

    def _snapshot_path(self):
        d = snapshot_dir(self.config)
        d.mkdir(parents=True, exist_ok=True)
        # Keyed by memory SCOPE (the agent's volume name), so sequential sessions of the same
        # agent — and agents deliberately sharing a scope — see the same memory, exactly as the
        # shared PVC behaves today.
        return d / f"{dns_name(self.sess.memory_volume)}.tar.gz"

    def _restore_memory(self) -> None:
        """Unpack the agent's snapshot into /memory, then drop the sentinel the worker waits on."""
        snap = self._snapshot_path()
        try:
            if snap.exists() and snap.stat().st_size > self.config.memory_snapshot_max_bytes:
                raise ValueError(
                    f"snapshot exceeds {self.config.memory_snapshot_max_bytes} byte limit"
                )
            if snap.exists() and snap.stat().st_size > 0:
                self._tar_into_pod(snap.read_bytes(), dest="/memory", gzip=True)
        except Exception:  # noqa: BLE001 — a broken snapshot must not strand the session
            self._memory_restored = False
            _log.warning("memory restore failed for session %s; starting with empty /memory",
                        self.session_id, exc_info=True)
        finally:
            # Always release the worker, even if the restore failed — an agent with empty memory
            # is far better than one wedged forever behind a sentinel that never appears.
            try:
                self._exec_capture(["touch", MEMORY_SENTINEL], container="worker")
            except Exception:  # noqa: BLE001
                self._memory_restored = False
                _log.warning("could not write the memory sentinel for %s", self.session_id, exc_info=True)

    @staticmethod
    def _tar_has_content(blob: bytes) -> bool:
        """Does this .tar.gz hold anything beyond bare directory entries?

        An empty /memory still produces a well-formed archive (just ``./``), so byte-length
        alone cannot distinguish "no memory" from "memory". Unreadable archives count as
        content: a corrupt blob should never be treated as an empty one and used to justify
        overwriting a good snapshot."""
        import tarfile
        try:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
                return any(m.name not in (".", "./") and not m.isdir() for m in tf)
        except Exception:  # noqa: BLE001 — unreadable → assume content, i.e. do not overwrite
            return True

    def _snapshot_memory(self) -> None:
        """Tar /memory out of the pod and write it to the orchestrator's runtime volume.

        base64 over the exec channel: the k8s WS exec decodes to text, so raw tar bytes would be
        mangled. /memory is agent notes (KB), so the encoding overhead is irrelevant."""
        limit = self.config.memory_snapshot_max_bytes
        b64 = self._exec_capture(
            ["sh", "-c",
             f"tar czf - -C /memory --exclude ./{MEMORY_SENTINEL_NAME} . 2>/dev/null "
             f"| head -c {limit + 1} | base64 -w0"],
            container="worker",
        )
        blob = base64.b64decode((b64 or "").strip() or "")
        if len(blob) > limit:
            raise ValueError(f"memory snapshot exceeds {limit} byte limit")
        if not blob:
            return  # nothing to persist (empty /memory) — don't clobber a good snapshot with 0 bytes
        snap = self._snapshot_path()
        # Never let an EMPTY /memory overwrite a snapshot that has content. `not blob` above
        # does not catch this: an empty directory still tars to a valid ~100-byte archive, so
        # it sails past that check. Chained with a failed restore (the pod's /memory is then
        # empty through no fault of the agent), the turn-end snapshot would quietly destroy
        # everything the agent had remembered.
        # The trade-off is deliberate: an agent that genuinely clears /memory will not have
        # that clearing persisted, and the next session restores the old notes. Recoverable
        # and visible; silent data loss is neither.
        if not self._tar_has_content(blob):
            if snap.exists() and snap.stat().st_size > 0 and self._tar_has_content(snap.read_bytes()):
                _log.warning("refusing to overwrite a non-empty memory snapshot for %s with an "
                             "empty one (restore_ok=%s)", self.session_id, self._memory_restored)
                return
        elif not self._memory_restored:
            # Restore never completed, so what is in the pod is not this agent's memory.
            _log.warning("skipping memory snapshot for %s: the restore did not complete, so "
                         "/memory in the pod is not authoritative", self.session_id)
            return
        tmp = snap.with_suffix(".tmp")
        tmp.write_bytes(blob)
        tmp.replace(snap)  # atomic: a crash mid-write can never leave a truncated snapshot

    def _exec_capture(self, command: list[str], container: str = "warden") -> str:
        """Run a command in `container` and return its stdout.

        `container` MUST be honoured, and getting it wrong fails SILENTLY: warden has no
        /memory mount, but the image still ships an empty /memory directory, so a misrouted
        /memory command succeeds against the wrong filesystem instead of erroring. The
        symptoms are remote from the cause — a restore sentinel the worker never sees, and
        snapshots that are valid but empty."""
        from kubernetes.stream import stream
        return stream(
            self._core.connect_get_namespaced_pod_exec, self.pod_name, self.namespace,
            container=container, command=command, stdin=False, stdout=True, stderr=False,
            _preload_content=True,
        ) or ""

    def _put(self, item: dict[str, Any]) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._q.put_nowait, item)

    def _emit_lines(self, buf: str, stdout: bool) -> str:
        # The worker is untrusted: cap line + leftover size so one giant no-newline
        # line can't accumulate unbounded in RAM and OOM the single control plane.
        # (Mirrors DockerRunner._read_stdout's _MAX_STDOUT_LINE guard.)
        *lines, rest = buf.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if len(line) > _MAX_STDOUT_LINE:
                self._put({"type": "error", "subtype": "oversized_stdout",
                           "detail": f"dropped {len(line)}-byte line"})
                continue
            if stdout:
                try:
                    self._put(json.loads(line))
                except Exception:
                    self._put({"type": "system", "subtype": "stdout", "data": {"line": line}})
            else:
                self._put({"type": "system", "subtype": "stderr", "data": {"line": line}})
        if len(rest) > _MAX_STDOUT_LINE:  # runaway line, still no newline — drop it
            self._put({"type": "error", "subtype": "oversized_stdout",
                       "detail": f"dropped {len(rest)} bytes (no newline)"})
            return ""
        return rest

    def _read_loop(self) -> None:
        ws = self._ws
        out, err = "", ""
        try:
            while ws.is_open():
                ws.update(timeout=1)
                if ws.peek_stdout():
                    out = self._emit_lines(out + ws.read_stdout(), stdout=True)
                if ws.peek_stderr():
                    err = self._emit_lines(err + ws.read_stderr(), stdout=False)
        finally:
            # EOF on ANY exit (incl. an exception mid-loop) so the pump never
            # blocks forever and detach()/stop() don't stall on the 10s timeout.
            self._put({"__eof__": True})

    def _pod_phase(self) -> str | None:
        """Pod phase, or None if the Pod is definitively gone (404). Re-raises on
        a transient API error so the caller can avoid reaping a still-live session."""
        from kubernetes import client

        try:
            return self._core.read_namespaced_pod(self.pod_name, self.namespace).status.phase
        except client.ApiException as exc:
            if exc.status == 404:
                return None
            raise

    # ---- Runner interface ----
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._load)
        # A PVC we never mount is pure latency + a stray 1Gi claim, so only "volume" provisions it.
        if self.memory_mode == "volume":
            await asyncio.to_thread(self._ensure_pvc)
        await asyncio.to_thread(self._ensure_warden)
        await asyncio.to_thread(self._create_pod)
        await asyncio.to_thread(self._wait_running)
        # Restore BEFORE attaching/reading: the worker blocks on the sentinel this writes, so the
        # agent can never observe a half-restored /memory (see worker.py _await_memory).
        if self.memory_mode == "synced":
            await asyncio.to_thread(self._restore_memory)
        await asyncio.to_thread(self._attach)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    async def probe_state(self) -> str:
        """Classify the sandbox Pod for rehydrate (used on orchestrator boot):
        ``"running"`` → reattach · ``"gone"`` (404/Failed/Succeeded) → reap ·
        ``"unknown"`` (transient API error, or Pending) → keep + retry next boot,
        so a flaky API call never reaps a session whose Pod is actually alive."""
        self._loop = asyncio.get_running_loop()
        if self._core is None:
            await asyncio.to_thread(self._load)
        try:
            phase = await asyncio.to_thread(self._pod_phase)
        except Exception:
            return "unknown"
        if phase == "Running":
            return "running"
        if phase in ("Failed", "Succeeded") or phase is None:
            return "gone"
        return "unknown"

    async def reattach(self) -> None:
        """Reattach to an already-Running Pod (skip create/wait) — the restart
        recovery path. Assumes ``probe()`` already confirmed it is Running."""
        self._loop = asyncio.get_running_loop()
        if self._core is None:
            await asyncio.to_thread(self._load)
        await asyncio.to_thread(self._attach)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    async def detach(self) -> None:
        """Graceful-shutdown release: close the attach stream but LEAVE the Pod
        running so the next orchestrator boot can reattach to it."""
        # Drain first: the Pod survives us, but it may die (OOM/evict/node drain) while the
        # orchestrator is down, and the audit would go with it. Reattach re-seeds the offset
        # from the file, so this is safe to repeat.
        await self.drain_audit()
        if self._ws:
            try:
                await asyncio.to_thread(self._ws.close)
            except Exception:
                pass

    async def send(self, cmd: dict[str, Any]) -> None:
        if self._ws and self._ws.is_open():
            await asyncio.to_thread(self._ws.write_stdin, json.dumps(cmd) + "\n")

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._q.get()
            if item.get("__eof__"):
                return
            yield item

    async def stop(self) -> None:
        try:
            await self.send(P.shutdown_cmd())
        except Exception:
            pass
        # Last chance to persist /memory — the pod is about to be deleted.
        if self.memory_mode == "synced":
            try:
                await asyncio.to_thread(self._snapshot_memory)
            except Exception:  # noqa: BLE001 — a failed snapshot must not block teardown
                _log.warning("memory snapshot failed for session %s", self.session_id, exc_info=True)
        # Same deadline for the audit: after _delete_pod the chain is unrecoverable, and the
        # tail we'd lose is exactly the interesting part (whatever the agent did last).
        await self.drain_audit()
        if self._ws:
            try:
                await asyncio.to_thread(self._ws.close)
            except Exception:
                pass
        for fn, args in (
            (self._delete_pod, ()),
            (self._delete_warden_resources, ()),
            (self._delete_isolated_pvc, ()),
        ):
            try:
                await asyncio.to_thread(fn, *args)
            except Exception:
                pass

    def _delete_pod(self) -> None:
        from kubernetes import client

        try:
            self._core.delete_namespaced_pod(self.pod_name, self.namespace, grace_period_seconds=5)
        except client.ApiException:
            pass

    def _delete_isolated_pvc(self) -> None:
        """Reap this session's ISOLATED memory clone (concurrent-run scratch). Only ever
        fires for a clone — the agent's durable base volume is never isolated. The PVC
        delete is async server-side (waits for the Pod to release it), so ordering after
        _delete_pod is enough."""
        from kubernetes import client

        if not self.sess.memory_isolated:
            return
        try:
            self._core.delete_namespaced_persistent_volume_claim(self.memory_pvc, self.namespace)
        except client.ApiException:
            pass

    def _delete_warden_resources(self) -> None:
        """Delete this session's Warden Secret (which holds the REAL credential)
        and policy ConfigMap. These are where the live token lives; leaving them behind
        accumulates valid bearer tokens at rest in the namespace."""
        from kubernetes import client

        if getattr(self, "warden_cred_secret", None):
            try:
                self._core.delete_namespaced_secret(self.warden_cred_secret, self.namespace)
            except client.ApiException:
                pass
        if getattr(self, "warden_policy_cm", None):
            try:
                self._core.delete_namespaced_config_map(self.warden_policy_cm, self.namespace)
            except client.ApiException:
                pass
