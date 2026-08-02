//! MITM TLS plumbing: present a minted leaf to the worker (server side), and
//! re-originate to the real upstream with verification ON (client side).
use crate::ca::SessionCa;
use anyhow::{anyhow, Result};
use rustls::pki_types::ServerName;
use rustls::server::{ClientHello, ResolvesServerCert};
use rustls::sign::CertifiedKey;
use rustls::{ClientConfig, RootCertStore, ServerConfig};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::net::TcpStream;
use tokio::time::timeout;
use tokio_rustls::{client::TlsStream as ClientTls, TlsAcceptor, TlsConnector};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(15);

struct Resolver {
    ca: Arc<SessionCa>,
}
impl std::fmt::Debug for Resolver {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("WardenResolver")
    }
}
impl ResolvesServerCert for Resolver {
    fn resolve(&self, hello: ClientHello<'_>) -> Option<Arc<CertifiedKey>> {
        let name = hello.server_name()?.to_string();
        self.ca.leaf_for(&name).ok()
    }
}

/// Agent-facing MITM server config. Concealment (F7): advertise ALPN `http/1.1`
/// EXPLICITLY rather than leaving the extension absent. Warden terminates HTTP/1.1
/// only, and a real corporate TLS-inspection proxy that bumps a host commonly forces
/// http/1.1 too — so an inspected host negotiating http/1.1 reads as a normal
/// inspection proxy, whereas an *absent* ALPN extension is the more anomalous tell.
/// Residual (documented, not chased): an allow-listed *tunnel* host is an opaque TCP
/// relay, so it still negotiates h2 directly with its real upstream — a prober that
/// compares ALPN across an inspected vs a tunneled host can still infer selective
/// MITM. Closing that fully needs an h2 MITM terminator, a large change to the
/// credential-injection core we deliberately don't take on here.
pub fn server_config(ca: Arc<SessionCa>) -> ServerConfig {
    let mut cfg = ServerConfig::builder()
        .with_no_client_auth()
        .with_cert_resolver(Arc::new(Resolver { ca }));
    cfg.alpn_protocols = vec![b"http/1.1".to_vec()];
    cfg
}

pub fn server_acceptor(ca: Arc<SessionCa>) -> TlsAcceptor {
    TlsAcceptor::from(Arc::new(server_config(ca)))
}

pub fn upstream_connector() -> TlsConnector {
    let mut roots = RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    let cfg = ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    TlsConnector::from(Arc::new(cfg))
}

/// Connect to a PRE-RESOLVED, SSRF-validated address and TLS-handshake using
/// ``servername`` (the allow-listed host) for cert verification. Taking the
/// address (not the hostname) guarantees we dial exactly the IP the SSRF guard
/// approved — no second, unchecked DNS lookup. Both phases are bounded so a
/// slow/blackholed upstream can't pin the connection task forever.
pub async fn connect_upstream(
    connector: &TlsConnector, addr: SocketAddr, servername: &str,
) -> Result<ClientTls<TcpStream>> {
    let tcp = timeout(CONNECT_TIMEOUT, TcpStream::connect(addr))
        .await
        .map_err(|_| anyhow!("upstream connect timed out"))??;
    let name = ServerName::try_from(servername.to_string())
        .map_err(|_| anyhow!("bad servername {servername}"))?;
    Ok(timeout(HANDSHAKE_TIMEOUT, connector.connect(name, tcp))
        .await
        .map_err(|_| anyhow!("upstream TLS handshake timed out"))??)
}

/// Read an HTTP message head up to and including the blank line, WITHOUT consuming
/// body bytes — so the head can be inspected/modified before forwarding.
pub async fn read_head<R: AsyncRead + Unpin>(r: &mut R) -> Result<Vec<u8>> {
    let mut buf = Vec::with_capacity(1024);
    let mut b = [0u8; 1];
    loop {
        if r.read(&mut b).await? == 0 {
            break;
        }
        buf.push(b[0]);
        if buf.len() >= 4 && &buf[buf.len() - 4..] == b"\r\n\r\n" {
            break;
        }
        if buf.len() > 64 * 1024 {
            return Err(anyhow!("request head too large"));
        }
    }
    Ok(buf)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ca::SessionCa;

    #[test]
    fn agent_facing_alpn_advertises_http11_explicitly() {
        // Concealment (F7): an explicit http/1.1 ALPN reads as a normal http/1.1
        // TLS-inspection proxy; an ABSENT ALPN extension is the more anomalous tell.
        let ca = Arc::new(SessionCa::generate().unwrap());
        let cfg = server_config(ca);
        assert_eq!(cfg.alpn_protocols, vec![b"http/1.1".to_vec()]);
    }
}
