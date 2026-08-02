import { ORCH_BASE, orchHeaders } from "@/lib/orchestrator";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ id: string }> };

/** Stream the durable JSONL export without buffering it in the console process. */
export async function GET(req: Request, { params }: Ctx) {
  const { id } = await params;
  let upstream: Response;
  try {
    upstream = await fetch(
      `${ORCH_BASE}/v1/sessions/${encodeURIComponent(id)}/events/export`,
      {
        headers: await orchHeaders({ accept: "application/x-ndjson" }),
        cache: "no-store",
        signal: req.signal,
      },
    );
  } catch {
    return Response.json(
      { error: "orchestrator_unreachable", detail: "The orchestrator could not be reached." },
      { status: 502 },
    );
  }

  if (!upstream.ok || !upstream.body) {
    return Response.json(
      { error: "export_failed", detail: `Orchestrator returned ${upstream.status}.` },
      { status: upstream.status },
    );
  }

  const headers = new Headers({
    "content-type": upstream.headers.get("content-type") ?? "application/x-ndjson",
    "cache-control": "no-store",
  });
  const disposition = upstream.headers.get("content-disposition");
  const length = upstream.headers.get("content-length");
  if (disposition) headers.set("content-disposition", disposition);
  if (length) headers.set("content-length", length);
  return new Response(upstream.body, { status: 200, headers });
}
