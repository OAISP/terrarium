"use client";

import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { cn } from "@/lib/utils";

// Syntax highlighting via highlight.js (theme tokens live in globals.css under .hljs).
// `detect` highlights fenced blocks even without a language hint; `ignoreMissing` keeps a
// mid-stream block with an unknown language from throwing.
const REMARK = [remarkGfm];
const REHYPE = [[rehypeHighlight, { detect: true, ignoreMissing: true }]];

/** Render GFM markdown with the shared `.prose-chat` styling (headings, lists, tables,
 *  syntax-highlighted code, links). One place so agent text, subagent output, etc. all look
 *  identical. */
export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn("prose-chat", className)}>
      {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
      <ReactMarkdown remarkPlugins={REMARK} rehypePlugins={REHYPE as any}>{children}</ReactMarkdown>
    </div>
  );
}
