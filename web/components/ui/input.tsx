"use client";

import * as React from "react";
import * as LabelPrimitive from "@radix-ui/react-label";
import { cn } from "@/lib/utils";

const fieldBase =
  "flex w-full rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-text shadow-soft outline-none transition-colors placeholder:text-faint focus-visible:ring-2 focus-visible:ring-accent focus-visible:border-[color-mix(in_oklch,var(--accent)_50%,var(--border))] disabled:cursor-not-allowed disabled:opacity-50";

type FieldControl = { id?: string; describedBy?: string };
const FieldControlContext = React.createContext<FieldControl>({});

const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, type, id, "aria-describedby": describedBy, ...props }, ref) => {
    const field = React.useContext(FieldControlContext);
    return (
      <input
        type={type} ref={ref} id={id ?? field.id}
        aria-describedby={describedBy ?? field.describedBy}
        className={cn(fieldBase, "h-9", className)} {...props}
      />
    );
  },
);
Input.displayName = "Input";

const Textarea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, id, "aria-describedby": describedBy, ...props }, ref) => {
    const field = React.useContext(FieldControlContext);
    return (
      <textarea
        ref={ref} id={id ?? field.id}
        aria-describedby={describedBy ?? field.describedBy}
        className={cn(fieldBase, "min-h-[72px] resize-y", className)} {...props}
      />
    );
  },
);
Textarea.displayName = "Textarea";

const Label = React.forwardRef<
  React.ElementRef<typeof LabelPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof LabelPrimitive.Root>
>(({ className, ...props }, ref) => (
  <LabelPrimitive.Root ref={ref} className={cn("text-xs font-medium text-muted", className)} {...props} />
));
Label.displayName = "Label";

function Field({ label, hint, children, className }: { label?: string; hint?: React.ReactNode; children: React.ReactNode; className?: string }) {
  // Wire htmlFor/id + aria-describedby once here so every form control across the console
  // gets a programmatic label + hint association without per-call boilerplate.
  const autoId = React.useId();
  const hintId = hint ? `${autoId}-hint` : undefined;
  const isEl = React.isValidElement(children);
  const childProps = isEl ? (children.props as { id?: string; "aria-describedby"?: string }) : {};
  const controlId = childProps.id ?? (isEl ? autoId : undefined);
  const describedBy = [childProps["aria-describedby"], hintId].filter(Boolean).join(" ") || undefined;
  const child = isEl
    ? React.cloneElement(children as React.ReactElement<Record<string, unknown>>, { id: controlId, "aria-describedby": describedBy })
    : children;
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {label && <Label htmlFor={controlId}>{label}</Label>}
      <FieldControlContext.Provider value={{ id: controlId, describedBy }}>
        {child}
      </FieldControlContext.Provider>
      {hint && <span id={hintId} className="text-2xs text-faint">{hint}</span>}
    </div>
  );
}

export { Input, Textarea, Label, Field, FieldControlContext };
