import { ORCH_BASE, orchHeaders } from "@/lib/orchestrator";

export const dynamic = "force-dynamic";
// Keep the connection open: this is a long-lived SSE stream.
export const fetchCache = "force-no-store";

type Ctx = { params: Promise<{ id: string }> };

/**
 * Proxy the orchestrator's Server-Sent Events stream. We pass `?after=<seq>`
 * straight through so the orchestrator replays persisted events with
 * seq > after before streaming live ones. The upstream body is piped to the
 * client untouched so the browser EventSource sees `data: <json>` lines.
 */
export async function GET(req: Request, { params }: Ctx) {
  const { id } = await params;
  const search = new URL(req.url).searchParams;
  const qs = new URLSearchParams();
  const after = search.get("after");
  const tail = search.get("tail");
  if (after !== null) qs.set("after", after);
  if (tail !== null) qs.set("tail", tail);
  const query = qs.size ? `?${qs}` : "";
  const url = `${ORCH_BASE}/v1/sessions/${encodeURIComponent(id)}/events${query}`;

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      headers: await orchHeaders({ accept: "text/event-stream" }),
      cache: "no-store",
      // Allow the abort signal from the client to tear down the upstream fetch.
      signal: req.signal,
    });
  } catch (err) {
    return new Response(
      `data: ${JSON.stringify({
        type: "error",
        message: err instanceof Error
          ? "Cannot reach the orchestrator."
          : "The event stream is unavailable.",
      })}\n\n`,
      { status: 502, headers: { "content-type": "text/event-stream" } },
    );
  }

  if (!upstream.ok || !upstream.body) {
    const detail = upstream.body ? await upstream.text() : "no body";
    return new Response(
      `data: ${JSON.stringify({
        type: "error",
        message: `Orchestrator returned ${upstream.status}: ${detail.slice(0, 300)}`,
      })}\n\n`,
      { status: upstream.status, headers: { "content-type": "text/event-stream" } },
    );
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    },
  });
}
