import { cookies } from "next/headers";
import { ORCH_BASE, AUTH_COOKIE, ENV_TOKEN } from "@/lib/orchestrator";

export const dynamic = "force-dynamic";

/** Does the orchestrator require a token, and are we currently authenticated? */
export async function GET() {
  let required = false;
  let reachable = true;
  try {
    // probe a protected endpoint with NO auth — 401 means a token is required
    const r = await fetch(`${ORCH_BASE}/v1/agents`, { cache: "no-store" });
    required = r.status === 401;
  } catch {
    reachable = false;
  }
  const cookieToken = (await cookies()).get(AUTH_COOKIE)?.value;
  const token = cookieToken || ENV_TOKEN;
  let authed = false;
  if (token && reachable) {
    try {
      const r = await fetch(`${ORCH_BASE}/v1/me`, {
        headers: { Authorization: `Bearer ${token}` }, cache: "no-store",
      });
      const principal = r.ok ? await r.json() : null;
      authed = !!principal?.can?.admin;
    } catch {
      reachable = false;
    }
  } else if (!required && reachable) {
    authed = true;
  }
  return Response.json({ required, authed, reachable });
}
