"use client";

// One notifier for the whole console — replaces window.alert and the silent catches
// so every async result (success or failure) surfaces the same way. Import { toast }
// anywhere; render <Toaster/> once (in Providers).

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";

type Variant = "success" | "error" | "info";
type Toast = { id: number; message: string; variant: Variant };

let counter = 0;
let store: Toast[] = [];
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

function dismiss(id: number) {
  store = store.filter((t) => t.id !== id);
  emit();
}
function push(message: string, variant: Variant) {
  const id = ++counter;
  store = [...store, { id, message, variant }];
  emit();
  setTimeout(() => dismiss(id), variant === "error" ? 6000 : 3500);
}

export const toast = {
  success: (m: string) => push(m, "success"),
  error: (m: string) => push(m, "error"),
  info: (m: string) => push(m, "info"),
};

const TONE: Record<Variant, { color: string; Icon: React.ElementType }> = {
  success: { color: "var(--c-agent)", Icon: CheckCircle2 },
  error: { color: "var(--c-error)", Icon: AlertTriangle },
  info: { color: "var(--muted)", Icon: Info },
};

export function Toaster() {
  const toasts = React.useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
    () => store,
    () => store,
  );
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-[min(92vw,360px)] flex-col gap-2">
      <AnimatePresence initial={false}>
        {toasts.map((t) => {
          const { color, Icon } = TONE[t.variant];
          return (
            <motion.div
              key={t.id}
              layout
              initial={{ opacity: 0, y: 12, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: 0.98 }}
              transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
              role={t.variant === "error" ? "alert" : "status"}
              className="pointer-events-auto flex items-start gap-2.5 rounded-lg border bg-surface p-3 text-sm shadow-soft"
              style={{ borderColor: `color-mix(in oklch, ${color} 30%, var(--border))` }}
            >
              <Icon className="mt-px size-4 flex-none" style={{ color }} />
              <span className="min-w-0 flex-1 text-text">{t.message}</span>
              <button onClick={() => dismiss(t.id)} className="rounded p-0.5 text-faint outline-none transition-colors hover:text-text focus-visible:ring-2 focus-visible:ring-accent" aria-label="Dismiss">
                <X className="size-3.5" />
              </button>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}
