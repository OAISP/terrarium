import type { LogEvent } from "@/lib/types";

/** A file the agent wrote that the operator can pull back out of the workspace. */
export type Artifact = { name: string; tool: string; seq: number };

// Tools whose input names a file the agent CREATED or CHANGED. Read/Grep touch files too,
// but nothing came out of the run because of them — this list is "what did it produce",
// not "what did it look at".
const WRITE_TOOLS = new Set(["Write", "Edit", "MultiEdit", "NotebookEdit"]);

// The download endpoint takes a bare name under /workspace: no separators, and only the
// characters the orchestrator's _safe_name allows. That is deliberate (the sandbox is
// untrusted and picks these names), so a file the agent wrote into a subdirectory is not
// offered — listing one would just produce a 400 on click.
const DOWNLOADABLE = /^[A-Za-z0-9._-]+$/;

function workspaceBasename(raw: unknown): string | null {
  if (typeof raw !== "string" || !raw) return null;
  const path = raw.startsWith("/workspace/") ? raw.slice("/workspace/".length) : raw;
  if (path.includes("/") || path.includes("\\")) return null;  // a subdirectory — not fetchable
  return DOWNLOADABLE.test(path) ? path : null;
}

/**
 * The downloadable artifacts of a session, newest first.
 *
 * Derived from the transcript rather than listed from the sandbox: there is no
 * directory-listing endpoint, and adding one would mean handing an untrusted process's
 * view of its own filesystem to the console. The transcript already records every write
 * the agent made, and it survives the sandbox — so this still works on a finished run.
 *
 * De-duplicated by name, keeping the LAST write: a file edited five times is one artifact
 * whose current contents are what a download returns.
 */
export function artifactsFrom(events: LogEvent[]): Artifact[] {
  const byName = new Map<string, Artifact>();
  for (const ev of events) {
    if (ev.type !== "tool_use") continue;
    const tool = typeof ev.name === "string" ? ev.name : "";
    if (!WRITE_TOOLS.has(tool)) continue;
    const input = ev.input as Record<string, unknown> | undefined;
    const name = workspaceBasename(input?.file_path ?? input?.path ?? input?.notebook_path);
    if (name) byName.set(name, { name, tool, seq: ev.seq });
  }
  return [...byName.values()].sort((a, b) => b.seq - a.seq);
}
