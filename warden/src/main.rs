//! Terrarium Warden — per-session MITM egress gateway.
//! Phase 1: per-session CA, policy (shared policy.json), audit, CONNECT proxy.
//! Phases 2-6 (in progress): selective MITM, credential injection, content scan,
//! secret store, signed receipts, kill switch.
mod audit;
mod ca;
mod http;
mod inject;
mod scan;
mod config;
mod policy;
mod proxy;
mod tls;

use std::sync::{Arc, Mutex};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cfg = config::Config::from_env();

    // per-session CA → write the public cert for the sandbox to trust
    let session_ca = std::sync::Arc::new(ca::SessionCa::generate()?);
    session_ca.write_public(&cfg.ca_dir)?;

    let policy = Arc::new(Mutex::new(policy::Policy::new(cfg.policy_path.clone(), &cfg.static_allow)));
    let audit = Arc::new(audit::Audit::new(cfg.audit_path.clone()));
    let injector = Arc::new(inject::Injector::from_env());
    audit.log(serde_json::json!({"decision": "listening", "listen": cfg.listen, "phase": 3}));

    proxy::run(&cfg.listen, policy, audit, session_ca, injector).await
}
