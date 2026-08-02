#!/bin/sh
# Container entrypoint. Runs as root (with NET_ADMIN) just long enough to lock
# in the egress firewall, then permanently drops to the unprivileged `agent`
# user. The agent — and anything it spawns — runs without NET_ADMIN and cannot
# alter the firewall. `no-new-privileges` (set at launch) blocks regaining it.
set -eu

/opt/runtime/firewall.sh

# Deception: the firewall is up, so wipe the orchestrator/Warden tell-tale vars from
# the env the worker (and thus the agent) inherits — a compromised agent shouldn't be
# able to fingerprint the sandbox by reading /proc/<pid>/environ.
# (TERRA_HARNESS is kept here — the worker needs it — but is stripped from the CLI's
# own env before the agent is spawned.)
unset WARDEN_UID TERRA_EGRESS_ALLOW_IP TERRA_EGRESS_ALLOW_PORT TERRA_EGRESS_DEFAULT_DROP \
      2>/dev/null || true

# Optional: run an arbitrary command as the agent (used by the red-team test).
if [ "$#" -gt 0 ]; then
  exec gosu agent:agent "$@"
fi

exec gosu agent:agent python3 /opt/runtime/worker.py
