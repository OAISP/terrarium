"use client";

import * as React from "react";
import * as SeparatorPrimitive from "@radix-ui/react-separator";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

const Separator = React.forwardRef<
  React.ElementRef<typeof SeparatorPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SeparatorPrimitive.Root>
>(({ className, orientation = "horizontal", decorative = true, ...props }, ref) => (
  <SeparatorPrimitive.Root
    ref={ref}
    decorative={decorative}
    orientation={orientation}
    className={cn("shrink-0 bg-border", orientation === "horizontal" ? "h-px w-full" : "h-full w-px", className)}
    {...props}
  />
));
Separator.displayName = "Separator";

const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root>
>(({ className, ...props }, ref) => (
  <SwitchPrimitive.Root
    ref={ref}
    className={cn(
      "peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 data-[state=checked]:bg-accent data-[state=unchecked]:bg-surface-3",
      className,
    )}
    {...props}
  >
    <SwitchPrimitive.Thumb className="pointer-events-none block size-4 rounded-full bg-surface shadow-soft transition-transform data-[state=checked]:translate-x-4 data-[state=unchecked]:translate-x-0" />
  </SwitchPrimitive.Root>
));
Switch.displayName = "Switch";

function Skeleton({ className }: { className?: string }) {
  return (
    <div className={cn("relative overflow-hidden rounded-lg bg-surface-2", className)}>
      <div className="absolute inset-0 -translate-x-full animate-[terra-shimmer_1.5s_infinite] bg-gradient-to-r from-transparent via-[oklch(1_0_0/0.04)] to-transparent" />
    </div>
  );
}

const TooltipProvider = TooltipPrimitive.Provider;

function Tooltip({ children, label, side = "top" }: { children: React.ReactNode; label: React.ReactNode; side?: "top" | "right" | "bottom" | "left" }) {
  return (
    <TooltipPrimitive.Root delayDuration={200}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className="z-50 max-w-xs rounded-md border border-border bg-panel px-2.5 py-1.5 text-xs text-text shadow-pop"
        >
          {label}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

/** Ghost copy-to-clipboard button with a brief success tick. */
function CopyButton({ value, label = "Copy", className }: { value: string; label?: string; className?: string }) {
  const [copied, setCopied] = React.useState(false);
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  React.useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  return (
    <button
      type="button"
      aria-label={label}
      onClick={(e) => {
        e.stopPropagation();
        navigator.clipboard?.writeText(value);
        setCopied(true);
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(false), 1400);
      }}
      className={cn(
        "grid size-6 place-items-center rounded-md text-faint outline-none transition-colors hover:bg-surface-2 hover:text-text",
        className,
      )}
    >
      {copied ? <Check className="size-3.5 text-agent" /> : <Copy className="size-3.5" />}
    </button>
  );
}

export { Separator, Switch, Skeleton, Tooltip, TooltipProvider, CopyButton };
