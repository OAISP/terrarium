"use client";

import * as React from "react";
import { ShieldQuestion, Check, X, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CollapsibleCard } from "@/components/ui/collapsible-card";
import { toolOneLiner } from "@/lib/format";
import { Chip } from "@/components/ui/chip";
import type { Decision } from "@/lib/api";

const TOOL_TONE = "var(--c-tool)";

// Tools whose primary input arg reads well as the prompt header. Gating keeps the prompt quiet
// for everything else (it falls back to the title / JSON), matching the prior behavior.
const ONE_LINER_TOOLS = new Set(["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch"]);

/** Pull a short, human one-liner out of a tool's input for the prompt header. */
function summarize(tool: string, input: Record<string, unknown> | undefined): string {
  return ONE_LINER_TOOLS.has(tool) ? toolOneLiner(input) : "";
}

/**
 * Renders an agent tool-permission request (EV_PERMISSION) as an Allow / Always / Deny
 * prompt with the tool + its arguments. Locks once decided (driven by EV_DECIDED so
 * replay/reconnect stays consistent).
 */
export function PermissionCard({
  requestId,
  toolName,
  input,
  title,
  description,
  decided,
  onDecide,
}: {
  requestId: string;
  toolName: string;
  input?: Record<string, unknown>;
  title?: string;
  description?: string;
  decided?: Decision; // set → locked
  onDecide: (requestId: string, decision: Decision) => Promise<void>;
}) {
  const [busy, setBusy] = React.useState<Decision | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const oneLiner = summarize(toolName, input);

  async function decide(d: Decision) {
    setBusy(d); setErr(null);
    try { await onDecide(requestId, d); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); setBusy(null); }
  }

  // Once decided, the prompt folds to a compact one-line verdict (expandable) so resolved
  // permissions don't keep taking full-card space in the transcript.
  if (decided) {
    const denied = decided === "deny";
    const vtone = denied ? "var(--c-error)" : "var(--c-agent)";
    const verdict = denied ? "Denied" : decided === "always" ? "Allowed (always)" : "Allowed";
    const body = oneLiner || (input && Object.keys(input).length > 0) || description;
    return (
      <CollapsibleCard tone={vtone} contentClassName="px-3 py-2"
        header={<>
          {denied ? <X className="size-3.5 flex-none" style={{ color: vtone }} /> : <Check className="size-3.5 flex-none" style={{ color: vtone }} />}
          <span className="flex-none font-medium" style={{ color: vtone }}>{verdict}</span>
          <Chip tone={TOOL_TONE} mono className="flex-none font-medium">{toolName}</Chip>
          <span className="min-w-0 flex-1 truncate font-mono text-faint">{oneLiner || title}</span>
        </>}>
        {body ? (
          <>
            {oneLiner ? (
              <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded bg-surface px-2 py-1.5 font-mono text-xs text-text">{oneLiner}</pre>
            ) : input && Object.keys(input).length > 0 ? (
              <pre className="max-h-40 overflow-auto rounded bg-surface px-2 py-1.5 font-mono text-2xs text-muted">{JSON.stringify(input, null, 2)}</pre>
            ) : null}
            {description && <div className="mt-1.5 text-xs text-faint">{description}</div>}
          </>
        ) : null}
      </CollapsibleCard>
    );
  }

  return (
    <div className="my-2 rounded-lg border p-4 shadow-soft"
      style={{ borderColor: `color-mix(in oklch, ${TOOL_TONE} 32%, var(--border))`, background: `color-mix(in oklch, ${TOOL_TONE} 6%, var(--surface))` }}>
      <div className="mb-2 flex items-center gap-2 text-sm font-medium">
        <ShieldQuestion className="size-4" style={{ color: TOOL_TONE }} /> The agent wants to use a tool
      </div>

      <div className="rounded-lg border border-border bg-surface-2 p-2.5">
        <div className="flex items-center gap-2">
          <Chip tone={TOOL_TONE} mono className="rounded-md font-medium">{toolName}</Chip>
          {title && <span className="truncate text-xs text-muted">{title}</span>}
        </div>
        {oneLiner ? (
          <pre className="mt-1.5 overflow-x-auto whitespace-pre-wrap break-words rounded bg-surface px-2 py-1.5 font-mono text-xs text-text">{oneLiner}</pre>
        ) : input && Object.keys(input).length > 0 ? (
          <pre className="mt-1.5 max-h-40 overflow-auto rounded bg-surface px-2 py-1.5 font-mono text-2xs text-muted">{JSON.stringify(input, null, 2)}</pre>
        ) : null}
        {description && <div className="mt-1.5 text-xs text-faint">{description}</div>}
      </div>

      {err && <div className="mt-2 text-xs text-error">{err}</div>}

      <div className="mt-3 flex flex-wrap justify-end gap-2">
        <Button variant="danger" size="sm" onClick={() => decide("deny")} disabled={busy != null}><X className="size-3.5" /> Deny</Button>
        <Button variant="outline" size="sm" onClick={() => decide("always")} disabled={busy != null}><ShieldCheck className="size-3.5" /> Always allow</Button>
        <Button size="sm" onClick={() => decide("allow")} disabled={busy != null}><Check className="size-3.5" /> Allow</Button>
      </div>
    </div>
  );
}
