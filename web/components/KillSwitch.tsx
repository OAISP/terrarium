"use client";

import { useState } from "react";
import { Ban } from "lucide-react";
import { setEgressPolicy } from "@/lib/api";
import { qk, useEgressPolicy, useInvalidate } from "@/lib/queries";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

/**
 * The panic button, in the global header.
 *
 * It sits beside ⌘K, reachable from every view: a kill switch you have to navigate to before you
 * can cut every agent's network is not a kill switch.
 *
 * Self-contained (reads its own policy, owns its own confirm) because the Hud is rendered by the
 * console shell, not by the Egress view.
 */
export function KillSwitch() {
  const policyQ = useEgressPolicy();
  const invalidate = useInvalidate();
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const policy = policyQ.data ?? null;

  // Nothing to freeze if the policy hasn't loaded — don't render a control that can't act.
  if (!policy) return null;
  const frozen = !!policy.kill;

  async function toggle() {
    setBusy(true);
    // Let it throw: ConfirmDialog only closes on success, so a failed freeze never reads as "done".
    try { await setEgressPolicy({ kill: !frozen }); invalidate(qk.egressPolicy); }
    finally { setBusy(false); }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => (frozen ? toggle() : setConfirm(true))}
        disabled={busy}
        // Frozen is a state the operator must never miss, so it shouts; armed-and-ready stays quiet
        // and matches the ⌘K button beside it rather than competing with it.
        className="flex flex-none items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-2xs font-medium transition-colors disabled:opacity-60"
        style={frozen
          ? { borderColor: "var(--c-error)", background: "color-mix(in oklch, var(--c-error) 16%, transparent)", color: "var(--c-error)" }
          : { borderColor: "var(--border)", background: "var(--surface-2)", color: "var(--muted)" }}
        title={frozen ? "All egress is frozen. Click to resume." : "Freeze all egress instantly (overrides every profile)"}
        aria-label={frozen ? "Egress frozen. Resume egress" : "Freeze all egress"}
      >
        {frozen
          ? <span className="size-2 flex-none rounded-full bg-error motion-safe:animate-[terra-breathe_1.4s_ease-in-out_infinite]" aria-hidden />
          : <Ban className="size-3.5" />}
        <span className="hidden sm:inline">{busy ? "…" : frozen ? "FROZEN" : "Freeze"}</span>
      </button>

      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title="Freeze ALL egress now?"
        description="Every live session loses network access, including Anthropic, until you resume."
        confirmLabel="Freeze egress"
        destructive
        onConfirm={toggle}
      />
    </>
  );
}
