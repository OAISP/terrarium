import { Suspense } from "react";
import { Console } from "@/components/Console";

// Optional catch-all so every view has a real address (/sessions, /sessions/:id, /agents,
// /egress, …) while the console stays a single client tree — no per-route state duplication.
// The shell derives its view from the path; this segment just renders it.

// Render fresh every request — the console is a live dashboard, and this avoids
// the static-route cache (s-maxage) serving a stale shell after a redeploy.
export const dynamic = "force-dynamic";

// The console reads useSearchParams (?new=<agentId>), which must sit under a Suspense
// boundary or Next bails the route out of static generation at build time.
export default function Page() {
  return (
    <Suspense>
      <Console />
    </Suspense>
  );
}
