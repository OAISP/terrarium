//! Audit — same JSONL contract as the Python gateway (decision/host/reason/...),
//! plus an optional HMAC-SHA256 *receipt* per line so the orchestrator can detect
//! tampering. The receipt is a HASH CHAIN over the FULL record:
//!
//!     receipt_n = HMAC(key, receipt_{n-1} || "\n" || canonical(record_n))
//!
//! where `canonical` is the key-sorted JSON of the record minus the receipt, and
//! each line carries a monotonic `seq`. This binds every field (not just
//! ts|decision|host) and chains lines together, so deleting, reordering, or
//! editing any line — including silently dropping `deny`/`dlp-block` entries —
//! breaks verification. Genesis is the empty previous receipt.
// KeyInit is imported explicitly: hmac 0.13 stopped re-exporting it through Mac, so a
// crate bump turns `new_from_slice` into a compile error rather than a behaviour change.
use hmac::{Hmac, KeyInit, Mac};
use serde_json::{json, Map, Value};
use sha2::Sha256;
use std::collections::BTreeMap;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

type HmacSha256 = Hmac<Sha256>;

pub struct Audit {
    path: Option<PathBuf>,
    key: Option<Vec<u8>>,        // WARDEN_RECEIPT_KEY — shared with the orchestrator
    chain: Mutex<(u64, String)>, // (next seq, previous receipt hex); genesis = (0, "")
}

fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

/// Canonical, key-sorted JSON of a record excluding its own `receipt` field.
fn canonical(obj: &Map<String, Value>) -> String {
    let sorted: BTreeMap<&str, &Value> = obj
        .iter()
        .filter(|(k, _)| k.as_str() != "receipt")
        .map(|(k, v)| (k.as_str(), v))
        .collect();
    serde_json::to_string(&sorted).unwrap_or_default()
}

/// receipt_n = HMAC(key, prev || "\n" || canonical_record).
fn sign(key: &[u8], prev: &str, canon: &str) -> String {
    let mut mac = HmacSha256::new_from_slice(key).expect("hmac accepts any key length");
    mac.update(prev.as_bytes());
    mac.update(b"\n");
    mac.update(canon.as_bytes());
    hex(&mac.finalize().into_bytes())
}

/// Known-answer test vector for the receipt construction.
///
/// The chain is verified INDEPENDENTLY by the orchestrator (Python `hmac`/`hashlib` in
/// receipts.py), so the two implementations have to agree byte for byte, forever — an
/// audit already on disk must stay verifiable after any crate upgrade. A hmac/sha2 major
/// bump compiles cleanly whether or not it changed the construction, so the format is
/// pinned here by value rather than left to trust. Regenerate ONLY if the documented
/// receipt formula changes, and change receipts.py in the same commit.
#[cfg(test)]
pub(crate) const KAT_KEY: &[u8] = b"terrarium-test-key";
#[cfg(test)]
pub(crate) const KAT_PREV: &str = "";
#[cfg(test)]
pub(crate) const KAT_CANON: &str = r#"{"decision":"allow","host":"api.anthropic.com","seq":0}"#;

/// The last appended line's (next_seq, receipt) so a restart continues the chain.
fn resume_chain(path: &PathBuf) -> Option<(u64, String)> {
    let txt = std::fs::read_to_string(path).ok()?;
    let last = txt.lines().rev().find(|l| !l.trim().is_empty())?;
    let v: Value = serde_json::from_str(last).ok()?;
    let seq = v.get("seq")?.as_u64()?;
    let receipt = v.get("receipt")?.as_str()?.to_string();
    Some((seq + 1, receipt))
}

impl Audit {
    pub fn new(path: Option<PathBuf>) -> Self {
        let key = std::env::var("WARDEN_RECEIPT_KEY").ok().filter(|s| !s.is_empty()).map(|s| s.into_bytes());
        // L1: no key ⇒ audit lines are written WITHOUT a seq/receipt, so the chain is not
        // tamper-evident. Both runners always set one; if it's missing, say so loudly rather
        // than silently degrade to unsigned audit.
        if key.is_none() && path.is_some() {
            eprintln!("[warden] WARDEN_RECEIPT_KEY unset — audit receipts are UNSIGNED (not tamper-evident)");
        }
        // Resume the chain from the last line if the audit file already exists, so a
        // Warden restart mid-session doesn't reset (seq, prev) to genesis and break
        // verification while still appending to the same file.
        let chain = path.as_ref().and_then(resume_chain).unwrap_or((0, String::new()));
        Audit { path, key, chain: Mutex::new(chain) }
    }

    pub fn log(&self, mut fields: Value) {
        let mut obj = Map::new();
        obj.insert("ts".into(), json!(chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)));
        obj.insert("kind".into(), json!("egress"));
        if let Value::Object(m) = std::mem::take(&mut fields) {
            for (k, v) in m {
                obj.insert(k, v);
            }
        }
        // Hold the chain lock across BOTH the receipt increment AND the file append below, so the
        // physical line order always matches seq order. If two concurrent log() calls signed in
        // order (seq N, N+1) but then appended out of order, a restart's resume_chain() — which
        // reads the LAST physical line — would resume from a stale (seq, receipt) and FORK the
        // chain (duplicate seqs, verification breaks on a legitimate file). Serializing the append
        // here is fine: audit is nowhere near hot enough for the lock hold to matter.
        // Recover from poison so a panic elsewhere can't wedge the audit chain.
        let _chain = if let Some(key) = &self.key {
            let mut st = self.chain.lock().unwrap_or_else(|e| e.into_inner());
            obj.insert("seq".into(), json!(st.0)); // monotonic → gaps reveal deletion/truncation
            let receipt = sign(key, &st.1, &canonical(&obj));
            obj.insert("receipt".into(), json!(receipt.clone()));
            st.0 += 1;
            st.1 = receipt;
            Some(st) // held until end of function (past the append)
        } else {
            None
        };
        let line = Value::Object(obj).to_string();
        println!("{}", line); // captured by docker logs (off-host copy)
        if let Some(p) = &self.path {
            // This signed stream must remain complete from genesis. Rotation here used
            // to strand an unverifiable `.1` prefix and made the k8s byte-offset drain
            // skip records. Capacity/retention belongs to the orchestrator, where a
            // complete stream can be archived or deleted as one evidence object.
            if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(p) {
                let _ = writeln!(f, "{}", line);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn receipt_chain_binds_full_record_and_order() {
        let key = b"test-key";
        let mut r0 = Map::new();
        r0.insert("seq".into(), json!(0));
        r0.insert("decision".into(), json!("deny"));
        r0.insert("host".into(), json!("evil.example"));
        let c0 = canonical(&r0);
        let rec0 = sign(key, "", &c0);

        // line 1 chains off line 0's receipt
        let mut r1 = Map::new();
        r1.insert("seq".into(), json!(1));
        r1.insert("decision".into(), json!("allow"));
        r1.insert("host".into(), json!("api.anthropic.com"));
        let rec1 = sign(key, &rec0, &canonical(&r1));

        // tampering with ANY field (not just ts/decision/host) breaks the receipt
        let mut tampered = r0.clone();
        tampered.insert("host".into(), json!("nice.example"));
        assert_ne!(sign(key, "", &canonical(&tampered)), rec0);

        // reordering/deleting breaks the chain: recomputing line 1 against genesis
        // (as if line 0 were deleted) yields a different receipt
        assert_ne!(sign(key, "", &canonical(&r1)), rec1);

        // the receipt field itself is excluded from canonicalization (idempotent)
        let mut with_receipt = r0.clone();
        with_receipt.insert("receipt".into(), json!(rec0));
        assert_eq!(canonical(&with_receipt), c0);
    }

    #[test]
    fn resume_chain_continues_from_last_line() {
        // A Warden restart mid-session must continue (seq, prev) from the last line,
        // not reset to genesis while appending to the same file (which would break
        // verification at the restart boundary).
        let dir = std::env::temp_dir().join(format!("warden-resume-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("audit.jsonl");
        std::fs::write(&path,
            "{\"seq\":0,\"decision\":\"allow\",\"receipt\":\"aa\"}\n\
             {\"seq\":1,\"decision\":\"deny\",\"receipt\":\"bb\"}\n").unwrap();
        assert_eq!(resume_chain(&path), Some((2, "bb".to_string())));
        // missing / empty file → genesis
        assert_eq!(resume_chain(&dir.join("nope.jsonl")), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn concurrent_log_appends_in_seq_order() {
        // Physical line order must match seq order even under concurrent logging — the chain lock
        // is held across the append, so resume_chain() (last physical line) can never resume from
        // a stale seq and fork the chain. Deterministic with the fix; racy 400-line contention
        // would surface an out-of-order line if the lock were released before the append.
        use std::sync::Arc;
        let dir = std::env::temp_dir().join(format!("warden-audit-conc-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("audit.jsonl");
        let audit = Arc::new(Audit { path: Some(path.clone()), key: Some(b"k".to_vec()), chain: Mutex::new((0, String::new())) });
        let mut handles = Vec::new();
        for _ in 0..8 {
            let a = audit.clone();
            handles.push(std::thread::spawn(move || {
                for _ in 0..50 {
                    a.log(json!({"decision": "allow", "host": "h"}));
                }
            }));
        }
        for h in handles { h.join().unwrap(); }
        let txt = std::fs::read_to_string(&path).unwrap();
        let seqs: Vec<u64> = txt.lines().filter(|l| !l.trim().is_empty())
            .map(|l| serde_json::from_str::<Value>(l).unwrap()["seq"].as_u64().unwrap())
            .collect();
        assert_eq!(seqs.len(), 400);
        for (i, s) in seqs.iter().enumerate() {
            assert_eq!(*s, i as u64, "audit line {i} out of seq order — chain-fork risk");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn cross_language_golden_vector_matches_python_verifier() {
        // Pins byte-for-byte compatibility with orchestrator/receipts.py so
        // `terra verify-egress` recomputes the SAME chain. If canonicalization or
        // the HMAC key encoding ever diverges, this and the Python test both fail.
        let key = b"abc123"; // WARDEN_RECEIPT_KEY string bytes (Warden uses s.into_bytes())
        let mut r0 = Map::new();
        r0.insert("decision".into(), json!("deny"));
        r0.insert("host".into(), json!("evil.example"));
        r0.insert("kind".into(), json!("egress"));
        r0.insert("seq".into(), json!(0));
        r0.insert("ts".into(), json!("2026-06-23T00:00:00.000Z"));
        assert_eq!(
            canonical(&r0),
            r#"{"decision":"deny","host":"evil.example","kind":"egress","seq":0,"ts":"2026-06-23T00:00:00.000Z"}"#
        );
        let rec0 = sign(key, "", &canonical(&r0));
        assert_eq!(rec0, "52287111acb3b1f2c76bcdb6a7cb6b5d1470d418b8c5a1a30d59dd880cd9b08e");

        let mut r1 = Map::new();
        r1.insert("decision".into(), json!("allow"));
        r1.insert("host".into(), json!("api.anthropic.com"));
        r1.insert("kind".into(), json!("egress"));
        r1.insert("seq".into(), json!(1));
        r1.insert("ts".into(), json!("2026-06-23T00:00:01.000Z"));
        let rec1 = sign(key, &rec0, &canonical(&r1));
        assert_eq!(rec1, "95e8f3a9a37d666861f6473e8ecb4cb4234e96a8e4bf699cc7d1c493e8f707c4");
    }

    /// The receipt formula is a cross-language contract with the orchestrator's Python
    /// verifier. This value was produced by that verifier (hmac/hashlib), so a mismatch here
    /// means Rust and Python have diverged and every audit on disk just became unverifiable.
    #[test]
    fn receipt_matches_the_python_verifier() {
        let got = sign(KAT_KEY, KAT_PREV, KAT_CANON);
        assert_eq!(got, "4eba225bcd2509d1ac9fdb61ccbf79f58ed9910571af348745ef082c6b9dfa5b", "receipt construction changed — audits on disk are now unverifiable");
    }
}
