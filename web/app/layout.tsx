import type { Metadata } from "next";
import "./globals.css";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import { Providers } from "@/components/providers";

export const metadata: Metadata = {
  title: "Terrarium",
  description: "Mission control for sandboxed Claude agents",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning className={`${GeistSans.variable} ${GeistMono.variable}`}>
      <body className="min-h-screen bg-bg font-sans text-text antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
