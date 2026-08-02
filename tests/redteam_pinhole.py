#!/usr/bin/env python3
"""Red-team the Tier-2 firewall pinhole.

Starts a sidecar "proxy" container on the agent network (mirroring the real
egress proxy), launches the sandbox with the pinhole env (TERRA_EGRESS_ALLOW_IP/
PORT = that sidecar), and asserts the agent can reach *exactly that* endpoint —
while a different port on the same sidecar, and cloud metadata, stay blocked.

Run:  uv run python tests/redteam_pinhole.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator.runners import hardened_flags

IMAGE = "terrarium-sandbox"
NETWORK = "terrarium-net"
PROXY_NAME = "terrarium-pinhole-test"
PORT = 8199
OTHER_PORT = 8200

DUMMY = (
    "import http.server,socketserver\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    " def log_message(self,*a): pass\n"
    " def do_GET(self):\n"
    "  self.send_response(200); self.send_header('content-length','9'); self.end_headers(); self.wfile.write(b'PROXY_OK\\n')\n"
    f"socketserver.TCPServer(('0.0.0.0',{PORT}),H).serve_forever()\n"
)

PROBE = """
fail=0
out=$(curl -s --max-time 6 http://{ip}:{port}/ || true)
case "$out" in *PROXY_OK*) echo "ok   pinhole reachable ({ip}:{port})";; *) echo "FAIL pinhole NOT reachable"; fail=1;; esac
curl -s --max-time 6 -o /dev/null "http://{ip}:{other}/"; rc=$?
[ $rc -eq 28 ] && echo "ok   other port on proxy host blocked" || {{ echo "FAIL other port reachable (rc=$rc)"; fail=1; }}
curl -s --max-time 6 -o /dev/null http://169.254.169.254/; rc=$?
[ $rc -eq 28 ] && echo "ok   cloud metadata still blocked" || {{ echo "FAIL metadata reachable (rc=$rc)"; fail=1; }}
echo "RESULT: $([ $fail -eq 0 ] && echo PASS || echo FAIL)"
"""


def _docker(*args: str) -> tuple[int, str]:
    p = subprocess.run(["docker", *args], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def proxy_ip() -> str | None:
    rc, out = _docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", PROXY_NAME)
    if rc != 0:
        return None
    try:
        for net in json.loads(out.strip()).values():
            if net.get("IPAddress"):
                return net["IPAddress"]
    except Exception:
        return None
    return None


def main() -> int:
    _docker("rm", "-f", PROXY_NAME)
    rc, out = _docker(
        "run", "-d", "--name", PROXY_NAME, "--network", NETWORK,
        "--entrypoint", "python3", IMAGE, "-c", DUMMY,
    )
    if rc != 0:
        print("could not start sidecar:", out)
        return 1
    try:
        import time

        ip = None
        for _ in range(10):
            ip = proxy_ip()
            if ip:
                break
            time.sleep(0.3)
        if not ip:
            print("could not resolve sidecar IP")
            return 1
        print(f"sidecar proxy at {ip}:{PORT}\n--- probing pinhole ---")
        cmd = [
            "docker", "run", "--rm", *hardened_flags(NETWORK),
            "-e", f"TERRA_EGRESS_ALLOW_IP={ip}", "-e", f"TERRA_EGRESS_ALLOW_PORT={PORT}",
            IMAGE, "sh", "-c", PROBE.format(ip=ip, port=PORT, other=OTHER_PORT),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        print(proc.stdout, end="")
        if proc.stderr.strip():
            print("--- container stderr ---")
            print(proc.stderr, end="")
        passed = "RESULT: PASS" in proc.stdout
        print("\n==> PINHOLE", "PASS ✅" if passed else "FAIL ❌")
        return 0 if passed else 1
    finally:
        _docker("rm", "-f", PROXY_NAME)


if __name__ == "__main__":
    sys.exit(main())
