"use client";

import { useState } from "react";
import { ThemeProvider } from "next-themes";
import { QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/misc";
import { Toaster } from "@/components/ui/toast";
import { makeQueryClient } from "@/lib/queries";

export function Providers({ children }: { children: React.ReactNode }) {
  // One QueryClient per browser session (stable across re-renders).
  const [queryClient] = useState(makeQueryClient);
  return (
    <ThemeProvider attribute="class" defaultTheme="dark" enableSystem={false} disableTransitionOnChange>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider delayDuration={200}>{children}</TooltipProvider>
        <Toaster />
      </QueryClientProvider>
    </ThemeProvider>
  );
}
