// Shared egress logic for the firewall-rule UI: destination classification, action metadata,
// effective-reach summary, and a client-side decision SIMULATOR that mirrors Warden's
// precedence (warden/src/policy.rs::decide_ip). IPv4 is fully modeled; IPv6 CIDRs are matched
// best-effort (exact-only) — the console tester is a guide; Warden is authoritative.
import type { EgressRule, EgressHostOverride } from "@/lib/types";

export const ANTHROPIC_HOSTS = ["api.anthropic.com", "platform.claude.com"];

/**
 * Warden's wire decisions → words an operator can read. One source, because the SAME raw token
 * surfaced in two places (the Egress feed and the Logs "Type" column): `mitm` — our own
 * TLS-terminating inspection, the normal path for the agent's Anthropic traffic — printed verbatim
 * on most rows of the security pages, reading as an attack name. Never echo an unmapped token.
 */
export const DECISION_LABELS: Record<string, string> = {
  allow: "allowed",
  "monitor-allow": "logged",
  mitm: "inspected",
  "mitm-error": "inspection failed",
  "dlp-block": "blocked · secret",
  "dlp-hit": "secret flagged",
  "deny-method": "blocked · method",
  "upstream-error": "upstream error",
  "resolve-error": "dns error",
  closed: "closed",
  listening: "listening",
};

export function decisionLabel(d: string): string {
  return DECISION_LABELS[d] ?? (d.startsWith("deny") ? "blocked" : `other (${d})`);
}
const DEFAULT_PORTS = [80, 443];
const METADATA_V4 = "169.254.169.254";

export type DestKind = "domain" | "ip" | "cidr";
export type DestInfo = {
  kind: DestKind;
  /** true for a range broader than /16 (public) or any RFC1918/link-local supernet, or 0.0.0.0/0. */
  broad: boolean;
  /** whole-internet: 0.0.0.0/0 or ::/0. */
  entireInternet: boolean;
  /** host count for a CIDR (undefined for domain/ip). */
  hosts?: number;
};

const IPV4_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;

function v4ToInt(s: string): number | null {
  const m = IPV4_RE.exec(s);
  if (!m) return null;
  const o = m.slice(1).map(Number);
  if (o.some((n) => n > 255)) return null;
  return ((o[0] << 24) >>> 0) + (o[1] << 16) + (o[2] << 8) + o[3];
}

/** Classify a destination string and flag dangerous breadth. */
export function destType(value: string): DestInfo {
  const v = value.trim().toLowerCase();
  if (v.includes("/")) {
    const [net, pfxRaw] = v.split("/");
    const pfx = Number(pfxRaw);
    const isV4 = IPV4_RE.test(net);
    const entireInternet = pfx === 0;
    if (isV4 && Number.isFinite(pfx)) {
      const hosts = pfx >= 31 ? (pfx === 32 ? 1 : 2) : Math.pow(2, 32 - pfx);
      const v4 = v4ToInt(net) ?? 0;
      const rfc1918 = (v4 >>> 24) === 10 || (v4 >>> 20) === 0xac1 || (v4 >>> 16) === 0xc0a8; // 10/8, 172.16/12-ish, 192.168/16
      const broad = pfx === 0 || (pfx <= 16 && !rfc1918) || (rfc1918 && pfx <= 12) || (v4 >>> 24) === 10 && pfx <= 8;
      return { kind: "cidr", broad, entireInternet, hosts };
    }
    return { kind: "cidr", broad: pfx <= 32, entireInternet }; // v6 CIDR: /≤32 is very broad
  }
  if (IPV4_RE.test(v) || v.includes(":")) return { kind: "ip", broad: false, entireInternet: false };
  return { kind: "domain", broad: false, entireInternet: false };
}

/** Human host-count for a CIDR breadth badge. */
export function hostCountLabel(n?: number): string | null {
  if (!n || n <= 1) return null;
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M hosts`;
  if (n >= 1000) return `${Math.round(n / 1000)}K hosts`;
  return `${n} hosts`;
}

export type ActionKey = "allow" | "deny" | "inspect";
export const ACTION_META: Record<ActionKey, { label: string; token: string; short: string }> = {
  allow: { label: "Allow", token: "var(--accent)", short: "allow" },
  deny: { label: "Block", token: "var(--c-error)", short: "deny" },
  inspect: { label: "Allow + Inspect", token: "var(--c-result)", short: "inspect" },
};

const norm = (h: string) => h.trim().toLowerCase().replace(/\.$/, "").split(":")[0];

function v4InCidr(ip: string, dest: string): boolean {
  const [net, pfxRaw] = dest.split("/");
  const a = v4ToInt(ip);
  const n = v4ToInt(net);
  if (a === null || n === null) return false;
  const pfx = Number(pfxRaw);
  if (pfx === 0) return true;
  const mask = (0xffffffff << (32 - pfx)) >>> 0;
  return (a & mask) === (n & mask);
}

function isPrivateV4(ip: string): boolean {
  const n = v4ToInt(ip);
  if (n === null) return false;
  const o = [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255];
  return o[0] === 10 || (o[0] === 172 && o[1] >= 16 && o[1] <= 31) || (o[0] === 192 && o[1] === 168)
    || o[0] === 127 || o[0] === 169 && o[1] === 254 || o[0] === 0 || (o[0] === 100 && (o[1] & 0xc0) === 64);
}

function destMatches(rule: EgressRule, hnorm: string, ip: string | null): boolean {
  const d = rule.dest.toLowerCase();
  const info = destType(d);
  if (info.kind === "domain") return d === hnorm;
  if (!ip) return false; // IP/CIDR rules only match a known address
  if (info.kind === "cidr") return v4InCidr(ip, d);
  return norm(d) === ip;
}

const portOk = (rule: EgressRule, port: number) =>
  rule.ports && rule.ports.length ? rule.ports.includes(port) : DEFAULT_PORTS.includes(port);

export type SimVerdict = {
  decision: ActionKey | "block";
  reason: string;
  /** true when the input is a domain but the outcome could depend on the resolved IP. */
  approximate?: boolean;
};

/** Mirror Warden's decision for a (host, port). `host` may be a domain or an IP literal;
 * a matching host-override supplies the resolve address (as Warden does before DNS). */
export function simulate(
  rules: EgressRule[],
  mode: "enforce" | "monitor",
  allowMetadata: boolean,
  host: string,
  port: number,
  hosts: EgressHostOverride[] = [],
): SimVerdict {
  const on = rules.filter((r) => r.enabled !== false);
  const h = norm(host);
  const info = destType(host);
  const override = hosts.find((o) => norm(o.host) === h)?.ip ?? null;
  const ip = override ?? (info.kind === "ip" ? h : null);
  const monitor = mode === "monitor";

  if (ANTHROPIC_HOSTS.includes(h) && DEFAULT_PORTS.includes(port))
    return { decision: "inspect", reason: "Anthropic · always reachable + TLS-inspected + credential-injected" };

  if (on.some((r) => r.action === "deny" && destMatches(r, h, ip)))
    return monitor ? { decision: "allow", reason: "deny is inert in monitor mode (logged, not blocked)" } : { decision: "block", reason: "matched a Block rule" };

  if (ip && ip === METADATA_V4 && !allowMetadata)
    return { decision: "block", reason: "cloud-metadata floor · requires the allow_metadata switch" };

  const coverAt = (a: ActionKey) => on.some((r) => r.action === a && destMatches(r, h, ip) && portOk(r, port));
  const coverAny = (a: ActionKey) => on.some((r) => r.action === a && destMatches(r, h, ip));
  const inspectAt = coverAt("inspect"), allowAt = coverAt("allow");
  const destCovered = coverAny("allow") || coverAny("inspect");
  const approximate = !override && info.kind === "domain" && (on.some((r) => destType(r.dest).kind !== "domain"));

  if (ip && isPrivateV4(ip) && !destCovered)
    return { decision: "block", reason: "private-range floor · no allow rule covers this internal address" };

  if (inspectAt) return { decision: "inspect", reason: "matched an Allow + Inspect rule (TLS-terminated)", approximate };
  if (allowAt) return { decision: "allow", reason: "matched an Allow rule (opaque tunnel)", approximate };
  if (monitor) return { decision: "allow", reason: "monitor default-allow + log", approximate };
  if (destCovered) return { decision: "block", reason: `port ${port} not allowed for this destination`, approximate };
  if (!DEFAULT_PORTS.includes(port)) return { decision: "block", reason: `port ${port} · only 80/443 without a matching rule`, approximate };
  return { decision: "block", reason: "not allow-listed (enforce blocks anything not allowed)", approximate };
}

/** A resolved, plain-language reachability verdict for the profile header / card. */
export type Reach = { tone: "safe" | "warn" | "danger"; headline: string; counts: { allow: number; deny: number; inspect: number } };
export function effectiveReach(rules: EgressRule[], mode: "enforce" | "monitor", allowMetadata: boolean): Reach {
  const on = rules.filter((r) => r.enabled !== false);
  const counts = {
    allow: on.filter((r) => r.action === "allow").length,
    deny: on.filter((r) => r.action === "deny").length,
    inspect: on.filter((r) => r.action === "inspect").length,
  };
  const openInternet = on.some((r) => r.action === "allow" && destType(r.dest).entireInternet);
  if (openInternet) return { tone: "danger", headline: "Open to the entire internet · the allow-list is defeated", counts };
  if (mode === "monitor") return { tone: "warn", headline: "Monitor mode · nothing is blocked, only logged; private + metadata floors still apply", counts };
  if (allowMetadata) return { tone: "danger", headline: `Cloud metadata reachable · ${counts.allow + counts.inspect} allowed`, counts };
  if (counts.allow + counts.inspect === 0) return { tone: "safe", headline: "Anthropic only · every other host is blocked", counts };
  return { tone: "safe", headline: `${counts.allow + counts.inspect} allowed · ${counts.deny} blocked · every other host is blocked`, counts };
}
