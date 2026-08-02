"use client";

import * as React from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { AsyncButton } from "@/components/ui/async-button";

/**
 * One confirm-before-acting dialog for the whole console — replaces both the
 * bespoke per-view confirm dialogs and the native window.confirm() calls. The
 * confirm button runs the action with a pending state and toasts on failure.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  destructive,
  extra,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: string;
  description?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  extra?: React.ReactNode; // e.g. a "purge memory" checkbox
  onConfirm: () => Promise<unknown> | unknown;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        {extra}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>{cancelLabel}</Button>
          <AsyncButton
            variant={destructive ? "danger" : "default"}
            onClick={onConfirm}
            onDone={() => onOpenChange(false)}
            pendingLabel="Working…"
          >
            {confirmLabel}
          </AsyncButton>
        </div>
      </DialogContent>
    </Dialog>
  );
}
