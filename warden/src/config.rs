use std::path::PathBuf;

/// Runtime config from env (set by the runner when it spawns the sidecar).
pub struct Config {
    pub listen: String,             // WARDEN_LISTEN          (default 127.0.0.1:8888 — loopback only)
    pub policy_path: Option<PathBuf>, // WARDEN_POLICY        (shared policy.json, hot-reloaded)
    pub audit_path: Option<PathBuf>,  // WARDEN_AUDIT         (shared audit.jsonl, appended)
    pub ca_dir: PathBuf,            // WARDEN_CA_DIR          (we write session-ca.pem here, public)
    pub static_allow: Vec<String>,  // WARDEN_ALLOW           (comma list, fallback allow)
}

impl Config {
    pub fn from_env() -> Self {
        let env = |k: &str| std::env::var(k).ok();
        Config {
            // Loopback by default: the credential-injecting proxy must never be
            // reachable beyond the sandbox's own netns. A missing/typo'd
            // WARDEN_LISTEN must fail closed (unreachable), not expose the proxy
            // on every interface where a co-located container could drive it.
            listen: env("WARDEN_LISTEN").unwrap_or_else(|| "127.0.0.1:8888".into()),
            policy_path: env("WARDEN_POLICY").map(PathBuf::from),
            audit_path: env("WARDEN_AUDIT").map(PathBuf::from),
            ca_dir: env("WARDEN_CA_DIR").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/ca")),
            static_allow: env("WARDEN_ALLOW").unwrap_or_default()
                .split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect(),
        }
    }
}
