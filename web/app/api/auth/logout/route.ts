import { cookies } from "next/headers";
import { AUTH_COOKIE, crossSiteMutation } from "@/lib/orchestrator";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const rejected = crossSiteMutation(req);
  if (rejected) return rejected;
  (await cookies()).delete(AUTH_COOKIE);
  return Response.json({ ok: true });
}
