"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * One scroll/width/rhythm shell for every view, so content width and vertical spacing
 * stay consistent. "wide" for list/grid views, "narrow" for forms/settings.
 */
export function PageContainer({
  width = "wide",
  children,
  className,
}: {
  width?: "wide" | "narrow";
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className="h-full overflow-y-auto">
      <div className={cn("mx-auto space-y-4", width === "narrow" ? "max-w-3xl" : "max-w-6xl", className)}>
        {children}
      </div>
    </div>
  );
}
