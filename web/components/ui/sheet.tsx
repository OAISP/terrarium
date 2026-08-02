"use client";

import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

// A right-side drawer built on Radix Dialog. Used for drill-down detail (e.g. inspecting a
// single subagent) without pushing the conversation around or losing scroll position.

const Sheet = DialogPrimitive.Root;
const SheetTrigger = DialogPrimitive.Trigger;
const SheetClose = DialogPrimitive.Close;

const SheetContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>
>(({ className, children, ...props }, ref) => (
  <DialogPrimitive.Portal>
    <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-[oklch(0_0_0/0.45)] backdrop-blur-[2px] data-[state=open]:animate-[terra-in_0.2s_ease]" />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        "fixed inset-y-0 right-0 z-50 flex w-[min(560px,94vw)] flex-col gap-4 overflow-y-auto border-l border-border bg-panel p-6 shadow-pop",
        "data-[state=open]:animate-[sheet-in_0.24s_cubic-bezier(0.2,0.7,0.3,1)]",
        className,
      )}
      {...props}
    >
      {children}
      <DialogPrimitive.Close className="absolute right-4 top-4 rounded-md p-1 text-muted opacity-70 outline-none transition hover:bg-surface-2 hover:opacity-100">
        <X className="size-4" />
        <span className="sr-only">Close</span>
      </DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPrimitive.Portal>
));
SheetContent.displayName = "SheetContent";

const SheetTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title ref={ref} className={cn("text-base font-semibold tracking-tight", className)} {...props} />
));
SheetTitle.displayName = "SheetTitle";

const SheetDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description ref={ref} className={cn("text-sm text-muted", className)} {...props} />
));
SheetDescription.displayName = "SheetDescription";

export { Sheet, SheetTrigger, SheetClose, SheetContent, SheetTitle, SheetDescription };
