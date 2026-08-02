//! CONNECT forward proxy. Sandbox reaches the internet ONLY via this loopback
//! proxy (HTTPS_PROXY + a policy-drop firewall). Deny → 403; Tunnel → opaque
//! relay (Warden sees only ciphertext); Mitm → TLS-terminate with a minted leaf,
//! read the plaintext head (Phase 3 injects here, Phase 4 scans), re-originate to
//! the real upstream with verification ON.
use crate::audit::Audit;
use crate::ca::SessionCa;
use crate::inject::{rewrite_head, strip_auth_for_scan, Injector};
use crate::{http, scan};
use crate::policy::{Decision, HostDecision, Policy};
use crate::tls::{connect_upstream, read_head, server_acceptor, upstream_connector};
use serde_json::json;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Semaphore;
use tokio_rustls::{TlsAcceptor, TlsConnector};

/// Max concurrent client connections. The sandbox can open unlimited CONNECTs;
/// without a cap each holds two TLS sessions + buffers → memory/FD exhaustion.
const MAX_CONNS_DEFAULT: usize = 256;
/// Worker-facing TLS handshake bound (slowloris on the accept side).
const ACCEPT_TIMEOUT: Duration = Duration::from_secs(15);
/// Plaintext CONNECT preamble (request line + headers) bound. Without it a client that grabs a
/// semaphore permit then withholds/dribbles bytes pins the permit forever — MAX_CONNS such
/// connections halt all egress. Time is bounded here; size by the two caps below.
const PREAMBLE_TIMEOUT: Duration = Duration::from_secs(15);
const MAX_PREAMBLE_BYTES: usize = 64 * 1024;
const MAX_PREAMBLE_LINES: usize = 200;

/// Parse a CONNECT target into (host, port), correctly handling IPv6 literals.
/// `[::1]:443`→("::1",443) · `[::1]`→("::1",443) · `h:443`→("h",443) ·
/// bare `::1`→("::1",443) · `h`→("h",443). The returned host is what BOTH the
/// policy decision and the upstream dial use — they must never diverge.
fn split_host_port(target: &str) -> (String, u16) {
    if let Some(rest) = target.strip_prefix('[') {
        if let Some((h, after)) = rest.split_once(']') {
            let port = after.strip_prefix(':').and_then(|p| p.parse().ok()).unwrap_or(443);
            return (h.to_string(), port);
        }
        return (rest.to_string(), 443);
    }
    match target.rsplit_once(':') {
        // a single trailing ":port" only — a bare IPv6 literal has many colons,
        // so treat the whole string as the host and default the port.
        Some((h, p)) if !h.contains(':') => (h.to_string(), p.parse().unwrap_or(0)),
        _ => (target.to_string(), 443),
    }
}

/// Resolve `host:port` to the address we will dial. No filtering here anymore — the
/// destination policy (private-range floor, metadata hard-deny, IP/CIDR allow) lives in
/// `Policy::decide_ip`, which is evaluated against the address returned here and dialed
/// EXACTLY (rebinding-safe: decide + dial can't diverge). Returns the first address.
async fn resolve(host: &str, port: u16) -> anyhow::Result<SocketAddr> {
    tokio::net::lookup_host((host, port)).await?
        .next()
        .ok_or_else(|| anyhow::anyhow!("no addresses for {host}"))
}

/// True if the error is a benign peer disconnect (connection reset / aborted /
/// broken pipe / unexpected EOF) — normal socket lifecycle, not a mediation
/// failure. Checks the io::ErrorKind in the source chain, with a string fallback
/// for io errors that arrive already rendered.
fn is_benign_disconnect(e: &anyhow::Error) -> bool {
    use std::io::ErrorKind::{BrokenPipe, ConnectionAborted, ConnectionReset, UnexpectedEof};
    if let Some(io) = e.chain().find_map(|c| c.downcast_ref::<std::io::Error>()) {
        if matches!(io.kind(), ConnectionReset | ConnectionAborted | BrokenPipe | UnexpectedEof) {
            return true;
        }
    }
    let m = e.to_string().to_lowercase();
    m.contains("reset by peer") || m.contains("broken pipe")
        || m.contains("connection reset") || m.contains("unexpected end of file")
}

pub async fn run(listen: &str, policy: Arc<Mutex<Policy>>, audit: Arc<Audit>, ca: Arc<SessionCa>,
                 injector: Arc<Injector>) -> anyhow::Result<()> {
    let acceptor = server_acceptor(ca);
    let connector = Arc::new(upstream_connector());
    let listener = TcpListener::bind(listen).await?;
    let max_conns = std::env::var("WARDEN_MAX_CONNS").ok()
        .and_then(|v| v.parse().ok())
        .filter(|n| *n > 0)
        .unwrap_or(MAX_CONNS_DEFAULT);
    let sem = Arc::new(Semaphore::new(max_conns));
    loop {
        // Acquire before accept so we apply backpressure at the listener instead
        // of spawning unbounded tasks (a connection-flood DoS of the proxy).
        let permit = match sem.clone().acquire_owned().await {
            Ok(p) => p,
            Err(_) => return Ok(()),
        };
        let (client, _peer) = match listener.accept().await { Ok(v) => v, Err(_) => continue };
        let (policy, audit, acceptor, connector, injector) =
            (policy.clone(), audit.clone(), acceptor.clone(), connector.clone(), injector.clone());
        tokio::spawn(async move {
            let _permit = permit; // released when this connection finishes
            let _ = handle(client, policy, audit, acceptor, connector, injector).await;
        });
    }
}

async fn handle(client: TcpStream, policy: Arc<Mutex<Policy>>, audit: Arc<Audit>,
                acceptor: TlsAcceptor, connector: Arc<TlsConnector>, injector: Arc<Injector>) -> anyhow::Result<()> {
    let mut reader = BufReader::new(client);
    // Read the CONNECT line + drain its headers, bounded in TIME (a client that grabs a permit
    // then withholds/dribbles bytes must not pin it) and SIZE (a running byte budget + line cap,
    // so a newline-less flood can't grow unboundedly within the timeout window).
    let mut line = String::new();
    let preamble = tokio::time::timeout(PREAMBLE_TIMEOUT, async {
        if reader.read_line(&mut line).await? == 0 {
            return Ok::<Option<String>, anyhow::Error>(None); // empty / EOF
        }
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() < 2 || !parts[0].eq_ignore_ascii_case("CONNECT") {
            anyhow::bail!("not a CONNECT request");
        }
        let target = parts[1].to_string();
        let mut budget = line.len();
        for _ in 0..MAX_PREAMBLE_LINES {
            let mut h = String::new();
            let n = reader.read_line(&mut h).await?;
            if n == 0 || h == "\r\n" || h == "\n" {
                return Ok(Some(target));
            }
            budget += n;
            if budget > MAX_PREAMBLE_BYTES {
                anyhow::bail!("preamble too large");
            }
        }
        anyhow::bail!("too many header lines")
    })
    .await;
    let target = match preamble {
        Ok(Ok(Some(t))) => t,
        Ok(Ok(None)) => return Ok(()), // empty first line / EOF — nothing to serve
        Ok(Err(_)) => {
            // malformed / non-CONNECT / oversized preamble
            let mut s = reader.into_inner();
            let _ = s.write_all(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n").await;
            audit.log(json!({"decision": "deny-method"}));
            return Ok(());
        }
        Err(_) => {
            // timed out: withheld/dribbled preamble (slowloris) — free the permit
            let mut s = reader.into_inner();
            let _ = s.write_all(b"HTTP/1.1 408 Request Timeout\r\n\r\n").await;
            audit.log(json!({"decision": "deny", "reason": "preamble-timeout"}));
            return Ok(());
        }
    };
    let (host, port) = split_host_port(&target);
    // Phase 1: host-level decision (no DNS). Recover from a poisoned lock instead of panicking
    // — one panic holding this lock would otherwise brick ALL future egress mediation.
    let (host_dec, override_ip) = {
        let mut p = policy.lock().unwrap_or_else(|e| e.into_inner());
        p.reload();
        (p.decide_host(&host, port), p.host_override(&host))
    };
    let mut client = reader.into_inner();
    // A host-level Deny short-circuits WITHOUT resolving (no DNS query for a denied host).
    if let HostDecision::Resolved(Decision::Deny(reason)) = &host_dec {
        let _ = client.write_all(b"HTTP/1.1 403 Forbidden\r\n\r\n").await;
        audit.log(json!({"decision": "deny", "host": host, "reason": reason}));
        return Ok(());
    }
    // Everything else needs the address we'll dial — to apply the IP/CIDR + private-range
    // floor, and to stay rebinding-safe (decide + dial use the SAME resolved address). A policy
    // host-override wins over DNS: an internal name resolves to its configured private address
    // (the sandbox's own resolver can't see the internal DNS).
    let addr = match override_ip {
        Some(ip) => std::net::SocketAddr::new(ip, port),
        None => match resolve(&host, port).await {
            Ok(a) => a,
            Err(e) => {
                let _ = client.write_all(b"HTTP/1.1 502 Bad Gateway\r\n\r\n").await;
                audit.log(json!({"decision": "resolve-error", "host": host, "error": e.to_string()}));
                return Ok(());
            }
        },
    };
    let decision = match host_dec {
        HostDecision::Resolved(d) => d, // Anthropic Mitm, or an IP-literal (already IP-aware)
        HostDecision::NeedsIp => {
            let mut p = policy.lock().unwrap_or_else(|e| e.into_inner());
            p.reload(); // re-read in case a kill/policy edit landed during the DNS window
            p.decide_ip(&host, port, addr.ip())
        }
    };
    match decision {
        Decision::Deny(reason) => {
            let _ = client.write_all(b"HTTP/1.1 403 Forbidden\r\n\r\n").await;
            audit.log(json!({"decision": "deny", "host": host, "reason": reason, "ip": addr.ip().to_string()}));
        }
        Decision::Mitm => {
            if client.write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n").await.is_err() {
                return Ok(());
            }
            if let Err(e) = mitm(client, &host, port, addr, acceptor, connector, &injector, &audit, policy).await {
                // A peer closing the socket (the CLI's connection pool evicting an idle
                // keep-alive, or either side ending a finished SSE stream) surfaces as
                // ECONNRESET/EPIPE/EOF — that's NORMAL connection lifecycle, not a
                // mediation failure. Log it as a benign "closed" so it doesn't masquerade
                // as alarming "mitm-error" noise (real failures — TLS/cert/timeout/protocol
                // — still log as mitm-error with the message).
                if is_benign_disconnect(&e) {
                    audit.log(json!({"decision": "closed", "host": host}));
                } else {
                    audit.log(json!({"decision": "mitm-error", "host": host, "error": e.to_string()}));
                }
            }
        }
        Decision::Tunnel => {
            // Dial EXACTLY the evaluated address (not a fresh lookup) so a multi-record /
            // rebinding host can't slip a different, unevaluated IP past the decision.
            let mut upstream = match TcpStream::connect(addr).await {
                Ok(u) => u,
                Err(e) => {
                    let _ = client.write_all(b"HTTP/1.1 502 Bad Gateway\r\n\r\n").await;
                    audit.log(json!({"decision": "upstream-error", "host": host, "error": e.to_string()}));
                    return Ok(());
                }
            };
            client.write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n").await?;
            // record the resolved IP — the audit-visible trust-boundary crossing for an internal allow
            audit.log(json!({"decision": "allow", "host": host, "port": port, "ip": addr.ip().to_string()}));
            let _ = tokio::io::copy_bidirectional(&mut client, &mut upstream).await;
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)] // per-session context (acceptor, connector,
// injector, audit, policy) travels together; bundling it into a struct would refactor the MITM
// hot path, which does not belong in a dependency bump.
async fn mitm(client: TcpStream, host: &str, port: u16, addr: SocketAddr, acceptor: TlsAcceptor,
              connector: Arc<TlsConnector>, injector: &Injector, audit: &Audit,
              policy: Arc<Mutex<Policy>>) -> anyhow::Result<()> {
    const MAX_BODY: usize = 16 << 20; // 16 MiB per request (reject larger, don't truncate)
    // `addr` is the address the caller already decided on (policy-checked, rebinding-safe) —
    // we dial exactly it, and re-check policy mid-tunnel against its IP.
    let ip = addr.ip();
    let mut wtls = tokio::time::timeout(ACCEPT_TIMEOUT, acceptor.accept(client)).await
        .map_err(|_| anyhow::anyhow!("client TLS handshake timed out"))??; // worker-facing TLS (our leaf)
    // The leaf was minted for the inner-TLS SNI, but the policy decision, credential
    // selection, and the upstream dial all key off the CONNECT host — they MUST agree.
    // A request whose SNI diverges from its CONNECT target is misdirected (and the
    // precondition for any future SNI/Host-keyed credential misdirection); reject it
    // before touching the upstream.
    let sni = wtls.get_ref().1.server_name().map(str::to_string);
    if let Some(sni) = sni {
        if !sni.eq_ignore_ascii_case(host) {
            let _ = wtls.write_all(b"HTTP/1.1 421 Misdirected Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await;
            let _ = wtls.shutdown().await;
            audit.log(json!({"decision": "deny", "host": host, "reason": "sni-mismatch", "sni": sni}));
            return Ok(());
        }
    }
    let mut utls = connect_upstream(&connector, addr, host).await?;

    // HTTP/1.1 keep-alive loop. Policy is re-evaluated EVERY request (and on idle
    // timeout) so kill / deny / mode changes apply mid-tunnel — a held-open tunnel
    // cannot outlive a kill switch.
    loop {
        // bounded idle wait so a kill applies within ~30s even on a silent tunnel
        let head = match tokio::time::timeout(Duration::from_secs(30), read_head(&mut wtls)).await {
            Ok(Ok(h)) => h,
            Ok(Err(_)) => break,
            Err(_) => {
                // idle: apply a kill/deny that arrived while the tunnel was silent
                let killed = {
                    let mut p = policy.lock().unwrap_or_else(|e| e.into_inner());
                    p.reload();
                    matches!(p.decide_ip(host, port, ip), Decision::Deny(_))
                };
                if killed {
                    let _ = wtls.write_all(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await;
                    let _ = wtls.shutdown().await;
                    audit.log(json!({"decision": "revoked", "host": host}));
                    break;
                }
                continue;
            }
        };
        if head.is_empty() {
            break;
        }
        // re-evaluate policy for THIS request (AFTER the head is read) so a kill /
        // deny / mode flip applies to a held-open tunnel's very next request.
        let (decision, enforce) = {
            let mut p = policy.lock().unwrap_or_else(|e| e.into_inner());
            p.reload();
            (p.decide_ip(host, port, ip), p.is_enforce())
        };
        if matches!(decision, Decision::Deny(_)) {
            let _ = wtls.write_all(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await;
            let _ = wtls.shutdown().await;
            audit.log(json!({"decision": "revoked", "host": host})); // kill / deny / mode flip mid-tunnel
            break;
        }
        if !http::well_formed_head(&head) {
            let _ = wtls.write_all(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await;
            let _ = wtls.shutdown().await;
            audit.log(json!({"decision": "deny", "host": host, "reason": "malformed-head"}));
            break;
        }
        // Reject ambiguous framing (CL+TE, duplicate/non-numeric CL) BEFORE reading
        // the body: otherwise Warden and the upstream can frame the same bytes
        // differently and a smuggled second request slips past the per-request
        // allow-list / DLP re-check above.
        if !http::well_framed(&head) {
            let _ = wtls.write_all(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await;
            let _ = wtls.shutdown().await;
            audit.log(json!({"decision": "deny", "host": host, "reason": "ambiguous-framing"}));
            break;
        }
        // Credential scope is keyed by the CONNECT host. Bind the decrypted HTTP
        // authority to that same host before selecting/injecting a secret; otherwise
        // a shared CDN endpoint could be connected under an allowed host while an
        // attacker supplies a different Host or absolute-form URI.
        if !request_authority_matches(&head, host, port) {
            let _ = wtls.write_all(b"HTTP/1.1 421 Misdirected Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await;
            let _ = wtls.shutdown().await;
            audit.log(json!({"decision": "deny", "host": host, "reason": "host-mismatch"}));
            break;
        }
        // Re-read the credential per request so a mid-tunnel rotation/refresh is
        // picked up by a long-lived keep-alive tunnel (not just at tunnel open).
        injector.reload();
        let cred = injector.get(host);
        let inject_host = cred.is_some();
        let (body_raw, body_dec) = http::read_body(&mut wtls, &head, MAX_BODY).await?;
        let reqline = String::from_utf8_lossy(head.split(|&b| b == b'\r' || b == b'\n').next().unwrap_or(&[])).to_string();

        // DLP: secret-exfil. Scan the DE-CHUNKED body (no chunk-split evasion). Block
        // egress to an UNTRUSTED host (not credential-injected) in enforce; Anthropic
        // traffic is the agent's legit LLM call → scan-and-log, don't block.
        // Scan the head WITHOUT its auth headers: the (decoy/injected) credential is
        // not exfil and would otherwise self-trigger the anthropic-key pattern on
        // every call, drowning the audit (and self-blocking when the cred is absent).
        let mut hits = scan::scan_dlp(&strip_auth_for_scan(&head, cred.as_ref()));
        hits.extend(scan::scan_dlp(&body_dec));
        if !hits.is_empty() && !inject_host && enforce {
            let _ = wtls.write_all(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await;
            let _ = wtls.shutdown().await;
            audit.log(json!({"decision": "dlp-block", "host": host, "patterns": hits, "req": reqline}));
            break;
        }
        if !hits.is_empty() {
            audit.log(json!({"decision": "dlp-hit", "host": host, "patterns": hits, "blocked": false, "req": reqline}));
        }

        // inject the real credential (never logged); the sandbox only ever held a dummy
        let head_out = match &cred {
            Some(c) => rewrite_head(&head, c),
            None => head,
        };
        audit.log(json!({"decision": "mitm", "host": host, "port": port, "injected": inject_host, "req": reqline}));
        // Forward the request and stream the response back CONCURRENTLY (SSE-preserving,
        // injection-scanning). Must not be sequential: a request body larger than the
        // upstream socket send buffer would otherwise deadlock against an upstream that
        // starts responding before it has drained the request. See forward_request_response.
        // Guard the in-flight forward: an idle timeout (so a stalled upstream/worker can't
        // pin this connection's semaphore permit forever) plus a mid-stream kill check that
        // re-evaluates the LIVE policy for this host, so a kill switch flipped during a long
        // streaming response interrupts it — not just the next request.
        let guard = http::StreamGuard::new(http::stream_idle(), {
            let policy = policy.clone();
            let host = host.to_string();
            move || {
                let mut p = policy.lock().unwrap_or_else(|e| e.into_inner());
                p.reload();
                matches!(p.decide_ip(&host, port, ip), Decision::Deny(_))
            }
        });
        let (utls_back, keepalive) =
            http::forward_request_response(utls, &mut wtls, &head_out, &body_raw, host, audit, &guard).await?;
        utls = utls_back;
        if !keepalive {
            break;
        }
    }
    Ok(())
}

fn split_authority(authority: &str) -> Option<(String, Option<u16>)> {
    let authority = authority.trim();
    if authority.is_empty() || authority.contains('/') || authority.contains('@')
        || authority.contains('\r') || authority.contains('\n')
    {
        return None;
    }
    if let Some(rest) = authority.strip_prefix('[') {
        let end = rest.find(']')?;
        let host = rest[..end].to_lowercase();
        let suffix = &rest[end + 1..];
        let port = if suffix.is_empty() {
            None
        } else {
            Some(suffix.strip_prefix(':')?.parse().ok()?)
        };
        return Some((host, port));
    }
    if authority.matches(':').count() == 1 {
        let (host, raw_port) = authority.rsplit_once(':')?;
        return Some((host.to_lowercase(), Some(raw_port.parse().ok()?)));
    }
    Some((authority.to_lowercase(), None))
}

fn authority_matches(authority: &str, connect_host: &str, connect_port: u16) -> bool {
    let Some((host, port)) = split_authority(authority) else {
        return false;
    };
    if !host.eq_ignore_ascii_case(connect_host) {
        return false;
    }
    port.map_or(connect_port == 443, |p| p == connect_port)
}

fn request_authority_matches(head: &[u8], connect_host: &str, connect_port: u16) -> bool {
    let text = String::from_utf8_lossy(head);
    let mut lines = text.split("\r\n");
    let Some(request_line) = lines.next() else {
        return false;
    };
    let parts: Vec<&str> = request_line.split_whitespace().collect();
    if parts.len() != 3 {
        return false;
    }
    let hosts: Vec<&str> = lines
        .filter_map(|line| line.split_once(':'))
        .filter(|(name, _)| name.eq_ignore_ascii_case("host"))
        .map(|(_, value)| value.trim())
        .collect();
    if hosts.len() != 1 || !authority_matches(hosts[0], connect_host, connect_port) {
        return false;
    }
    let target = parts[1];
    if let Some(rest) = target.strip_prefix("https://") {
        let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
        return authority_matches(authority, connect_host, connect_port);
    }
    // Absolute cleartext-form is never valid inside this CONNECT TLS tunnel.
    !target.to_ascii_lowercase().starts_with("http://")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn benign_disconnects_are_not_mitm_errors() {
        // ECONNRESET/EPIPE/EOF from a peer closing → benign "closed", not "mitm-error".
        for kind in [std::io::ErrorKind::ConnectionReset, std::io::ErrorKind::BrokenPipe,
                     std::io::ErrorKind::UnexpectedEof, std::io::ErrorKind::ConnectionAborted] {
            let e = anyhow::Error::new(std::io::Error::new(kind, "x"));
            assert!(is_benign_disconnect(&e), "{kind:?} should be benign");
        }
        // the exact os-error-104 message seen in the live audit, wrapped with context
        let reset = anyhow::Error::new(std::io::Error::new(std::io::ErrorKind::ConnectionReset,
            "Connection reset by peer (os error 104)")).context("streaming response");
        assert!(is_benign_disconnect(&reset));
        // real failures stay errors
        assert!(!is_benign_disconnect(&anyhow::anyhow!("client TLS handshake timed out")));
        assert!(!is_benign_disconnect(&anyhow::anyhow!("request head too large")));
    }

    #[test]
    fn parses_ipv6_and_ipv4_targets() {
        assert_eq!(split_host_port("[::1]:8443"), ("::1".into(), 8443));
        assert_eq!(split_host_port("[2606:4700::1111]"), ("2606:4700::1111".into(), 443));
        assert_eq!(split_host_port("::1"), ("::1".into(), 443));        // bare IPv6 → host, default port
        assert_eq!(split_host_port("api.anthropic.com:443"), ("api.anthropic.com".into(), 443));
        assert_eq!(split_host_port("api.anthropic.com"), ("api.anthropic.com".into(), 443));
    }

    #[test]
    fn binds_http_authority_to_connect_target() {
        assert!(request_authority_matches(
            b"GET /v1 HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
            "api.anthropic.com", 443,
        ));
        assert!(request_authority_matches(
            b"GET https://api.anthropic.com/v1 HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
            "api.anthropic.com", 443,
        ));
        assert!(!request_authority_matches(
            b"GET / HTTP/1.1\r\nHost: attacker.example\r\n\r\n",
            "api.anthropic.com", 443,
        ));
        assert!(!request_authority_matches(
            b"GET https://attacker.example/ HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
            "api.anthropic.com", 443,
        ));
        assert!(!request_authority_matches(
            b"GET / HTTP/1.1\r\nHost: api.example\r\nHost: attacker.example\r\n\r\n",
            "api.example", 443,
        ));
        assert!(request_authority_matches(
            b"GET / HTTP/1.1\r\nHost: api.example:8443\r\n\r\n",
            "api.example", 8443,
        ));
    }

    #[test]
    fn ssrf_ranges_are_blocked() {
        // The blocked-range predicate now lives in the policy layer (it's a policy default a
        // per-agent allow CIDR can override) — assert it still covers the full set.
        use crate::policy::{is_blocked_range, is_metadata};
        let blocked = ["169.254.169.254", "127.0.0.1", "10.0.0.1", "172.16.5.5",
                       "192.168.1.1", "100.64.0.1", "0.0.0.0",
                       "::1", "fd00::1", "fe80::1", "::ffff:10.0.0.1"];
        for ip in blocked {
            assert!(is_blocked_range(&ip.parse().unwrap()), "{ip} should be blocked");
        }
        let allowed = ["8.8.8.8", "1.1.1.1", "140.82.112.3", "2606:4700:4700::1111"];
        for ip in allowed {
            assert!(!is_blocked_range(&ip.parse().unwrap()), "{ip} should be allowed");
        }
        // metadata is a distinct HARD floor (a broad allow can't reopen it)
        assert!(is_metadata(&"169.254.169.254".parse().unwrap()));
        assert!(!is_metadata(&"169.254.10.10".parse().unwrap()));
    }
}
