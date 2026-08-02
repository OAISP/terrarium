"use client";

import * as React from "react";
import { ChevronDown } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { tint, tintBorder } from "@/lib/tint";

/**
 * A tinted summary-header + collapsible-body shell with one chevron idiom — the recipe
 * behind the "Answered" (QuestionCard) and "decided" (PermissionCard) folded cards, which
 * were ~90% identical. The `header` fills the trigger row (lay out your own leading icon /
 * label / flex-1 summary); the chevron is appended and rotates on open. Body renders only
 * when `children` is provided.
 */
export function CollapsibleCard({
  tone = "var(--accent)",
  defaultOpen,
  open,
  onOpenChange,
  header,
  children,
  className,
  contentClassName,
}: {
  tone?: string;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  header: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
  contentClassName?: string;
}) {
  return (
    <Collapsible open={open} defaultOpen={defaultOpen} onOpenChange={onOpenChange}
      className={cn("group/cc my-2 overflow-hidden rounded-lg border", className)}
      style={{ borderColor: tintBorder(tone, 26) }}>
      <CollapsibleTrigger
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs outline-none transition-colors hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-accent"
        style={{ background: tint(tone, 5) }}>
        {header}
        <ChevronDown size={13} className="flex-none text-faint transition-transform group-data-[state=open]/cc:rotate-180" />
      </CollapsibleTrigger>
      {children != null && (
        <CollapsibleContent className={cn("border-t border-border bg-surface-2", contentClassName)}>
          {children}
        </CollapsibleContent>
      )}
    </Collapsible>
  );
}
