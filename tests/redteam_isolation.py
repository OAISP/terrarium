#!/usr/bin/env python3
"""Red-team the sandbox boundary.

Launches the hardened image with the same flags the orchestrator uses and runs probes
*as the agent user, after the firewall is locked in*. No Warden pinhole is configured
here, which is the point: this asserts what the sandbox looks like with nothing to
mediate through.

  • egress is FAIL-CLOSED — with no pinhole, not even the public internet is reachable
  • cloud metadata / RFC1918 / the host gateway are cleanly DROPPED (timeout)
  • the agent is non-root, holds no effective capabilities, and cannot alter the firewall
  • the root filesystem is read-only and no host path is mounted

That the pinhole itself works — and that it is the ONLY thing that works — is
redteam_pinhole.py's job.

Run:  python tests/redteam_isolation.py
Exits non-zero if any security assertion fails.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator.runners import hardened_flags  # the exact flags the orchestrator uses

IMAGE = "terrarium-sandbox"
NETWORK = "terrarium-net"

# Use the orchestrator's own launch flags so the test can never drift from prod.
HARDENED_FLAGS = ["--rm", *hardened_flags(NETWORK)]

PROBE = r"""
fail=0
# `policy drop` in firewall.sh is unconditional; the pinhole rule is only added when
# TERRA_EGRESS_ALLOW_IP/PORT are set. None are set here, so EVERY destination must be
# dropped — a sandbox that can reach the internet without a mediator to reach it through
# is the failure this asserts against.
blocked() {
  curl -s --max-time 6 -o /dev/null "$2"; rc=$?
  # 28 = timeout (silently dropped, the shape we want). 6 = DNS failure, which is also a
  # pass: the resolver is cut, so the agent cannot even name a host to try.
  if [ $rc -eq 28 ] || [ $rc -eq 6 ]; then echo "ok   blocked $1 (dropped/timeout)";
  elif [ $rc -eq 0 ]; then echo "FAIL reached $1"; fail=1;
  else echo "FAIL $1 not cleanly dropped (curl rc=$rc)"; fail=1; fi
}
blocked internet-ip  https://1.1.1.1/
blocked internet-dns https://example.com/
blocked metadata     http://169.254.169.254/
blocked rfc1918-10   http://10.0.0.1/
blocked rfc1918-192  http://192.168.0.1/
blocked gateway      "http://$GW/"
[ "$(id -u)" = "1001" ] && echo "ok   non-root (uid 1001)" || { echo "FAIL uid=$(id -u)"; fail=1; }
CAP=$(awk '/CapEff/{print $2}' /proc/self/status)
[ "$CAP" = "0000000000000000" ] && echo "ok   no effective capabilities" || { echo "FAIL CapEff=$CAP"; fail=1; }
if nft add rule inet egress output accept 2>/dev/null; then echo "FAIL agent altered firewall"; fail=1; else echo "ok   cannot alter firewall"; fi
[ ! -e /host ] && echo "ok   no /host mount" || { echo "FAIL /host exists"; fail=1; }
if echo x > /rootfs_probe 2>/dev/null; then echo "FAIL rootfs writable"; rm -f /rootfs_probe; fail=1; else echo "ok   rootfs read-only"; fi
echo "RESULT: $([ $fail -eq 0 ] && echo PASS || echo FAIL)"
"""


def network_gateway(name: str) -> str:
    out = subprocess.check_output(["docker", "network", "inspect", name], text=True)
    cfg = json.loads(out)[0]["IPAM"]["Config"]
    return cfg[0].get("Gateway") or "172.18.0.1"


def main() -> int:
    gw = network_gateway(NETWORK)
    print(f"network gateway: {gw}\n--- probing sandbox ---")
    cmd = (
        ["docker", "run", *HARDENED_FLAGS, "-e", f"GW={gw}", IMAGE, "sh", "-c", PROBE]
    )
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(proc.stdout, end="")
    if proc.stderr.strip():
        print("--- container stderr ---")
        print(proc.stderr, end="")
    passed = "RESULT: PASS" in proc.stdout and proc.returncode == 0
    print("\n==> ISOLATION", "PASS ✅" if passed else "FAIL ❌")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
