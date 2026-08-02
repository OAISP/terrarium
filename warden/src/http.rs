//! Minimal HTTP/1.1 framing for the MITM loop. Requests are read whole (so we can
//! scan/inject each one and find the next — keep-alive correctness); responses are
//! STREAMED chunk-by-chunk (so token-streaming/SSE UX is preserved) while scanned.
use crate::audit::Audit;
use crate::scan;
use crate::tls::read_head;
use anyhow::Result;
use serde_json::json;
use std::time::{Duration, Instant};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

/// Default idle bound for an in-flight forward: if neither an upstream read nor a worker
/// write makes progress for this long, the connection is torn down (freeing its semaphore
/// permit). Generous enough not to cut a legitimately slow token stream — Anthropic emits
/// SSE keepalives well inside this window — but finite, so one stalled peer can't pin a
/// permit forever (the DoS this closes). Override with WARDEN_STREAM_IDLE_SECS.
pub fn stream_idle() -> Duration {
    std::env::var("WARDEN_STREAM_IDLE_SECS").ok()
        .and_then(|v| v.parse().ok())
        .map(Duration::from_secs)
        .unwrap_or(Duration::from_secs(120))
}

/// How often, at most, a long streaming response re-checks the live policy for a kill/deny
/// so a switch flipped MID-STREAM interrupts it (not just the next request). Bounds the
/// mtime stat rate regardless of how fast chunks arrive.
const KILL_POLL: Duration = Duration::from_secs(2);

/// Bounds an in-flight forward: an idle read/write timeout (so a stalled upstream or worker
/// can't pin a connection — and thus a semaphore permit — forever) plus a mid-stream kill
/// check (so a kill switch flipped DURING a long streaming response interrupts it). The
/// kill closure is supplied by the proxy (it locks the live policy and re-checks the CONNECT
/// target); callers that don't need it (tests) use `idle_only`.
pub struct StreamGuard {
    idle: Duration,
    kill: Box<dyn Fn() -> bool + Send + Sync>,
}

impl StreamGuard {
    pub fn new(idle: Duration, kill: impl Fn() -> bool + Send + Sync + 'static) -> Self {
        Self { idle, kill: Box::new(kill) }
    }
    fn killed(&self) -> bool {
        (self.kill)()
    }
}

/// Await `fut`, but fail (tearing the connection down) if it makes no progress within the
/// guard's idle window. Because a timeout aborts the whole connection, the wrapped I/O need
/// not be cancel-safe — we never resume it.
async fn within<T>(g: &StreamGuard, what: &str, fut: impl std::future::Future<Output = Result<T>>) -> Result<T> {
    match tokio::time::timeout(g.idle, fut).await {
        Ok(r) => r,
        Err(_) => anyhow::bail!("{what} idle for {:?} — connection torn down", g.idle),
    }
}

pub fn header_value(head: &[u8], name: &str) -> Option<String> {
    header_values(head, name).into_iter().next()
}
/// ALL values for a header (case-insensitive), in order — so framing-critical
/// headers (Content-Length / Transfer-Encoding) can be checked for duplicates,
/// not just first-match. First-match-only is what enables a smuggling desync.
pub fn header_values(head: &[u8], name: &str) -> Vec<String> {
    let text = String::from_utf8_lossy(head);
    text.split("\r\n")
        .skip(1)
        .filter_map(|line| line.split_once(':'))
        .filter(|(k, _)| k.trim().eq_ignore_ascii_case(name))
        .map(|(_, v)| v.trim().to_string())
        .collect()
}
pub fn content_length(head: &[u8]) -> Option<usize> {
    header_value(head, "content-length").and_then(|v| v.parse().ok())
}
pub fn is_chunked(head: &[u8]) -> bool {
    header_value(head, "transfer-encoding").map(|v| v.to_lowercase().contains("chunked")).unwrap_or(false)
}
fn wants_close(head: &[u8]) -> bool {
    header_value(head, "connection").map(|v| v.to_lowercase().contains("close")).unwrap_or(false)
}

/// Reject heads with a bare CR or LF — a smuggled bare-LF separator can hide a
/// dummy auth header from the injector's CRLF-based strip (header smuggling).
pub fn well_formed_head(head: &[u8]) -> bool {
    let mut i = 0;
    while i < head.len() {
        match head[i] {
            b'\r' => {
                if head.get(i + 1) != Some(&b'\n') {
                    return false;
                }
                // Reject obs-fold (RFC 7230 §3.2.4): a header line continued via a leading
                // SP/HTAB. The CRLF-based auth-header strip in inject.rs can't see a folded
                // continuation, so a folded `Authorization:` line could survive the strip —
                // refuse the whole head rather than forward an ambiguous one.
                if matches!(head.get(i + 2), Some(&b' ') | Some(&b'\t')) {
                    return false;
                }
                i += 2;
            }
            b'\n' => return false, // bare LF
            _ => i += 1,
        }
    }
    true
}

/// Reject ambiguous request framing (request smuggling). Warden frames the body
/// by Content-Length (preferring it over Transfer-Encoding), but it forwards the
/// head verbatim — so an upstream that prefers Transfer-Encoding, or that picks a
/// different one of two Content-Length headers, frames the SAME bytes differently.
/// The bytes Warden treats as the next request are body to the upstream (or vice
/// versa): a smuggled second request that bypasses the per-request allow-list and
/// DLP re-check inside the keep-alive loop. Fail closed on any such ambiguity.
pub fn well_framed(head: &[u8]) -> bool {
    let cls = header_values(head, "content-length");
    let te = header_values(head, "transfer-encoding");
    // CL + TE together → Warden frames by CL, upstream may frame by TE.
    if !cls.is_empty() && !te.is_empty() {
        return false;
    }
    // Duplicate or non-numeric Content-Length → ambiguous length.
    if cls.len() > 1 || cls.first().is_some_and(|v| v.parse::<usize>().is_err()) {
        return false;
    }
    // Duplicate Transfer-Encoding header lines, or a coding we don't de-frame as
    // chunked (read_body only handles chunked).
    if te.len() > 1 || te.first().is_some_and(|v| !v.to_lowercase().contains("chunked")) {
        return false;
    }
    true
}

/// Read the request body as `(raw_to_forward, decoded_for_scan)`. Rejects bodies
/// over `max` (no silent truncation → no upstream desync) and bounds chunk sizes
/// BEFORE allocating (no attacker-controlled multi-GB alloc).
pub async fn read_body<R: AsyncRead + Unpin>(r: &mut R, head: &[u8], max: usize) -> Result<(Vec<u8>, Vec<u8>)> {
    if let Some(n) = content_length(head) {
        if n > max {
            anyhow::bail!("request body too large ({n} > {max})");
        }
        let mut body = vec![0u8; n];
        r.read_exact(&mut body).await?;
        let dec = body.clone();
        return Ok((body, dec));
    }
    if is_chunked(head) {
        return read_chunked(r, max).await;
    }
    Ok((Vec::new(), Vec::new()))
}

async fn read_crlf_line<R: AsyncRead + Unpin>(r: &mut R, out: &mut Vec<u8>) -> Result<()> {
    let mut b = [0u8; 1];
    loop {
        if r.read(&mut b).await? == 0 {
            break;
        }
        out.push(b[0]);
        if out.len() >= 2 && &out[out.len() - 2..] == b"\r\n" {
            break;
        }
    }
    Ok(())
}

async fn read_chunked<R: AsyncRead + Unpin>(r: &mut R, max: usize) -> Result<(Vec<u8>, Vec<u8>)> {
    let mut raw = Vec::new();
    let mut dec = Vec::new(); // de-chunked payload — DLP scans this, so a secret split
                              // across chunk boundaries can't evade the patterns
    loop {
        let mut size_line = Vec::new();
        read_crlf_line(r, &mut size_line).await?;
        if size_line.is_empty() {
            break;
        }
        let size = chunk_size(&size_line);
        // bound BEFORE allocating size+2 (the agent controls `size`)
        if size > max || dec.len().saturating_add(size) > max {
            anyhow::bail!("chunked body exceeds cap");
        }
        raw.extend_from_slice(&size_line);
        if size == 0 {
            let mut tr = Vec::new();
            read_crlf_line(r, &mut tr).await?;
            raw.extend_from_slice(&tr);
            break;
        }
        let mut chunk = vec![0u8; size + 2];
        r.read_exact(&mut chunk).await?;
        raw.extend_from_slice(&chunk);
        dec.extend_from_slice(&chunk[..size]);
    }
    Ok((raw, dec))
}

fn chunk_size(size_line: &[u8]) -> usize {
    let s = String::from_utf8_lossy(size_line);
    usize::from_str_radix(s.trim().split(';').next().unwrap_or("0").trim(), 16).unwrap_or(0)
}

fn flag_injection(data: &[u8], host: &str, audit: &Audit) {
    let hits = scan::scan_injection(data);
    if !hits.is_empty() {
        audit.log(json!({"decision": "injection-flag", "host": host, "patterns": hits}));
    }
}

/// Forward an already-buffered request (head + body) to the upstream WHILE streaming
/// the response back — the two directions run concurrently on split halves of the same
/// upstream stream. Returns the reunited upstream stream (for keep-alive reuse) and
/// whether it can be reused.
///
/// This concurrency is load-bearing, not an optimization. The old path wrote the whole
/// request, then read the response. When the request body exceeded the upstream socket
/// send buffer, `write_all` blocked waiting for the upstream to drain, while the upstream
/// blocked writing a response we had not started reading — a classic proxy write-deadlock
/// that stalled EVERY large (>~100 KB) request until the upstream reset it ~15 s later.
/// The sandbox's own HTTP client pumps both directions at once (which is why a direct
/// call succeeded where the mediated one hung); Warden must too.
pub async fn forward_request_response<U, W>(
    upstream: U, worker_write: &mut W, head: &[u8], body: &[u8], host: &str, audit: &Audit, guard: &StreamGuard,
) -> Result<(U, bool)>
where
    U: AsyncRead + AsyncWrite + Unpin,
    W: AsyncWrite + Unpin,
{
    let (mut ur, mut uw) = tokio::io::split(upstream);
    // Bound the request-send direction too: if the upstream stops draining, write_all would
    // otherwise block forever even though the response side is idle-guarded.
    let send_req = within(guard, "request send", async {
        uw.write_all(head).await?;
        if !body.is_empty() {
            uw.write_all(body).await?;
        }
        uw.flush().await?;
        Ok::<(), anyhow::Error>(())
    });
    let recv_resp = stream_response(&mut ur, worker_write, host, audit, guard);
    let ((), keepalive) = tokio::try_join!(send_req, recv_resp)?;
    Ok((ur.unsplit(uw), keepalive))
}

/// Stream one HTTP response upstream→worker, forwarding chunk-by-chunk (streaming
/// preserved) while scanning for prompt-injection. Returns whether the connection
/// can be reused (keep-alive). `guard` bounds idle stalls and honors a mid-stream kill.
pub async fn stream_response<R, W>(ur: &mut R, ww: &mut W, host: &str, audit: &Audit, g: &StreamGuard) -> Result<bool>
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let head = within(g, "response head", read_head(ur)).await?;
    if head.is_empty() {
        return Ok(false);
    }
    within(g, "worker write", async {
        ww.write_all(&head).await?;
        ww.flush().await?;
        Ok::<(), anyhow::Error>(())
    }).await?;
    let close = wants_close(&head);
    if let Some(n) = content_length(&head) {
        forward_n(ur, ww, n, host, audit, g).await?;
        Ok(!close)
    } else if is_chunked(&head) {
        forward_chunked(ur, ww, host, audit, g).await?;
        Ok(!close)
    } else {
        forward_to_eof(ur, ww, host, audit, g).await?; // SSE / HTTP-close
        Ok(false)
    }
}

/// Check the kill switch at most once per KILL_POLL, updating `last`. Returns Err to abort
/// the stream when the policy now denies this host — so a kill flipped mid-stream interrupts
/// even an actively-streaming response (checked between forwarded segments, so cancel-safe).
fn kill_check(g: &StreamGuard, last: &mut Instant) -> Result<()> {
    if last.elapsed() >= KILL_POLL {
        *last = Instant::now();
        if g.killed() {
            anyhow::bail!("stream revoked by policy (kill switch) mid-response");
        }
    }
    Ok(())
}

async fn forward_n<R: AsyncRead + Unpin, W: AsyncWrite + Unpin>(r: &mut R, w: &mut W, mut n: usize, host: &str, audit: &Audit, g: &StreamGuard) -> Result<()> {
    let mut buf = vec![0u8; 16384];
    let mut last = Instant::now();
    while n > 0 {
        let want = n.min(buf.len());
        let got = within(g, "upstream read", async { Ok(r.read(&mut buf[..want]).await?) }).await?;
        if got == 0 {
            // Upstream closed before delivering the full Content-Length body. Treat it as an
            // error (tears the connection down) rather than returning Ok with keepalive — a
            // short read would otherwise leave the keep-alive stream desynced for the next req.
            anyhow::bail!("upstream closed mid-body — {n} Content-Length byte(s) missing");
        }
        flag_injection(&buf[..got], host, audit);
        within(g, "worker write", async {
            w.write_all(&buf[..got]).await?;
            w.flush().await?;
            Ok::<(), anyhow::Error>(())
        }).await?;
        n -= got;
        kill_check(g, &mut last)?;
    }
    Ok(())
}

async fn forward_chunked<R: AsyncRead + Unpin, W: AsyncWrite + Unpin>(r: &mut R, w: &mut W, host: &str, audit: &Audit, g: &StreamGuard) -> Result<()> {
    let mut last = Instant::now();
    loop {
        let mut size_line = Vec::new();
        within(g, "upstream read", read_crlf_line(r, &mut size_line)).await?;
        if size_line.is_empty() {
            break;
        }
        // Guard the size-line write too: a worker that stalls its read after a flushed data chunk
        // would otherwise block this unguarded write forever, pinning the connection's permit and
        // defeating the idle guard the data-chunk write already has.
        within(g, "worker write", async {
            w.write_all(&size_line).await?;
            Ok::<(), anyhow::Error>(())
        }).await?;
        let size = chunk_size(&size_line);
        if size == 0 {
            // Trailer section: zero or more trailer-header lines, terminated by a blank CRLF.
            // Consume/forward EVERY line (not just the first) so the stream stays framed for
            // keep-alive reuse — leaving real trailers unread desyncs the next request.
            loop {
                let mut tr = Vec::new();
                within(g, "upstream read", read_crlf_line(r, &mut tr)).await?;
                within(g, "worker write", async {
                    w.write_all(&tr).await?;
                    Ok::<(), anyhow::Error>(())
                }).await?;
                if tr.len() <= 2 {
                    break; // blank CRLF (len 2) ends the trailers; a short/EOF line (0/1) too
                }
            }
            w.flush().await?;
            break;
        }
        let mut chunk = vec![0u8; size + 2];
        within(g, "upstream read", async { Ok(r.read_exact(&mut chunk).await?) }).await?;
        flag_injection(&chunk[..size], host, audit);
        within(g, "worker write", async {
            w.write_all(&chunk).await?;
            w.flush().await?;
            Ok::<(), anyhow::Error>(())
        }).await?;
        kill_check(g, &mut last)?;
    }
    Ok(())
}

async fn forward_to_eof<R: AsyncRead + Unpin, W: AsyncWrite + Unpin>(r: &mut R, w: &mut W, host: &str, audit: &Audit, g: &StreamGuard) -> Result<()> {
    let mut buf = vec![0u8; 16384];
    let mut last = Instant::now();
    loop {
        let n = within(g, "upstream read", async { Ok(r.read(&mut buf).await?) }).await?;
        if n == 0 {
            break;
        }
        flag_injection(&buf[..n], host, audit);
        within(g, "worker write", async {
            w.write_all(&buf[..n]).await?;
            w.flush().await?;
            Ok::<(), anyhow::Error>(())
        }).await?;
        kill_check(g, &mut last)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn wellformed_rejects_bare_lf_cr() {
        assert!(well_formed_head(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"));
        assert!(!well_formed_head(b"GET / HTTP/1.1\r\nHost: x\nAuthorization: DUMMY\r\n\r\n")); // bare LF smuggle
        assert!(!well_formed_head(b"GET /\rHTTP/1.1\r\n\r\n"));                                // bare CR
    }
    #[test]
    fn wellformed_rejects_obs_fold() {
        // an Authorization header folded across a continuation line must be rejected — the
        // CRLF-based auth strip can't see the folded remainder, so it could survive.
        assert!(!well_formed_head(b"GET / HTTP/1.1\r\nAuthorization: DUM\r\n MY\r\n\r\n")); // SP fold
        assert!(!well_formed_head(b"GET / HTTP/1.1\r\nX: a\r\n\tb\r\n\r\n"));               // HTAB fold
        // a normal header immediately after CRLF (no leading whitespace) is still fine
        assert!(well_formed_head(b"GET / HTTP/1.1\r\nA: 1\r\nB: 2\r\n\r\n"));
    }

    #[tokio::test]
    async fn forward_times_out_on_a_stalled_upstream() {
        // A response head that never arrives must not hang forever — the idle guard tears the
        // connection down, freeing the semaphore permit (the DoS fix). A short idle keeps the
        // test fast.
        let (upstream, _server) = tokio::io::duplex(1024); // server end held open, never writes
        let (mut worker_write, _wr) = tokio::io::duplex(1024);
        let audit = Audit::new(None);
        let guard = StreamGuard::new(Duration::from_millis(50), || false);
        let head = "GET / HTTP/1.1\r\nhost: x\r\n\r\n";
        let err = forward_request_response(upstream, &mut worker_write, head.as_bytes(), &[], "x", &audit, &guard)
            .await
            .expect_err("a stalled upstream must time out, not hang");
        assert!(err.to_string().contains("idle"), "got: {err}");
    }

    #[tokio::test]
    async fn kill_check_aborts_between_segments() {
        // A kill flipped mid-stream aborts at the next segment boundary. kill_check is
        // rate-limited by KILL_POLL, so force the first check to fire by backdating `last`.
        let guard = StreamGuard::new(Duration::from_secs(1), || true); // policy now denies
        let mut last = Instant::now() - KILL_POLL - Duration::from_secs(1);
        assert!(kill_check(&guard, &mut last).is_err(), "a live kill must abort the stream");
        // and a non-killing guard is a no-op
        let ok = StreamGuard::new(Duration::from_secs(1), || false);
        let mut last2 = Instant::now() - KILL_POLL - Duration::from_secs(1);
        assert!(kill_check(&ok, &mut last2).is_ok());
    }

    #[tokio::test]
    async fn chunked_consumes_all_trailers() {
        // One data chunk, the 0-terminator, TWO trailer headers, blank line, then a sentinel
        // that MUST remain byte-aligned for the next keep-alive request.
        let resp = "HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n\
                    3\r\nabc\r\n0\r\nX-A: 1\r\nX-B: 2\r\n\r\nNEXT";
        let (mut client, mut server) = tokio::io::duplex(4096);
        server.write_all(resp.as_bytes()).await.unwrap();
        let (mut ww, _wr) = tokio::io::duplex(8192);
        let audit = Audit::new(None);
        let guard = StreamGuard::new(Duration::from_secs(5), || false);
        let keepalive = stream_response(&mut client, &mut ww, "x", &audit, &guard).await.unwrap();
        assert!(keepalive, "no connection: close ⇒ reusable");
        let mut rest = [0u8; 4];
        client.read_exact(&mut rest).await.unwrap();
        assert_eq!(&rest, b"NEXT", "trailers fully consumed → next request byte-aligned");
    }

    #[tokio::test]
    async fn truncated_content_length_is_an_error() {
        // Content-Length promises 10 bytes; the upstream sends 3 then closes. Must ERROR
        // (connection torn down), not return keepalive over a desynced stream.
        let resp = "HTTP/1.1 200 OK\r\ncontent-length: 10\r\n\r\nabc";
        let (mut client, server) = tokio::io::duplex(4096);
        let sh = tokio::spawn(async move { let mut s = server; s.write_all(resp.as_bytes()).await.unwrap(); });
        let (mut ww, _wr) = tokio::io::duplex(8192);
        let audit = Audit::new(None);
        let guard = StreamGuard::new(Duration::from_secs(5), || false);
        let err = stream_response(&mut client, &mut ww, "x", &audit, &guard).await
            .expect_err("a short Content-Length body must error");
        assert!(err.to_string().contains("mid-body"), "got: {err}");
        sh.await.unwrap();
    }

    #[test]
    fn framing_helpers() {
        let h = b"POST / HTTP/1.1\r\nContent-Length: 42\r\nTransfer-Encoding: chunked\r\n\r\n";
        assert_eq!(content_length(h), Some(42));
        assert!(is_chunked(h));
        assert_eq!(chunk_size(b"1a\r\n"), 26);
        assert_eq!(chunk_size(b"0\r\n"), 0);
    }
    #[test]
    fn well_framed_rejects_smuggling() {
        // legit: exactly one CL, or one chunked TE, or neither
        assert!(well_framed(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\n"));
        assert!(well_framed(b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"));
        assert!(well_framed(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"));
        // CL + TE together (classic CL.TE / TE.CL desync)
        assert!(!well_framed(b"POST / HTTP/1.1\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n"));
        // duplicate / conflicting Content-Length
        assert!(!well_framed(b"POST / HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 48\r\n\r\n"));
        // non-numeric Content-Length
        assert!(!well_framed(b"POST / HTTP/1.1\r\nContent-Length: 5x\r\n\r\n"));
        // duplicate Transfer-Encoding header lines
        assert!(!well_framed(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n\r\n"));
    }

    #[tokio::test]
    async fn forward_request_response_does_not_deadlock_on_large_body() {
        // Regression for the production stall: a request body larger than the upstream
        // socket buffer must not deadlock against an upstream that also sends a large
        // response. tokio::io::duplex has a small fixed buffer, so the OLD sequential
        // path (write whole request, THEN read response) hangs here; the concurrent
        // path completes. The 10s timeout turns a regression into a failure, not a hang.
        let body_len = 512 * 1024; // >> the 16 KiB duplex buffer
        let resp_len = 512 * 1024;
        let head = format!("POST /v1/messages HTTP/1.1\r\nhost: x\r\ncontent-length: {body_len}\r\n\r\n");
        let body = vec![b'a'; body_len];

        let (upstream, server) = tokio::io::duplex(16 * 1024);
        // A realistic bidirectional server: echo-drain the request and stream a
        // Content-Length response CONCURRENTLY, so neither side can rely on the other
        // fully draining first (exactly the condition that deadlocked the old path).
        let expected_req = head.len() + body_len;
        let server_task = tokio::spawn(async move {
            let (mut sr, mut sw) = tokio::io::split(server);
            let resp_head = format!("HTTP/1.1 200 OK\r\ncontent-length: {resp_len}\r\nconnection: close\r\n\r\n");
            let drain = async {
                let mut got = 0usize;
                let mut buf = vec![0u8; 32 * 1024];
                while got < expected_req {
                    let n = sr.read(&mut buf).await.unwrap();
                    if n == 0 { break; }
                    got += n;
                }
                got
            };
            let respond = async {
                sw.write_all(resp_head.as_bytes()).await.unwrap();
                sw.write_all(&vec![b'b'; resp_len]).await.unwrap();
                sw.flush().await.unwrap();
            };
            let (got, ()) = tokio::join!(drain, respond);
            got
        });

        // worker_write sink: drain concurrently so it never backpressures the forward.
        let (mut worker_write, mut worker_read) = tokio::io::duplex(16 * 1024);
        let sink = tokio::spawn(async move {
            let mut buf = vec![0u8; 32 * 1024];
            let mut total = 0usize;
            while let Ok(n) = worker_read.read(&mut buf).await {
                if n == 0 { break; }
                total += n;
            }
            total
        });

        let audit = Audit::new(None);
        let guard = StreamGuard::new(Duration::from_secs(30), || false);
        let fut = forward_request_response(upstream, &mut worker_write, head.as_bytes(), &body, "x", &audit, &guard);
        let (_, keepalive) = tokio::time::timeout(std::time::Duration::from_secs(10), fut)
            .await
            .expect("forward deadlocked (sequential-forward regression)")
            .expect("forward errored");
        assert!(!keepalive, "connection: close ⇒ not reusable");
        drop(worker_write); // let the sink see EOF
        let forwarded = sink.await.unwrap();
        // the worker receives the response head + the full body (head len varies, so
        // assert the body made it through in full)
        assert!(forwarded > resp_len, "the full response (head + body) reached the worker");
        assert_eq!(server_task.await.unwrap(), expected_req, "the full request reached the upstream");
    }
}
