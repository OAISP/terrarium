//! Per-session ephemeral CA + on-demand leaf minting for the MITM server side.
//! The sandbox trusts ONLY this CA (NODE_EXTRA_CA_CERTS → session-ca.pem); a leak
//! can MITM only this one already-dead session.
use anyhow::{anyhow, Result};
use rcgen::{BasicConstraints, CertificateParams, DnType, ExtendedKeyUsagePurpose,
            IsCa, Issuer, KeyPair, KeyUsagePurpose, SerialNumber};
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use rustls::sign::CertifiedKey;
use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};
use time::{Duration, OffsetDateTime};

const LEAF_CACHE_MAX: usize = 256; // far above any legit session's host count

/// Concealment: the intercepting CA wears a believable enterprise TLS-inspection
/// identity (default: Zscaler, the most ubiquitous corporate web-proxy) instead of a
/// generic self-signed root that reads as an ad-hoc MITM. Each field is operator
/// overridable so a deployment can mirror whatever proxy its environment actually runs.
fn ca_subject() -> (String, String, String) {
    let cn = std::env::var("WARDEN_CA_CN").unwrap_or_else(|_| "Zscaler Root CA".to_string());
    let org = std::env::var("WARDEN_CA_ORG").unwrap_or_else(|_| "Zscaler Inc.".to_string());
    let c = std::env::var("WARDEN_CA_C").unwrap_or_else(|_| "US".to_string());
    (cn, org, c)
}

/// A non-trivial, realistic-looking serial (real CAs never issue serial 1). Derived
/// from the CA key bytes so it's stable for this session but unique across sessions.
fn realistic_serial(seed: &[u8]) -> SerialNumber {
    let mut bytes = [0u8; 16];
    for (i, b) in seed.iter().enumerate().take(64) {
        bytes[i % 16] ^= *b;
    }
    bytes[0] |= 0x40; // keep it positive and 16 bytes wide, like a real issuer serial
    SerialNumber::from(bytes.to_vec())
}

pub struct SessionCa {
    pub cert_pem: String,
    /// The emitted CA cert, kept as DER for the leaf chain's second element.
    ca_der: CertificateDer<'static>,
    /// rcgen 0.14 signs against an `Issuer` (CA distinguished name + key-id method + key)
    /// rather than taking the CA cert and key as two separate arguments. It owns the SAME
    /// `CertificateParams` that produced `ca_der` just above, so the issuer DN and key
    /// identifier match the certificate the sandbox trusts by construction — no reparsing,
    /// and no second params struct that could drift from the emitted cert.
    issuer: Issuer<'static, KeyPair>,
    cache: Mutex<HashMap<String, Arc<CertifiedKey>>>,
}

impl SessionCa {
    pub fn generate() -> Result<Self> {
        let ca_key = KeyPair::generate()?;
        let mut params = CertificateParams::new(Vec::<String>::new())?;
        params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign,
                                 KeyUsagePurpose::DigitalSignature];
        // Believable enterprise-proxy issuer identity (deception layer).
        let (cn, org, c) = ca_subject();
        params.distinguished_name.push(DnType::CommonName, cn);
        params.distinguished_name.push(DnType::OrganizationName, org);
        params.distinguished_name.push(DnType::CountryName, c);
        // Backdate the root so it doesn't read as "minted seconds ago" (a real corporate
        // root is years old). notBefore ~2y back, notAfter ~3y out — a plausible window.
        let now = OffsetDateTime::now_utc();
        params.not_before = now - Duration::days(730);
        params.not_after = now + Duration::days(365 * 3);
        params.serial_number = Some(realistic_serial(&ca_key.serialize_der()));
        // self_signed BORROWS params, so the very same params can then move into the Issuer.
        let ca_cert = params.self_signed(&ca_key)?;
        let cert_pem = ca_cert.pem();
        let ca_der = ca_cert.der().clone();
        let issuer = Issuer::new(params, ca_key);
        Ok(SessionCa { cert_pem, ca_der, issuer, cache: Mutex::new(HashMap::new()) })
    }

    pub fn write_public(&self, dir: &Path) -> Result<()> {
        std::fs::create_dir_all(dir)?;
        std::fs::write(dir.join("session-ca.pem"), self.cert_pem.as_bytes())?;
        Ok(())
    }

    /// Mint (and cache) a leaf for `host`, signed by the session CA, as a rustls
    /// CertifiedKey for the MITM server side.
    pub fn leaf_for(&self, host: &str) -> Result<Arc<CertifiedKey>> {
        {
            // Recover from poison (match the policy/audit hot paths): a panic while another
            // thread held this lock must not brick leaf minting for every future host.
            let mut cache = self.cache.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(ck) = cache.get(host) {
                return Ok(ck.clone());
            }
            // Bound the cache: a legit session touches a handful of hosts, but a
            // worker-controlled SNI can vary per connection. Clear on overflow so an
            // adversarial flood can't grow keypairs/certs unbounded in Warden's RAM.
            if cache.len() >= LEAF_CACHE_MAX {
                cache.clear();
            }
        }
        let leaf_key = KeyPair::generate()?;
        let mut params = CertificateParams::new(vec![host.to_string()])?;
        params.distinguished_name.push(DnType::CommonName, host);
        params.use_authority_key_identifier_extension = true;     // AKI → strict verifiers (Python ssl)
        params.key_usages = vec![KeyUsagePurpose::DigitalSignature, KeyUsagePurpose::KeyEncipherment];
        params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        // A corporate proxy re-issues leaves on demand, so a recent notBefore is normal —
        // but back it off a few days (not the exact session-start second) and give it the
        // ~90-day lifetime such proxies typically use, plus a realistic serial.
        let now = OffsetDateTime::now_utc();
        params.not_before = now - Duration::days(3);
        params.not_after = now + Duration::days(90);
        params.serial_number = Some(realistic_serial(&leaf_key.serialize_der()));
        let leaf = params.signed_by(&leaf_key, &self.issuer)?;
        let chain = vec![leaf.der().clone(), self.ca_der.clone()];
        let key_der = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(leaf_key.serialize_der()));
        let signing_key = rustls::crypto::aws_lc_rs::sign::any_supported_type(&key_der)
            .map_err(|e| anyhow!("signing key: {e}"))?;
        let ck = Arc::new(CertifiedKey::new(chain, signing_key));
        self.cache.lock().unwrap_or_else(|e| e.into_inner()).insert(host.to_string(), ck.clone());
        Ok(ck)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ca_subject_defaults_to_a_believable_proxy_identity() {
        // Concealment: the interception CA wears an enterprise-proxy identity, not a
        // generic/ad-hoc self-signed name. (Env overrides aren't set in the test env.)
        let (cn, org, c) = ca_subject();
        assert_eq!(cn, "Zscaler Root CA");
        assert_eq!(org, "Zscaler Inc.");
        assert_eq!(c, "US");
    }

    #[test]
    fn generates_ca_and_mints_a_leaf() {
        // Exercises the backdated-validity + realistic-serial paths end to end.
        let ca = SessionCa::generate().expect("ca");
        assert!(ca.cert_pem.contains("BEGIN CERTIFICATE"));
        let a = ca.leaf_for("api.anthropic.com").expect("leaf");
        let b = ca.leaf_for("api.anthropic.com").expect("cached leaf");
        assert!(Arc::ptr_eq(&a, &b)); // cached, not re-minted per call
    }

    #[test]
    fn minted_leaf_actually_chains_to_the_session_ca() {
        // The pre-existing leaf test only proved a leaf came back and was cached — it would
        // have passed just as happily if the leaf were signed by the wrong key or carried a
        // mismatched issuer DN. This verifies the chain the way the sandbox's TLS stack will:
        // the session CA as the ONLY trust anchor, through the same aws-lc-rs provider Warden
        // serves with. That covers issuer/subject linkage, the signature, the backdated
        // validity window, the SAN, and serverAuth EKU in one assertion — the properties the
        // rcgen 0.14 `Issuer` refactor could plausibly have broken.
        use rustls::client::verify_server_cert_signed_by_trust_anchor;
        use rustls::pki_types::{ServerName, UnixTime};
        use rustls::server::ParsedCertificate;

        let ca = SessionCa::generate().expect("ca");
        let ck = ca.leaf_for("api.anthropic.com").expect("leaf");

        let mut roots = rustls::RootCertStore::empty();
        roots.add(ca.ca_der.clone()).expect("trust the session ca");
        let parsed = ParsedCertificate::try_from(&ck.cert[0]).expect("parse leaf");
        verify_server_cert_signed_by_trust_anchor(
            &parsed,
            &roots,
            &ck.cert[1..],                       // the CA, as sent on the wire
            UnixTime::now(),
            rustls::crypto::aws_lc_rs::default_provider()
                .signature_verification_algorithms
                .all,
        )
        .expect("leaf must chain to the session CA");

        // …and it must be issued FOR the requested host, not just be well-formed.
        let name = ServerName::try_from("api.anthropic.com").expect("name");
        rustls::client::verify_server_name(&parsed, &name).expect("SAN must cover the host");

        // The wire chain is exactly [leaf, ca] — a client trusting only the session CA needs
        // the CA present, and anything extra would be a leak of unrelated cert material.
        assert_eq!(ck.cert.len(), 2);
        assert_eq!(ck.cert[1], ca.ca_der);

        // Negative control, so the assertions above can't pass vacuously: a leaf minted by a
        // DIFFERENT session's CA must be rejected by this one. This is also the isolation
        // property the per-session CA exists for — one leaked CA can only MITM its own session.
        let other = SessionCa::generate().expect("second ca");
        let foreign = other.leaf_for("api.anthropic.com").expect("foreign leaf");
        let foreign_parsed = ParsedCertificate::try_from(&foreign.cert[0]).expect("parse");
        assert!(
            verify_server_cert_signed_by_trust_anchor(
                &foreign_parsed,
                &roots,
                &foreign.cert[1..],
                UnixTime::now(),
                rustls::crypto::aws_lc_rs::default_provider()
                    .signature_verification_algorithms
                    .all,
            )
            .is_err(),
            "another session's CA must not be able to mint a leaf this session trusts"
        );
    }

    #[test]
    fn serials_are_wide_and_nontrivial() {
        let s1 = realistic_serial(b"aaaaaaaaaaaaaaaa");
        let s2 = realistic_serial(b"bbbbbbbbbbbbbbbb");
        assert_ne!(s1.to_bytes(), s2.to_bytes());
        assert_eq!(s1.to_bytes().len(), 16); // not a trivial serial=1
    }
}
