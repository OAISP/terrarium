"use client";

import { useState } from "react";
import { KeyRound, Loader2, ArrowRight, ServerCrash } from "lucide-react";
import { LogoBadge } from "./Logo";
import { login } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export function LoginForm({ onSuccess, reachable = true }: { onSuccess: () => void; reachable?: boolean }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const t = token.trim();
    if (!t || busy) return;
    setBusy(true); setError(null);
    const r = await login(t);
    if (r.ok) onSuccess();
    else { setError(r.error ?? "Sign-in failed."); setBusy(false); }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg p-4">
      <div className="w-full max-w-sm rounded-2xl border border-border bg-panel p-7 shadow-pop">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <LogoBadge size={48} />
          <div>
            <h1 className="text-lg font-bold tracking-tight">Terrarium</h1>
            <p className="text-xs text-muted">Enter your access token to continue.</p>
          </div>
        </div>

        {!reachable && (
          <div className="mb-3 flex items-center gap-2 rounded-lg border p-2.5 text-xs"
            style={{ background: "color-mix(in oklch,var(--c-error) 10%,transparent)", borderColor: "color-mix(in oklch,var(--c-error) 30%,var(--border))", color: "var(--c-error)" }}>
            <ServerCrash className="size-4 shrink-0" /> Orchestrator unreachable. Sign-in will retry once it&apos;s back.
          </div>
        )}

        <form onSubmit={submit} className="space-y-3">
          <div className="relative">
            <KeyRound className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-faint" />
            <Input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoFocus
              placeholder="TERRA_TOKEN" autoComplete="off" className="pl-9 font-mono" />
          </div>
          {error && (
            <div className="rounded-lg border p-2.5 text-xs"
              style={{ background: "color-mix(in oklch,var(--c-error) 10%,transparent)", borderColor: "color-mix(in oklch,var(--c-error) 30%,var(--border))", color: "var(--c-error)" }}>
              {error}
            </div>
          )}
          <Button type="submit" className="w-full" disabled={busy || !token.trim()}>
            {busy ? <Loader2 className="size-4 animate-spin" /> : <ArrowRight className="size-4" />} Sign in
          </Button>
        </form>

        <p className="mt-5 text-center text-2xs text-faint">Sign in with the orchestrator&apos;s bearer token (TERRA_TOKEN).</p>
      </div>
    </div>
  );
}
