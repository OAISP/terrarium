#!/usr/bin/env python3
"""Red-team the sandbox CONCEALMENT — from the agent's point of view.

A curious or prompt-injected agent runs under the SAME uid as the worker, so it
can read the worker's /proc/<pid>/environ and inspect its own TLS trust store.
This test runs INSIDE the real sandbox image and asserts the known fingerprints
are closed:

  • the worker's /proc/self/environ carries no TERRA_*/WARDEN_* tell after the
    conceal re-exec (F6) — yet the harness stays usable on the heap, and the
    intended "corporate proxy" var (HTTPS_PROXY) is preserved;
  • the trust store handed to OpenSSL/curl/git is a COMBINED bundle (real roots +
    the one proxy CA), not a lone self-signed cert (F8/F9);
  • a same-uid child ("the agent") cannot recover the tells from the worker;
  • the worker's /proc/<pid>/cmdline (readable by a same-uid agent walking its ancestors)
    carries no orchestration/project tell — a generic interpreter+script is fine, but the
    invocation must never be project-named (e.g. "terrarium-worker");
  • no filesystem fingerprints: no /opt/terrarium, no binary named "warden", and the
    firewall/entrypoint scripts are root-only 0700 (the agent can't read the egress design).

Concealment regressions are invisible to functional tests, so this guards them.

Run:  uv run python tests/redteam_conceal.py     (needs the built image)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator.runners import hardened_flags

IMAGE = "terrarium-sandbox"
NETWORK = "terrarium-net"

# Runs in-container AS THE WORKER (after conceal_env's re-exec).
PROBE = r'''
import os, sys, subprocess
import terracore.conceal as cz

cz.conceal_env()  # re-exec once → sanitized stack env (idempotent on the sealed pass)

fail = 0
def check(name, ok):
    global fail
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fail += 1

env = open(f"/proc/{os.getpid()}/environ", "rb").read()
tell = any(t in env for t in (b"TERRA_HARNESS", b"WARDEN_UID", b"TERRA_DECOY", b"KUBERNETES_"))
check("worker /proc/environ has NO TERRA_/WARDEN_/KUBERNETES_ tell", not tell)
check("harness still usable (re-injected on heap)", os.environ.get("TERRA_HARNESS") is not None)
check("corp-proxy frame kept (HTTPS_PROXY visible)", os.environ.get("HTTPS_PROXY") is not None)

# Combined trust store: simulate a session CA, assert >1 cert + curl/git wired.
fakeca = "/tmp/session-ca.pem"
open(fakeca, "w").write("-----BEGIN CERTIFICATE-----\nFAKESESSIONCA\n-----END CERTIFICATE-----\n")
os.environ["NODE_EXTRA_CA_CERTS"] = fakeca
for v in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
    os.environ.pop(v, None)
bundle = cz.prepare_ca_bundle(wait_s=2)
ncerts = open(bundle).read().count("BEGIN CERTIFICATE") if bundle else 0
check("trust store is a COMBINED bundle, not a single self-signed cert", bool(bundle) and ncerts > 1)
check("curl/git CA vars point at the combined bundle",
      os.environ.get("CURL_CA_BUNDLE") == bundle and os.environ.get("GIT_SSL_CAINFO") == bundle)

# Filesystem tells: the orchestration dir is neutral (/opt/runtime, not /opt/terrarium),
# the egress mediator binary is NOT named "warden", and the root-only scripts (firewall +
# entrypoint) are 0700 so the agent (uid 1001) can't read the egress design.
import stat as _stat
check("no project-named /opt/terrarium path", not os.path.exists("/opt/terrarium"))
check("egress mediator is not a file named 'warden'", not os.path.exists("/opt/runtime/warden"))
check("egress mediator present under a neutral name", os.path.exists("/opt/runtime/zstunnel"))
for f in ("/opt/runtime/firewall.sh", "/opt/runtime/entrypoint.sh"):
    mode = _stat.S_IMODE(os.stat(f).st_mode) if os.path.exists(f) else None
    check(f"{f} is root-only 0700 (agent can't read the egress design)", mode == 0o700)

ppid = os.getpid()
child = subprocess.run(
    [sys.executable, "-c",
     "d=open('/proc/%d/environ','rb').read(); print('AGENTLEAK', (b'TERRA_HARNESS' in d) or (b'WARDEN_UID' in d))" % ppid],
    capture_output=True, text=True)
check("a same-uid agent child cannot read tells from the worker environ", "AGENTLEAK False" in child.stdout)

# cmdline: a same-uid agent can read the worker's (and its ancestors') /proc/<pid>/cmdline. The
# generic interpreter+script is acceptable — the runtime source under /opt/runtime is necessarily
# same-uid-readable anyway — but the INVOCATION must carry NO orchestration/project tell (a plain
# env scrub wouldn't hide a project-named argv). Guards against e.g. launching "terrarium-worker".
cmdline = open(f"/proc/{os.getpid()}/cmdline", "rb").read().lower()
cmd_tells = [t.decode() for t in (b"terrarium", b"warden", b"orchestrat") if t in cmdline]
check(f"worker /proc/cmdline carries no project tell (found {cmd_tells})", not cmd_tells)

print("RESULT:", "PASS" if fail == 0 else "FAIL")
sys.exit(1 if fail else 0)
'''

# Write the probe to a file (conceal re-execs via argv → needs a real file, not -c),
# then run it.
CMD = "cat > /tmp/probe.py <<'PROBE_EOF'\n" + PROBE + "\nPROBE_EOF\nexec python3 /tmp/probe.py\n"


def main() -> int:
    have = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, text=True)
    if have.returncode != 0:
        print(f"skip redteam-conceal: image {IMAGE} not built (run `make build`)")
        return 0
    flags = hardened_flags(NETWORK)
    args = ["docker", "run", "--rm", *flags,
            "-e", 'TERRA_HARNESS={"model":"opus","system_mode":"assistant"}',
            "-e", "WARDEN_UID=1002",
            "-e", "KUBERNETES_SERVICE_HOST=10.0.0.1",
            "-e", "HTTPS_PROXY=http://127.0.0.1:8888",
            "--entrypoint", "sh", IMAGE, "-c", CMD]
    p = subprocess.run(args, capture_output=True, text=True)
    sys.stdout.write(p.stdout)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
    print("\nCONCEALMENT:", "PASS ✅" if p.returncode == 0 and "RESULT: PASS" in p.stdout else "FAIL ❌")
    return p.returncode


if __name__ == "__main__":
    raise SystemExit(main())
