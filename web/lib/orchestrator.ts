// Shared helpers for proxying browser requests to the orchestrator API.
// The browser talks to our Next route handlers; these helpers talk to the
// orchestrator (FastAPI) and centralize base-URL + auth.
//
// Auth: the operator logs in with TERRA_TOKEN, which we store in an httpOnly
// cookie and forward as a bearer. An env TERRA_TOKEN (service deploys) is used
// as a fallback so the console can also run pre-authenticated.

import { cookies } from "next/headers";

export const ORCH_BASE =
  process.env.TERRA_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8900";

export const ENV_TOKEN = process.env.TERRA_TOKEN;
export const AUTH_COOKIE = "terra_token";

/** Reject browser mutations initiated outside this console's origin.
 *
 * SameSite cookies help, but "same site" is broader than "same origin" and does
 * not protect deployments with an untrusted sibling subdomain. Non-browser
 * callers without Fetch Metadata/Origin headers are left to the orchestrator's
 * bearer authentication.
 */
/** The origin the BROWSER used, which is not the one Node sees behind a TLS-terminating proxy.
 *
 *  nginx (or any reverse proxy) terminates TLS and forwards over plain HTTP, so `req.url` is
 *  `http://host/...` while the browser's `Origin` header says `https://host`. Comparing the two
 *  directly rejects every mutation on a correctly-configured HTTPS deployment — login included —
 *  with a message that reads like a CSRF attack rather than a proxy artifact.
 *
 *  Trusting X-Forwarded-* here is safe for the threat this guard exists to stop. A cross-site
 *  page cannot set them: a custom request header makes the request non-simple, which forces a
 *  CORS preflight, and nothing here answers a preflight — so the real request is never sent.
 *  The unspoofable signal, `Sec-Fetch-Site`, is checked separately below and is a forbidden
 *  header name, so page JS cannot forge it at all.
 */
function browserOrigin(req: Request): string {
  const url = new URL(req.url);
  const proto = req.headers.get("x-forwarded-proto")?.split(",")[0].trim();
  const host = req.headers.get("x-forwarded-host")?.split(",")[0].trim() ?? url.host;
  return `${proto ?? url.protocol.replace(":", "")}://${host}`;
}

export function crossSiteMutation(req: Request): Response | null {
  if (req.method === "GET" || req.method === "HEAD" || req.method === "OPTIONS") return null;
  const expected = browserOrigin(req);
  const origin = req.headers.get("origin");
  const fetchSite = req.headers.get("sec-fetch-site");
  if ((origin && origin !== expected) || fetchSite === "cross-site") {
    return Response.json({ error: "cross_site_request" }, { status: 403 });
  }
  return null;
}

export async function getToken(): Promise<string | undefined> {
  try {
    const c = await cookies();
    return c.get(AUTH_COOKIE)?.value || ENV_TOKEN;
  } catch {
    return ENV_TOKEN;
  }
}

export async function orchHeaders(extra?: HeadersInit): Promise<Record<string, string>> {
  const h: Record<string, string> = {};
  const token = await getToken();
  if (token) h["Authorization"] = `Bearer ${token}`;
  if (extra) {
    const e = new Headers(extra);
    e.forEach((v, k) => (h[k] = v));
  }
  return h;
}

/** Proxy a request to the orchestrator and mirror its response back.
 *
 *  Body is forwarded as BYTES, not text: most endpoints answer JSON, but the workspace
 *  download answers arbitrary file content, and decoding that to a string would corrupt
 *  anything non-UTF-8 (a PNG the agent generated, say). Passing an ArrayBuffer through is
 *  identical for JSON and correct for the rest.
 *
 *  Content-Disposition is forwarded too — it carries the filename the browser saves under. */
export async function proxyJson(path: string, init?: RequestInit): Promise<Response> {
  const url = `${ORCH_BASE}${path}`;
  try {
    const upstream = await fetch(url, { ...init, headers: await orchHeaders(init?.headers), cache: "no-store" });
    const headers: Record<string, string> = {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    };
    const disposition = upstream.headers.get("content-disposition");
    if (disposition) headers["content-disposition"] = disposition;
    return new Response(await upstream.arrayBuffer(), { status: upstream.status, headers });
  } catch (err) {
    return Response.json(
      {
        error: "orchestrator_unreachable",
        detail: err instanceof Error ? "The orchestrator could not be reached." : "Upstream unavailable.",
      },
      { status: 502 },
    );
  }
}
