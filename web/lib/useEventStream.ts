"use client";

import { useEffect, useRef, useState } from "react";
import type { LogEvent } from "./types";

type ConnState = "connecting" | "open" | "error" | "closed";
const INITIAL_REPLAY_LIMIT = 1000;
const RENDERED_EVENT_LIMIT = 2000;

/**
 * Subscribe to a session's SSE stream via our proxy route. Maintains a cursor
 * of the last seen seq so reconnects replay only missed events (`?after=`).
 * Uses EventSource; on error EventSource auto-reconnects, but to preserve the
 * cursor across reconnects we tear down and rebuild with the new `after`.
 */
export function useEventStream(sessionId: string | null) {
  const [events, setEvents] = useState<LogEvent[]>([]);
  // Live token-streaming text for the in-flight assistant turn (transient assistant_delta
  // events — never persisted). Cleared the instant the canonical assistant_text lands.
  const [streaming, setStreaming] = useState<string>("");
  const [conn, setConn] = useState<ConnState>(sessionId ? "connecting" : "closed");
  // Retry telemetry so the UI can say WHICH attempt and WHEN the next one lands, instead of
  // rendering the same "Reconnecting" pixel forever. `retryNow` lets the operator skip the wait.
  const [retry, setRetry] = useState<{ attempt: number; at: number } | null>(null);
  const [historyTruncated, setHistoryTruncated] = useState(false);
  const retryNowRef = useRef<(() => void) | null>(null);
  // -1 (not 0): the server returns events with seq > after, so 0 would skip
  // seq 0 (session_start). Start at -1 so the first connect replays from seq 0.
  const lastSeqRef = useRef(-1);

  // Reset on a session change DURING RENDER, not inside the effect. React handles a setState in
  // the render phase by discarding the in-progress output and re-rendering immediately, so the
  // consumer never paints a frame of the PREVIOUS session's transcript under the new session's
  // header. Doing it in an effect showed that stale frame first, and is exactly the cascading
  // render React 19 warns about.
  const [boundSid, setBoundSid] = useState(sessionId);
  if (boundSid !== sessionId) {
    setBoundSid(sessionId);
    setEvents([]);
    setStreaming("");
    setConn(sessionId ? "connecting" : "closed");
    setRetry(null);
    setHistoryTruncated(false);
  }

  useEffect(() => {
    if (!sessionId) return;
    // The CURSOR reset lives here, not in the render-phase block above: writing a ref during
    // render is disallowed (it can't be replayed). This effect re-runs on a sessionId change
    // and always before the EventSource below is created, so the cursor is fresh either way.
    lastSeqRef.current = -1;

    let es: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    let ended = false;  // got session_end → terminal; do NOT reconnect
    let attempt = 0;    // consecutive failed connects → drives the backoff curve

    // Coalesce incoming events into ONE state update per animation frame. A busy turn
    // (or a reconnect replay of the whole backlog) emits many events/second; appending
    // one at a time caused a full SessionView+EventTimeline render PER event — the
    // "not reactive" feel during a live run. Buffer + flush once per frame instead.
    const buf: LogEvent[] = [];
    let partial = { sid: 0, text: "" };  // current streaming text block
    let streamDirty = false;
    let raf: ReturnType<typeof requestAnimationFrame> | null = null;
    const flush = () => {
      raf = null;
      if (buf.length) {
        setEvents((prev) => [...prev, ...buf.splice(0)].slice(-RENDERED_EVENT_LIMIT));
      }
      if (streamDirty) { streamDirty = false; setStreaming(partial.text); }
    };
    const schedule = () => { if (raf == null) raf = requestAnimationFrame(flush); };

    const connect = () => {
      if (closed) return;
      const after = lastSeqRef.current;
      es = new EventSource(
        `/api/sessions/${sessionId}/events?after=${after}${after < 0 ? `&tail=${INITIAL_REPLAY_LIMIT}` : ""}`,
      );

      es.onopen = () => { attempt = 0; setRetry(null); setConn("open"); };

      es.onmessage = (e) => {
        if (!e.data) return;
        let ev: LogEvent;
        try {
          ev = JSON.parse(e.data) as LogEvent;
        } catch {
          return;
        }
        if (ev.type === "_history_truncated") {
          setHistoryTruncated(true);
          if (typeof ev.seq === "number") lastSeqRef.current = ev.seq;
          return;
        }
        // Live token streaming — transient (no seq): accumulate into the partial bubble,
        // never into the persisted events list. A new stream_id = a new text block.
        if (ev.type === "assistant_delta") {
          const sid = typeof ev.stream_id === "number" ? ev.stream_id : partial.sid;
          if (sid !== partial.sid) partial = { sid, text: "" };
          if (typeof ev.text === "string") partial.text += ev.text;
          streamDirty = true;
          schedule();
          return;
        }
        // de-dupe by seq (replay overlap on reconnect)
        if (typeof ev.seq === "number") {
          if (ev.seq <= lastSeqRef.current) return;
          if (ev.seq > lastSeqRef.current) lastSeqRef.current = ev.seq;
        }
        // canonical assistant text arrived → drop the live partial (no double-render)
        if (ev.type === "assistant_text") { partial = { sid: partial.sid, text: "" }; streamDirty = true; }
        buf.push(ev);
        schedule();
        // terminal event → close for good. The server always ends a finished
        // session with session_end (synthetic if it ended uncleanly), so without
        // this the EventSource's normal close looks like an error and we'd
        // reconnect-loop every 2s forever, re-reading the whole log each time.
        if (ev.type === "session_end") {
          ended = true;
          es?.close();
          setConn("closed");
        }
      };

      es.onerror = () => {
        es?.close();
        if (closed || ended) return;
        setConn("error");
        // Reconnect with the preserved cursor, backing off 2s → 30s with jitter. A fixed 2s retry
        // hammered a dead orchestrator forever, and attempt #2 was pixel-identical to attempt
        // #1800 — the operator could never tell "blipped" from "gone". Jitter avoids every open
        // session stampeding the orchestrator in lockstep the moment it restarts.
        attempt += 1;
        const base = Math.min(2000 * 2 ** (attempt - 1), 30000);
        const delay = base * (0.75 + Math.random() * 0.5);
        setRetry({ attempt, at: Date.now() + delay });
        retryTimer = setTimeout(connect, delay);
      };
    };

    connect();
    // "Retry now" skips the remaining backoff — a dead-then-revived orchestrator shouldn't make
    // the operator sit through a 30s wait with no way to ask again.
    retryNowRef.current = () => {
      if (closed || ended) return;
      if (retryTimer) clearTimeout(retryTimer);
      attempt = 0;
      setRetry(null);
      setConn("connecting");
      connect();
    };

    return () => {
      closed = true;
      retryNowRef.current = null;
      setConn("closed");
      if (retryTimer) clearTimeout(retryTimer);
      if (raf != null) cancelAnimationFrame(raf);
      es?.close();
    };
  }, [sessionId]);

  return {
    events, conn, streaming, retry, historyTruncated,
    retryNow: () => retryNowRef.current?.(),
  };
}
