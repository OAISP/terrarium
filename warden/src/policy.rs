//! Egress policy — the SAME model + policy.json the Python control plane writes
//! (orchestrator/egress.py). Warden adds the MITM decision on top.
//!
//! A policy is a flat list of RULES. Each rule is `{action, dest, ports}` where action is
//! allow / deny / inspect, dest is an exact domain, an IP literal, or a CIDR, and ports is the
//! set of destination ports the rule covers (default 80 + 443). Domain dests match the CONNECT
//! hostname; IP/CIDR dests match the RESOLVED address. The private-range guard and the
//! cloud-metadata deny are policy defaults an explicit allow/inspect rule can lift.
//!
//! Wire format is a superset for zero-downtime rollout: prefer `rules[]`; if absent, fall back
//! to the legacy `allow[]/deny[]/inspect[]` string lists (which imply the default 80/443 ports).
use serde::Deserialize;
use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::path::PathBuf;
use std::time::SystemTime;

pub const ANTHROPIC_HOSTS: &[&str] = &["api.anthropic.com", "platform.claude.com"];
const DEFAULT_PORTS: &[u16] = &[80, 443];

#[derive(Debug, Clone, PartialEq)]
pub enum Decision {
    Mitm,          // TLS-terminate: inject credential + scan content (Anthropic, or inspect rules)
    Tunnel,        // opaque allow (no CA exposure, Warden sees only ciphertext)
    Deny(String),  // 403 + reason
}

/// A host-level pre-decision. `Resolved` when the hostname alone settles it (kill, Anthropic,
/// domain deny, or an IP-literal target); `NeedsIp` when the outcome depends on the resolved
/// address — the caller resolves, then calls [`Policy::decide_ip`] with the address it dials.
#[derive(Debug, Clone, PartialEq)]
pub enum HostDecision {
    Resolved(Decision),
    NeedsIp,
}

// --- destination matchers (domain | ip | cidr) -------------------------------------------

#[derive(Debug, Clone)]
enum Matcher {
    Domain(String),
    Ip(IpRule),
}

#[derive(Debug, Clone)]
enum IpRule {
    Ip(IpAddr),
    Cidr(IpAddr, u8), // network base (masked), prefix length
}

impl IpRule {
    fn parse(s: &str) -> Option<IpRule> {
        let s = s.trim();
        if let Some((net, pfx)) = s.split_once('/') {
            let ip: IpAddr = net.trim().parse().ok()?;
            let pfx: u8 = pfx.trim().parse().ok()?;
            let max = if ip.is_ipv4() { 32 } else { 128 };
            if pfx > max {
                return None;
            }
            Some(IpRule::Cidr(mask_ip(ip, pfx), pfx))
        } else {
            Some(IpRule::Ip(s.parse().ok()?))
        }
    }
    fn contains(&self, ip: &IpAddr) -> bool {
        match self {
            IpRule::Ip(a) => a == ip,
            IpRule::Cidr(net, pfx) => in_cidr(ip, net, *pfx),
        }
    }
}

fn mask_ip(ip: IpAddr, pfx: u8) -> IpAddr {
    match ip {
        IpAddr::V4(a) => {
            let mask = if pfx == 0 { 0 } else { u32::MAX << (32 - pfx as u32) };
            IpAddr::V4(Ipv4Addr::from(u32::from(a) & mask))
        }
        IpAddr::V6(a) => {
            let mask = if pfx == 0 { 0 } else { u128::MAX << (128 - pfx as u32) };
            IpAddr::V6(Ipv6Addr::from(u128::from(a) & mask))
        }
    }
}

fn in_cidr(ip: &IpAddr, net: &IpAddr, pfx: u8) -> bool {
    match (ip, net) {
        (IpAddr::V4(a), IpAddr::V4(n)) => {
            let mask = if pfx == 0 { 0 } else { u32::MAX << (32 - pfx as u32) };
            (u32::from(*a) & mask) == (u32::from(*n) & mask)
        }
        (IpAddr::V6(a), IpAddr::V6(n)) => {
            let mask = if pfx == 0 { 0 } else { u128::MAX << (128 - pfx as u32) };
            (u128::from(*a) & mask) == (u128::from(*n) & mask)
        }
        _ => false, // v4 rule never matches a v6 address or vice versa
    }
}

// --- built-in destination floors (formerly the hardcoded SSRF guard in proxy.rs) ---------

/// A private / loopback / link-local / CGNAT / broadcast / documentation / unspecified
/// destination. Denied by default; an explicit allow/inspect rule lifts it (per-policy).
pub fn is_blocked_range(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => ipv4_blocked(v4),
        IpAddr::V6(v6) => ipv6_blocked(v6),
    }
}

fn ipv4_blocked(v4: &Ipv4Addr) -> bool {
    let o = v4.octets();
    v4.is_private() || v4.is_loopback() || v4.is_link_local()
        || v4.is_broadcast() || v4.is_documentation() || v4.is_unspecified()
        || o[0] == 0
        || (o[0] == 100 && (o[1] & 0xc0) == 64) // CGNAT 100.64/10
}

fn ipv6_blocked(v6: &Ipv6Addr) -> bool {
    if v6.is_loopback() || v6.is_unspecified() {
        return true;
    }
    if let Some(v4) = v6.to_ipv4_mapped() {
        return ipv4_blocked(&v4);
    }
    let seg0 = v6.segments()[0];
    (seg0 & 0xfe00) == 0xfc00 || (seg0 & 0xffc0) == 0xfe80
}

/// Cloud-metadata service address (the classic SSRF→credential pivot). A HARD floor: a broad
/// allow (e.g. 169.254.0.0/16) does NOT reopen it — only the explicit `allow_metadata` flag.
pub fn is_metadata(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => *v4 == Ipv4Addr::new(169, 254, 169, 254),
        // Normalize an IPv4-mapped address (::ffff:169.254.169.254) back to v4 FIRST, so a
        // broad v6 allow (::/0, ::ffff:0:0/96) can't slip past the v4 floor the way it would
        // if we only compared the native-v6 IMDS literal. Then check that native literal.
        IpAddr::V6(v6) => match v6.to_ipv4_mapped() {
            Some(v4) => is_metadata(&IpAddr::V4(v4)),
            None => *v6 == Ipv6Addr::new(0xfd00, 0x0ec2, 0, 0, 0, 0, 0, 0x0254),
        },
    }
}

// --- policy ------------------------------------------------------------------------------

fn norm(h: &str) -> String {
    h.trim().to_lowercase().split(':').next().unwrap_or("").trim_end_matches('.').to_string()
}

#[derive(Debug, Clone)]
struct Rule {
    matcher: Matcher,
    ports: Option<Vec<u16>>, // None → DEFAULT_PORTS (80/443)
}

impl Rule {
    /// Does this rule's destination match, ignoring port? (deny is dest-only; allow/inspect
    /// also require `port_ok`.)
    fn dest_matches(&self, host_norm: &str, ip: &IpAddr) -> bool {
        match &self.matcher {
            Matcher::Domain(d) => d == host_norm,
            Matcher::Ip(r) => r.contains(ip),
        }
    }
    fn port_ok(&self, port: u16) -> bool {
        match &self.ports {
            Some(ps) => ps.contains(&port),
            None => DEFAULT_PORTS.contains(&port),
        }
    }
}

/// Parse one dest string into a matcher (IP/CIDR first, else a normalized domain; wildcards
/// and empties dropped → None).
fn parse_matcher(dest: &str) -> Option<Matcher> {
    let t = dest.trim();
    if t.is_empty() {
        return None;
    }
    if let Some(r) = IpRule::parse(t) {
        return Some(Matcher::Ip(r));
    }
    let d = norm(t);
    if d.is_empty() || d.contains('*') {
        return None;
    }
    Some(Matcher::Domain(d))
}

#[derive(Deserialize)]
struct RuleJson {
    #[serde(default)] action: String,
    #[serde(default)] dest: String,
    #[serde(default)] ports: Option<Vec<u16>>,
    #[serde(default = "default_true")] enabled: bool,
    // `note` is operator metadata — Warden ignores it (accepted so the file round-trips).
    #[allow(dead_code)] #[serde(default)] note: Option<String>,
}
fn default_true() -> bool { true }

#[derive(Deserialize)]
struct HostOverrideJson {
    #[serde(default)] host: String,
    #[serde(default)] ip: String,
}

#[derive(Deserialize, Default)]
struct PolicyFile {
    #[serde(default)] mode: String,
    #[serde(default)] rules: Vec<RuleJson>,
    // static host→ip overrides: Warden resolves these names to the given address instead of
    // asking DNS — the mechanism for reaching an internal name (e.g. git.kokolab.moe) whose
    // record lives only on your internal DNS server (which the sandbox's resolver can't see).
    #[serde(default)] hosts: Vec<HostOverrideJson>,
    #[serde(default)] kill: bool,
    #[serde(default)] allow_metadata: bool,
}

#[derive(Default)]
struct Ruleset {
    allow: Vec<Rule>,
    deny: Vec<Rule>,
    inspect: Vec<Rule>,
}

impl Ruleset {
    fn from_rules(rules: &[RuleJson]) -> Ruleset {
        let mut rs = Ruleset::default();
        for r in rules {
            if !r.enabled {
                continue;
            }
            let Some(m) = parse_matcher(&r.dest) else { continue };
            let rule = Rule { matcher: m, ports: r.ports.clone() };
            match r.action.as_str() {
                "deny" => rs.deny.push(rule),
                "inspect" => rs.inspect.push(rule),
                _ => rs.allow.push(rule), // default/unknown action → allow-shaped (safe: still floor-gated)
            }
        }
        rs
    }
}

pub struct Policy {
    path: Option<PathBuf>,
    mtime: Option<SystemTime>,
    mode: String, // "enforce" | "monitor"
    rules: Ruleset,
    overrides: HashMap<String, IpAddr>, // host (normalized) → static resolve address
    kill: bool,
    allow_metadata: bool,
}

/// Whether a rule set covers (host, ip) at `port` — and, separately, at ANY port (so we can
/// tell "wrong port" from "not matched at all" for a precise deny reason).
struct Cover {
    at_port: bool,
    any_port: bool,
}
fn cover(rules: &[Rule], h: &str, ip: &IpAddr, port: u16) -> Cover {
    let mut c = Cover { at_port: false, any_port: false };
    for r in rules {
        if r.dest_matches(h, ip) {
            c.any_port = true;
            if r.port_ok(port) {
                c.at_port = true;
                break;
            }
        }
    }
    c
}

impl Policy {
    pub fn new(path: Option<PathBuf>, static_allow: &[String]) -> Self {
        let seed: Vec<Rule> = static_allow.iter().filter_map(|s| parse_matcher(s).map(|m| Rule { matcher: m, ports: None })).collect();
        let mut p = Policy {
            path, mtime: None, mode: "enforce".into(),
            rules: Ruleset { allow: seed, ..Default::default() },
            overrides: HashMap::new(), kill: false, allow_metadata: false,
        };
        p.reload();
        p
    }

    pub fn reload(&mut self) {
        let path = match &self.path { Some(p) => p.clone(), None => return };
        let mtime = std::fs::metadata(&path).ok().and_then(|m| m.modified().ok());
        if self.mtime.is_some() && mtime == self.mtime { return; }
        let txt = match std::fs::read_to_string(&path) { Ok(t) => t, Err(_) => return };
        let pf: PolicyFile = match serde_json::from_str(&txt) { Ok(p) => p, Err(_) => return }; // keep last-good
        self.mode = if pf.mode == "monitor" { "monitor".into() } else { "enforce".into() };
        self.rules = Ruleset::from_rules(&pf.rules);
        self.overrides = pf.hosts.iter()
            .filter_map(|h| h.ip.trim().parse::<IpAddr>().ok().map(|ip| (norm(&h.host), ip)))
            .filter(|(h, _)| !h.is_empty())
            .collect();
        self.kill = pf.kill;
        self.allow_metadata = pf.allow_metadata;
        self.mtime = mtime;
    }

    /// Host-level pre-decision (no DNS). Settles kill / Anthropic / domain-deny / IP-literal;
    /// returns `NeedsIp` for a domain that isn't outright denied.
    pub fn decide_host(&self, host: &str, port: u16) -> HostDecision {
        if self.kill {
            return HostDecision::Resolved(Decision::Deny("killed".into()));
        }
        let h = norm(host);
        if ANTHROPIC_HOSTS.contains(&h.as_str()) && DEFAULT_PORTS.contains(&port) {
            return HostDecision::Resolved(Decision::Mitm);
        }
        let monitor = self.mode == "monitor";
        // A DOMAIN deny (dest-only) can short-circuit without DNS. IP/CIDR denies need the addr.
        let domain_denied = self.rules.deny.iter().any(|r| matches!(&r.matcher, Matcher::Domain(d) if *d == h));
        if domain_denied {
            return HostDecision::Resolved(if monitor { Decision::Tunnel } else { Decision::Deny("deny-listed".into()) });
        }
        if let Ok(ip) = host.trim().parse::<IpAddr>() {
            return HostDecision::Resolved(self.decide_ip(host, port, ip));
        }
        HostDecision::NeedsIp
    }

    /// Full decision against the address that will be dialed. Precedence: kill > Anthropic >
    /// deny > metadata-floor > (allow/inspect with per-rule ports) > private-floor >
    /// port-wall > default. Also the mid-tunnel re-check point.
    pub fn decide_ip(&self, host: &str, port: u16, ip: IpAddr) -> Decision {
        if self.kill {
            return Decision::Deny("killed".into());
        }
        let h = norm(host);
        if ANTHROPIC_HOSTS.contains(&h.as_str()) && DEFAULT_PORTS.contains(&port) {
            return Decision::Mitm;
        }
        let monitor = self.mode == "monitor";
        // deny is dest-only (blocks the destination on every port)
        if self.rules.deny.iter().any(|r| r.dest_matches(&h, &ip)) {
            return if monitor { Decision::Tunnel } else { Decision::Deny("deny-listed".into()) };
        }
        // metadata: hard floor — only allow_metadata lifts it, and it still needs an allow rule
        // (checked via the private-floor below); a broad allow CIDR alone can't reach it.
        if is_metadata(&ip) && !self.allow_metadata {
            return Decision::Deny("metadata".into());
        }
        let inspect = cover(&self.rules.inspect, &h, &ip, port);
        let allow = cover(&self.rules.allow, &h, &ip, port);
        let dest_covered = allow.any_port || inspect.any_port; // operator allow-listed this dest (any port)
        // private-range floor: a blocked destination is denied in BOTH modes unless the operator
        // explicitly allow/inspect-listed it (the SSRF safety net, overridable per-policy). Uses
        // any-port coverage: a CIDR allowed only on :5432 still lifts the floor, so a wrong-port
        // hit falls through to the precise "port-not-allowed" below rather than this generic deny.
        if is_blocked_range(&ip) && !dest_covered {
            return Decision::Deny("private-not-allow-listed".into());
        }
        if inspect.at_port {
            return Decision::Mitm;
        }
        if allow.at_port {
            return Decision::Tunnel;
        }
        // Not matched at this port. Monitor is permissive (default-allow + log), so it tunnels
        // anything that survived the floors. Enforce denies — distinguishing "matched the host
        // but not the port" (port-wall) from "no rule at all" for a precise, honest audit reason.
        if monitor {
            return Decision::Tunnel;
        }
        if dest_covered {
            // host/CIDR is allow-listed but not on this port
            return Decision::Deny("port-not-allowed".into());
        }
        if !DEFAULT_PORTS.contains(&port) {
            return Decision::Deny("bad-port".into());
        }
        Decision::Deny("not-allow-listed".into())
    }

    pub fn is_enforce(&self) -> bool {
        self.mode == "enforce"
    }

    /// A static resolve address for `host`, if the policy defines one — used INSTEAD of DNS so
    /// an internal name unknown to the sandbox's resolver still reaches its (private) address.
    pub fn host_override(&self, host: &str) -> Option<IpAddr> {
        self.overrides.get(&norm(host)).copied()
    }

    /// Test/convenience shim: settle from the hostname alone, evaluating a domain target
    /// against a placeholder PUBLIC address so domain rules apply and the private floor never
    /// false-fires. Real callers use decide_host + decide_ip with the dialed address.
    #[cfg(test)]
    pub fn decide(&self, host: &str, port: u16) -> Decision {
        match self.decide_host(host, port) {
            HostDecision::Resolved(d) => d,
            HostDecision::NeedsIp => self.decide_ip(host, port, IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn policy_with(json: &str) -> Policy {
        // Unique per call: json.len() collides for equal-length policies, racing two parallel
        // tests on the same temp file (which flaked kill_switch_denies_everything).
        static SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let n = SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("warden-pol-{}-{}", std::process::id(), n));
        std::fs::create_dir_all(&dir).unwrap();
        let pf = dir.join("policy.json");
        std::fs::write(&pf, json).unwrap();
        Policy::new(Some(pf), &[])
    }
    fn ip(s: &str) -> IpAddr { s.parse().unwrap() }

    #[test]
    fn enforce_decisions() {
        let p = policy_with(r#"{"mode":"enforce","rules":[
            {"action":"allow","dest":"api.github.com"},
            {"action":"allow","dest":"PyPI.org"},
            {"action":"allow","dest":"*.bad"},
            {"action":"deny","dest":"telemetry.evil.com"}
        ]}"#);
        assert_eq!(p.decide("api.anthropic.com", 443), Decision::Mitm);
        assert_eq!(p.decide("api.github.com", 443), Decision::Tunnel);
        assert_eq!(p.decide("PYPI.ORG", 443), Decision::Tunnel);
        assert_eq!(p.decide("telemetry.evil.com", 443), Decision::Deny("deny-listed".into()));
        // allow-listed host, non-standard port → precise "port-not-allowed" (host matched, port didn't)
        assert_eq!(p.decide("api.github.com", 22), Decision::Deny("port-not-allowed".into()));
        assert_eq!(p.decide("random.com", 443), Decision::Deny("not-allow-listed".into()));
        assert_eq!(p.decide("evil.bad", 443), Decision::Deny("not-allow-listed".into())); // wildcard dropped
    }
    #[test]
    fn monitor_and_inspect() {
        let p = policy_with(r#"{"mode":"monitor","rules":[{"action":"deny","dest":"telemetry.evil.com"}]}"#);
        assert_eq!(p.decide("random.com", 443), Decision::Tunnel);
        assert_eq!(p.decide("telemetry.evil.com", 443), Decision::Tunnel); // deny is inert in monitor
        assert_eq!(p.decide("api.anthropic.com", 443), Decision::Mitm);
        let p = policy_with(r#"{"mode":"enforce","rules":[{"action":"allow","dest":"a.com"},{"action":"inspect","dest":"scan.me"}]}"#);
        assert_eq!(p.decide("scan.me", 443), Decision::Mitm);
        assert_eq!(p.decide("a.com", 443), Decision::Tunnel);
    }
    #[test]
    fn kill_switch_denies_everything() {
        let p = policy_with(r#"{"mode":"monitor","kill":true,"rules":[{"action":"allow","dest":"a.com"}]}"#);
        assert!(matches!(p.decide("api.anthropic.com", 443), Decision::Deny(_)));
        assert!(matches!(p.decide("a.com", 443), Decision::Deny(_)));
    }
    #[test]
    fn rules_basic_actions() {
        let p = policy_with(r#"{"mode":"enforce","rules":[
            {"action":"allow","dest":"github.com"},
            {"action":"deny","dest":"telemetry.evil.com"},
            {"action":"inspect","dest":"pypi.org"},
            {"action":"allow","dest":"old.host","enabled":false}
        ]}"#);
        assert_eq!(p.decide("github.com", 443), Decision::Tunnel);
        assert_eq!(p.decide("pypi.org", 443), Decision::Mitm);
        assert_eq!(p.decide("telemetry.evil.com", 443), Decision::Deny("deny-listed".into()));
        assert_eq!(p.decide("old.host", 443), Decision::Deny("not-allow-listed".into())); // disabled → absent
    }
    #[test]
    fn rules_per_rule_ports() {
        // an allow rule may open non-standard ports for its destination (internal services)
        let p = policy_with(r#"{"mode":"enforce","rules":[
            {"action":"allow","dest":"10.20.0.0/16","ports":[443,5432,8443]},
            {"action":"allow","dest":"github.com"}
        ]}"#);
        assert_eq!(p.decide_ip("db", 5432, ip("10.20.0.5")), Decision::Tunnel);   // internal DB port, allowed
        assert_eq!(p.decide_ip("db", 8443, ip("10.20.0.5")), Decision::Tunnel);   // internal https-alt
        assert_eq!(p.decide_ip("db", 443, ip("10.20.0.5")), Decision::Tunnel);
        // a port the CIDR rule doesn't list → precise "port-not-allowed", not a silent deny
        assert_eq!(p.decide_ip("db", 6379, ip("10.20.0.5")), Decision::Deny("port-not-allowed".into()));
        // github (default ports) on :8443 → the host matched but not the port
        assert_eq!(p.decide("github.com", 8443), Decision::Deny("port-not-allowed".into()));
        // an entirely unlisted host on a weird port → bad-port
        assert_eq!(p.decide("nope.com", 9999), Decision::Deny("bad-port".into()));
    }
    #[test]
    fn rules_private_and_metadata_floors() {
        let p = policy_with(r#"{"mode":"enforce","rules":[{"action":"allow","dest":"github.com"}]}"#);
        assert!(matches!(p.decide_ip("x", 443, ip("10.20.1.5")), Decision::Deny(_))); // private default-denied
        let p = policy_with(r#"{"mode":"enforce","rules":[{"action":"allow","dest":"10.20.0.0/16"}]}"#);
        assert_eq!(p.decide_ip("x", 443, ip("10.20.1.5")), Decision::Tunnel);          // CIDR lifts the floor
        // metadata: a broad link-local allow can't reopen it; the flag alone can't either (needs a rule)
        let p = policy_with(r#"{"mode":"enforce","rules":[{"action":"allow","dest":"169.254.0.0/16"}]}"#);
        assert_eq!(p.decide_ip("m", 443, ip("169.254.169.254")), Decision::Deny("metadata".into()));
        assert_eq!(p.decide_ip("l", 443, ip("169.254.10.10")), Decision::Tunnel);
        let p = policy_with(r#"{"mode":"enforce","allow_metadata":true,"rules":[{"action":"allow","dest":"169.254.169.254"}]}"#);
        assert_eq!(p.decide_ip("m", 443, ip("169.254.169.254")), Decision::Tunnel);
        // IPv4-mapped IMDS (::ffff:169.254.169.254) must hit the SAME hard floor — a broad v6
        // allow can't reopen the metadata pivot the way it would if only the native-v6 literal
        // were floored. (Regression guard for the mapped-address bypass.)
        assert!(is_metadata(&ip("::ffff:169.254.169.254")));
        let p = policy_with(r#"{"mode":"enforce","rules":[{"action":"allow","dest":"::/0"}]}"#);
        assert_eq!(p.decide_ip("m", 443, ip("::ffff:169.254.169.254")), Decision::Deny("metadata".into()));
        // and it's genuinely lifted only by the flag, like the v4 form
        let p = policy_with(r#"{"mode":"enforce","allow_metadata":true,"rules":[{"action":"allow","dest":"::/0"}]}"#);
        assert_eq!(p.decide_ip("m", 443, ip("::ffff:169.254.169.254")), Decision::Tunnel);
    }
    #[test]
    fn ip_cidr_matching_and_deny_precedence() {
        let p = policy_with(r#"{"mode":"enforce","rules":[
            {"action":"allow","dest":"198.51.100.0/24"},
            {"action":"deny","dest":"198.51.100.9"}
        ]}"#);
        assert_eq!(p.decide_ip("h", 443, ip("198.51.100.42")), Decision::Tunnel);
        assert!(matches!(p.decide_ip("h", 443, ip("198.51.100.9")), Decision::Deny(_))); // deny beats allow CIDR
    }
    #[test]
    fn host_override_resolves_internal_name() {
        // git.kokolab.moe → an internal IP; an allow rule (domain) authorizes it. The override
        // supplies the address DNS can't; the domain rule lifts the private-range floor → tunnel.
        let p = policy_with(r#"{"mode":"enforce",
            "hosts":[{"host":"git.kokolab.moe","ip":"10.1.20.50"}],
            "rules":[{"action":"allow","dest":"git.kokolab.moe"}]}"#);
        assert_eq!(p.host_override("git.kokolab.moe"), Some(ip("10.1.20.50")));
        assert_eq!(p.host_override("GIT.kokolab.moe:443"), Some(ip("10.1.20.50"))); // normalized
        assert_eq!(p.host_override("other.host"), None);
        // decided against the override address: domain allow rule lifts the floor → Tunnel
        assert_eq!(p.decide_ip("git.kokolab.moe", 443, ip("10.1.20.50")), Decision::Tunnel);
        // without an allow rule, the override address is still floor-denied (override ≠ authorization)
        let p2 = policy_with(r#"{"mode":"enforce","hosts":[{"host":"git.kokolab.moe","ip":"10.1.20.50"}]}"#);
        assert!(matches!(p2.decide_ip("git.kokolab.moe", 443, ip("10.1.20.50")), Decision::Deny(_)));
    }

    #[test]
    fn cidr_boundaries() {
        assert!(IpRule::parse("10.0.0.0/8").unwrap().contains(&ip("10.255.255.255")));
        assert!(!IpRule::parse("10.0.0.0/8").unwrap().contains(&ip("11.0.0.0")));
        assert!(IpRule::parse("0.0.0.0/0").unwrap().contains(&ip("8.8.8.8")));
        assert!(IpRule::parse("fd00::/8").unwrap().contains(&ip("fd12:3456::1")));
        assert!(IpRule::parse("not-an-ip").is_none());
        assert!(IpRule::parse("10.0.0.0/33").is_none());
    }
}
