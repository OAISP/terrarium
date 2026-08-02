import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-2xs font-medium",
  {
    variants: {
      variant: {
        default: "border-[color-mix(in_oklch,var(--accent)_28%,var(--border))] bg-[color-mix(in_oklch,var(--accent)_13%,transparent)] text-accent",
        secondary: "border-border bg-surface-2 text-text font-mono",
        outline: "border-border text-muted",
        muted: "border-transparent bg-surface-2 text-muted",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
