import { cookies } from "next/headers";
import { ORCH_BASE, AUTH_COOKIE, crossSiteMutation } from "@/lib/orchestrator";

export const dynamic = "force-dynamic";

/** Validate the supplied token against the orchestrator, then store it (httpOnly). */
export async function POST(req: Request) {
  const rejected = crossSiteMutation(req);
  if (rejected) return rejected;
  let token = "";
  try {
    token = (await req.json())?.token ?? "";
  } catch {
    /* ignore */
  }
  token = String(token).trim();
  if (!token) return Response.json({ ok: false, error: "Token is required." }, { status: 400 });

  let status: number;
  try {
    const r = await fetch(`${ORCH_BASE}/v1/me`, { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
    status = r.status;
    if (r.ok) {
      const principal = await r.json().catch(() => null);
      if (!principal?.can?.admin) {
        return Response.json(
          { ok: false, error: "The operator console requires an admin token." },
          { status: 403 },
        );
      }
    }
  } catch {
    return Response.json({ ok: false, error: "Cannot reach the orchestrator." }, { status: 502 });
  }
  if (status === 401) return Response.json({ ok: false, error: "Invalid token." }, { status: 401 });
  if (status >= 500) return Response.json({ ok: false, error: `Orchestrator error (${status}).` }, { status: 502 });

  (await cookies()).set(AUTH_COOKIE, token, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 60 * 60 * 24 * 30,
  });
  return Response.json({ ok: true });
}
