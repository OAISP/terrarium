#!/bin/sh
# In-container egress CHOKEPOINT. Runs as root (with NET_ADMIN) from the entrypoint,
# BEFORE we drop to the unprivileged agent user.
#
# This is L3/L4 ENFORCEMENT ONLY — it holds no destination policy. Its single job is to
# make Warden unbypassable: default-DROP everything, and permit exactly one route out for
# the agent — the loopback pinhole to the per-session Warden proxy. All destination policy
# (which domains / IPs / CIDRs an agent may reach, allow/deny/inspect, the private-range
# and cloud-metadata floors) lives in Warden's egress policy, evaluated against the
# resolved address. A raw socket the agent opens to anything but the pinhole is dropped
# here; everything it's allowed to reach goes through Warden and is mediated + audited.
#
#   allowed:  loopback (the proxy) · the Warden uid's own egress (k8s shared-netns) ·
#             the single Warden pinhole (TERRA_EGRESS_ALLOW_IP:PORT)
#   dropped:  every other destination, all direct IPs, DNS, and all IPv6
#
# Fail closed: if no firewall can be installed, exit non-zero so the agent never runs
# unprotected.
set -eu

# Capture install stderr in a root-owned, owner-only file (mktemp is 0600) rather than a
# world-readable /tmp path — an nft/iptables warning can echo the table name, the pinhole
# IP:port, or the Warden uid, which uid 1001 must not be able to read from tmpfs. Removed
# on exit so nothing lingers for the agent that runs after the privilege drop.
FW_ERR="$(mktemp)"
trap 'rm -f "$FW_ERR"' EXIT

# The one allowed egress endpoint: the per-session Warden sidecar's loopback pinhole.
ALLOW_RULE=""
if [ -n "${TERRA_EGRESS_ALLOW_IP:-}" ] && [ -n "${TERRA_EGRESS_ALLOW_PORT:-}" ]; then
  ALLOW_RULE="ip daddr ${TERRA_EGRESS_ALLOW_IP} tcp dport ${TERRA_EGRESS_ALLOW_PORT} accept"
fi

# Shared-netns (k8s) Warden mode: the Warden sidecar shares this Pod's network namespace,
# so this default-drop chokepoint also governs Warden's OWN egress (its DNS + upstream
# re-origination). Allow that one uid through — it's the mediator — while the agent stays
# confined to the loopback pinhole. (Not needed in Docker, where Warden has its own netns —
# WARDEN_UID is simply unset there.)
WARDEN_RULE=""
if [ -n "${WARDEN_UID:-}" ]; then
  WARDEN_RULE="meta skuid ${WARDEN_UID} accept"
fi

load_nft() {
  # NOTE: unquoted heredoc so the ${...} vars expand; nft rules contain no other $.
  nft -f - <<EOF
table inet egress {
  chain output {
    type filter hook output priority 0; policy drop;
    oifname "lo" accept
    ${WARDEN_RULE}
    ${ALLOW_RULE}
    ip6 daddr ::/0 drop
  }
}
EOF
}

load_iptables() {
  # Fail CLOSED. Run the whole sequence under `set -e` in a SUBSHELL so a mid-sequence
  # failure propagates as a non-zero return (the caller then hits the FATAL path instead of
  # reporting a half-installed ruleset as "active"). Policy DROP FIRST: a partial install can
  # then only over-block, never leak.
  (
    set -e
    iptables -P OUTPUT DROP
    # IPv6: fail CLOSED to match v4, but ONLY when an IPv6 stack is actually present
    # (/proc/net/if_inet6 exists). If v6 is up and we can't set the default DROP, aborting
    # (→ FATAL) is correct — leaving v6 at its default ACCEPT is an unmediated route out.
    # If v6 is disabled in the kernel, ip6tables would spuriously fail, so tolerate it there.
    if [ -e /proc/net/if_inet6 ]; then
      ip6tables -P OUTPUT DROP
    else
      ip6tables -P OUTPUT DROP 2>/dev/null || true
    fi
    iptables -A OUTPUT -o lo -j ACCEPT
    ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
    if [ -n "${WARDEN_UID:-}" ]; then
      iptables -A OUTPUT -m owner --uid-owner "$WARDEN_UID" -j ACCEPT
      ip6tables -A OUTPUT -m owner --uid-owner "$WARDEN_UID" -j ACCEPT 2>/dev/null || true
    fi
    if [ -n "${TERRA_EGRESS_ALLOW_IP:-}" ] && [ -n "${TERRA_EGRESS_ALLOW_PORT:-}" ]; then
      iptables -A OUTPUT -d "$TERRA_EGRESS_ALLOW_IP" -p tcp --dport "$TERRA_EGRESS_ALLOW_PORT" -j ACCEPT
    fi
  )
}

if command -v nft >/dev/null 2>&1 && load_nft 2>"$FW_ERR"; then
  echo "[firewall] nftables egress chokepoint active${ALLOW_RULE:+ (pinhole: $TERRA_EGRESS_ALLOW_IP:$TERRA_EGRESS_ALLOW_PORT)}" >&2
elif command -v iptables >/dev/null 2>&1 && load_iptables 2>"$FW_ERR"; then
  echo "[firewall] iptables egress chokepoint active" >&2
else
  echo "[firewall] FATAL: could not install egress firewall" >&2
  cat "$FW_ERR" >&2 2>/dev/null || true
  exit 1
fi
