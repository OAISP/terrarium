"use client";

// A Button that runs an async action with a built-in pending state (spinner +
// disabled, so it can't be double-fired) and routes failures to a toast. Replaces
// the per-view busy/err/try-catch boilerplate and the silent-catch + window.alert
// patterns, so every action gives consistent feedback.

import * as React from "react";
import { Loader2 } from "lucide-react";
import { Button, type ButtonProps } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";

export interface AsyncButtonProps extends Omit<ButtonProps, "onClick"> {
  onClick: () => Promise<unknown> | unknown;
  onDone?: () => void; // runs after success (close a dialog, invalidate a query…)
  pendingLabel?: string;
  errorToast?: boolean; // default true — surface a failure as a toast
}

export function AsyncButton({
  onClick,
  onDone,
  pendingLabel,
  errorToast = true,
  children,
  disabled,
  ...rest
}: AsyncButtonProps) {
  const [busy, setBusy] = React.useState(false);
  const mounted = React.useRef(true);
  React.useEffect(() => () => { mounted.current = false; }, []);

  async function run() {
    if (busy) return;
    setBusy(true);
    try {
      await onClick();
      onDone?.();
    } catch (e) {
      if (errorToast) toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  return (
    <Button {...rest} disabled={disabled || busy} onClick={run} aria-busy={busy}>
      {busy && <Loader2 className="animate-spin" />}
      {busy && pendingLabel ? pendingLabel : children}
    </Button>
  );
}
