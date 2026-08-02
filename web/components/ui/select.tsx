"use client";

import * as React from "react";
import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { FieldControlContext } from "@/components/ui/input";

const Select = SelectPrimitive.Root;
const SelectValue = SelectPrimitive.Value;

const SelectTrigger = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Trigger>
>(({ className, children, id, "aria-describedby": describedBy, ...props }, ref) => {
  const field = React.useContext(FieldControlContext);
  return (
    <SelectPrimitive.Trigger
      ref={ref}
      id={id ?? field.id}
      aria-describedby={describedBy ?? field.describedBy}
      className={cn(
        "flex h-9 w-full items-center justify-between rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-text shadow-soft outline-none transition-colors focus:ring-2 focus:ring-accent disabled:opacity-50 [&>span]:line-clamp-1 data-[placeholder]:text-faint",
        className,
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon asChild>
        <ChevronDown className="size-4 text-faint" />
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  );
});
SelectTrigger.displayName = "SelectTrigger";

const SelectContent = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Content>
>(({ className, children, position = "popper", ...props }, ref) => (
  <SelectPrimitive.Portal>
    <SelectPrimitive.Content
      ref={ref}
      position={position}
      className={cn(
        "relative z-50 max-h-72 min-w-[8rem] overflow-hidden rounded-lg border border-border bg-panel text-text shadow-pop",
        position === "popper" && "data-[side=bottom]:translate-y-1",
        className,
      )}
      {...props}
    >
      <SelectPrimitive.Viewport className="max-h-72 p-1">{children}</SelectPrimitive.Viewport>
    </SelectPrimitive.Content>
  </SelectPrimitive.Portal>
));
SelectContent.displayName = "SelectContent";

const SelectItem = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Item>
>(({ className, children, ...props }, ref) => (
  <SelectPrimitive.Item
    ref={ref}
    className={cn(
      "relative flex w-full cursor-pointer select-none items-center rounded-md py-1.5 pl-8 pr-2 text-sm outline-none focus:bg-surface-2 data-[disabled]:opacity-50",
      className,
    )}
    {...props}
  >
    <span className="absolute left-2 flex size-4 items-center justify-center">
      <SelectPrimitive.ItemIndicator><Check className="size-4 text-accent" /></SelectPrimitive.ItemIndicator>
    </span>
    <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
  </SelectPrimitive.Item>
));
SelectItem.displayName = "SelectItem";

export { Select, SelectValue, SelectTrigger, SelectContent, SelectItem };
