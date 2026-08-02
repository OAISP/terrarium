"use client";

import * as React from "react";
import { HelpCircle, ChevronDown } from "lucide-react";
import { Collapsible, CollapsibleTrigger, CollapsibleContent } from "@/components/ui/collapsible";

/**
 * A folded explainer.
 *
 * The console's habit was to teach its security model in standing banners — permanent tutorial
 * prose above the actual data, re-read never but re-rendered always, on a page the operator visits
 * daily. That text is worth keeping and not worth showing: it explains a guarantee that is always
 * true and never changes what you do on the page.
 *
 * So it is a footnote you can open: one quiet line closed, the whole explanation open.
 * Default-closed, because by the second visit you already know.
 */
export function HelpNote({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Collapsible className="group/help">
      <CollapsibleTrigger className="flex items-center gap-1.5 rounded-md text-2xs text-faint outline-none transition-colors hover:text-muted">
        <HelpCircle className="size-3" />
        {label}
        <ChevronDown className="size-3 transition-transform group-data-[state=open]/help:rotate-180" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <p className="mt-1.5 max-w-prose rounded-lg border border-border bg-surface-2 px-3 py-2 text-xs leading-relaxed text-muted">
          {children}
        </p>
      </CollapsibleContent>
    </Collapsible>
  );
}
