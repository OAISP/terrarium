import { crossSiteMutation, proxyJson } from "@/lib/orchestrator";

export const dynamic = "force-dynamic";

// One proxy for every orchestrator endpoint the console calls.
//
// This replaced 30 route files (~460 lines) that were each the same three lines with a
// different path and method spelled out — so adding a backend endpoint meant hand-writing
// another file, and the four that drifted (a missing method, a dropped query param) were
// invisible until something 405'd at runtime.
//
// It is NOT an open proxy: a request must match a shape in ALLOW below, or it 404s here
// without ever reaching the orchestrator. That keeps the reviewable surface in one table
// instead of spread across a directory tree.
//
// Not handled here (they need real logic, so they keep their own files):
//   /api/auth/*                     — cookie session, never forwarded upstream
//   /api/sessions/:id/events        — long-lived SSE stream, piped not buffered
//   /api/sessions/:id/events/export — potentially large JSONL, piped not buffered

type Method = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";

/** ":" matches exactly one path segment. Anything else is a literal. */
const ALLOW: { path: string; methods: Method[] }[] = [
  { path: "health", methods: ["GET"] },
  { path: "me", methods: ["GET"] },

  { path: "agents", methods: ["GET", "POST"] },
  { path: "agents/:", methods: ["GET", "PATCH", "DELETE"] },
  // Cumulative per-agent budget ledger (all-time / 24h / 30d) — powers the Usage view.
  { path: "agents/:/spend", methods: ["GET"] },

  { path: "sessions", methods: ["GET", "POST"] },
  { path: "sessions/:", methods: ["GET", "DELETE"] },
  { path: "sessions/:/messages", methods: ["POST"] },
  { path: "sessions/:/interrupt", methods: ["POST"] },
  // Reattach a session whose sandbox outlived the orchestrator's stream to it.
  { path: "sessions/:/recover", methods: ["POST"] },
  { path: "sessions/:/answer", methods: ["POST"] },
  { path: "sessions/:/permission", methods: ["POST"] },
  { path: "sessions/:/config", methods: ["POST"] },
  { path: "sessions/:/rewind", methods: ["POST"] },
  { path: "sessions/:/files/upload", methods: ["POST"] },
  // Download one workspace artifact. The response is arbitrary agent-authored bytes, which
  // is why proxyJson forwards a buffer rather than decoded text.
  { path: "sessions/:/files/:", methods: ["GET"] },
  { path: "sessions/:/egress/verify", methods: ["GET"] },

  { path: "schedules", methods: ["GET", "POST"] },
  { path: "schedules/:", methods: ["PATCH", "DELETE"] },
  { path: "schedules/:/run", methods: ["POST"] },

  { path: "tokens", methods: ["GET", "POST"] },
  { path: "tokens/:", methods: ["DELETE"] },

  { path: "secrets", methods: ["GET", "POST"] },
  { path: "secrets/:", methods: ["DELETE"] },

  { path: "environments", methods: ["GET", "POST"] },
  { path: "environments/:", methods: ["PATCH", "DELETE"] },

  { path: "egress/policy", methods: ["GET", "PUT"] },
  { path: "egress/presets", methods: ["GET"] },
  { path: "egress/audit", methods: ["GET"] },
  { path: "egress/profiles", methods: ["GET", "POST"] },
  { path: "egress/profiles/:", methods: ["PATCH", "DELETE"] },

  { path: "credentials", methods: ["POST", "DELETE"] },
  { path: "credentials/status", methods: ["GET"] },

  { path: "logs", methods: ["GET"] },
  // Fleet spend over a window (?days=N) — the durable ledger behind the Usage view.
  { path: "usage", methods: ["GET"] },
  { path: "templates", methods: ["GET"] },
  { path: "models", methods: ["GET"] },
  { path: "tools", methods: ["GET"] },
];

/** The orchestrator path for an allowed request, or null to 404 without forwarding. */
function upstream(segments: string[], method: string): string | null {
  // "." / ".." / "" in a wildcard slot would let URL normalization walk the upstream path
  // (…/v1/agents/.. collapses to /v1/), so reject them before building anything.
  if (segments.some((s) => !s || s === "." || s === "..")) return null;
  // The method is part of the match, not a check applied after it. Two rules can share a
  // shape — `files/upload` (POST) and `files/:` (GET) are both four segments — and matching
  // on shape alone would let the first one found reject a request the second one allows.
  const match = ALLOW.find((r) => {
    const want = r.path.split("/");
    return want.length === segments.length
      && want.every((w, i) => w === ":" || w === segments[i])
      && (r.methods as string[]).includes(method);
  });
  if (!match) return null;
  // Re-encode each segment: params arrive decoded, and a secret name or session id may
  // contain characters that must not change the path's shape on the way out.
  const encoded = segments.map(encodeURIComponent).join("/");
  // Liveness/readiness sit outside the versioned API.
  return segments[0] === "health" ? "/healthz" : `/v1/${encoded}`;
}

async function handle(req: Request, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const rejected = crossSiteMutation(req);
  if (rejected) return rejected;
  const { path } = await ctx.params;
  const target = upstream(path ?? [], req.method);
  if (!target) return Response.json({ error: "not_found" }, { status: 404 });

  const init: RequestInit = { method: req.method };
  if (req.method !== "GET" && req.method !== "DELETE") {
    // Forward the raw bytes plus the original content-type. That covers JSON and, crucially,
    // multipart uploads — re-building a FormData would mint a fresh boundary, and buffering
    // bytes lets fetch set an accurate Content-Length so the orchestrator's upload guard can
    // reject an oversized file on the header instead of after spooling it.
    init.body = await req.arrayBuffer();
    const ct = req.headers.get("content-type");
    if (ct) init.headers = { "content-type": ct };
  }
  // Query params (log filters, ?purge_memory, ?limit, ?after) pass through verbatim.
  return proxyJson(`${target}${new URL(req.url).search}`, init);
}

export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const PUT = handle;
export const DELETE = handle;
