"use client";

import { useState } from "react";
import { KeyRound, Loader2, Check, Trash2, ShieldCheck, ShieldAlert } from "lucide-react";
import { setCredentials, clearCredentials, type CredStatus } from "@/lib/api";
import { qk, useCredStatus, useInvalidate } from "@/lib/queries";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/input";
import { ErrorBox } from "@/components/ui/feedback";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";

function human(s?: number | null) {
  if (s == null) return "";
  if (s < 60) return "<1m";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function CredentialBadge() {
  const { data: st } = useCredStatus(); // polls every 30s, pauses on a hidden tab
  const invalidate = useInvalidate();
  const [open, setOpen] = useState(false);

  if (!st || !st.managed) return null; // not subscription mode → nothing to show

  const state = !st.present ? "missing" : st.valid ? "ok" : "expired";
  const color = state === "ok" ? "var(--c-agent)" : state === "expired" ? "var(--c-error)" : "var(--c-result)";
  const label = state === "ok" ? `creds · ${human(st.expires_in_s)}` : state === "expired" ? "creds expired" : "set credentials";

  return (
    <>
      <button onClick={() => setOpen(true)}
        className="flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-2xs font-medium transition-[filter] hover:brightness-110"
        style={{ color, background: `color-mix(in oklch, ${color} 12%, transparent)`, borderColor: `color-mix(in oklch, ${color} 26%, var(--border))` }}
        title="Sandbox subscription credential">
        <KeyRound className="size-3.5" /> {label}
      </button>
      <CredDialog open={open} onOpenChange={setOpen} status={st} onChanged={() => invalidate(qk.credStatus)} />
    </>
  );
}

function CredDialog({ open, onOpenChange, status, onChanged }: {
  open: boolean; onOpenChange: (o: boolean) => void; status: CredStatus; onChanged: () => void;
}) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function save() {
    const t = token.trim();
    if (!t || busy) return;
    setBusy(true); setErr(null);
    try { await setCredentials(t); setToken(""); setSaved(true); setTimeout(() => setSaved(false), 1500); onChanged(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  async function clear() {
    setBusy(true); setErr(null);
    try { await clearCredentials(); onChanged(); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  const ok = status.present && status.valid;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Sandbox credentials</DialogTitle>
          <DialogDescription>
            Paste the contents of the dedicated <span className="font-mono">~/.claude/.credentials.json</span>. It&apos;s
            stored only on the orchestrator and refreshed automatically, and never shown again.
          </DialogDescription>
        </DialogHeader>

        <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 p-3 text-xs">
          {ok ? <ShieldCheck className="size-4 text-agent" /> : <ShieldAlert className="size-4 text-result" />}
          {status.present ? (
            <div className="flex-1">
              <span style={{ color: status.valid ? "var(--c-agent)" : "var(--c-error)" }} className="font-medium">
                {status.valid ? `Valid · expires in ${human(status.expires_in_s)}` : "Expired"}
              </span>
              {status.subscription_type && <span className="text-muted"> · {status.subscription_type}</span>}
              {status.last_refresh && <span className="text-faint"> · refreshed {new Date(status.last_refresh).toLocaleString()}</span>}
              {status.last_error && <div className="mt-0.5 text-result">last error: {status.last_error}</div>}
            </div>
          ) : <span className="text-muted">No credential set · sandboxes can&apos;t authenticate until you add one.</span>}
        </div>

        <Textarea rows={5} value={token} onChange={(e) => setToken(e.target.value)} autoFocus
          placeholder={'{"claudeAiOauth":{"accessToken":"…","refreshToken":"…","expiresAt":…}}'} className="font-mono text-xs" />

        {err && <ErrorBox>{err}</ErrorBox>}

        <div className="flex items-center justify-between">
          <Button variant="danger" size="sm" onClick={clear} disabled={busy || !status.present}><Trash2 className="size-3.5" /> Clear</Button>
          <Button size="sm" onClick={save} disabled={busy || !token.trim()}>
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : saved ? <Check className="size-3.5" /> : <KeyRound className="size-3.5" />} Save
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
