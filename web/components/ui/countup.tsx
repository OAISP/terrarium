"use client";

import { useEffect, useRef, useState } from "react";
import { useReducedMotion } from "framer-motion";

/** Lightweight count-up — animates from the previous value to `to` over ~0.5s. */
export function CountUp({ to, decimals = 0, duration = 0.5, prefix = "", suffix = "" }: {
  to: number; decimals?: number; duration?: number; prefix?: string; suffix?: string;
}) {
  const reduce = useReducedMotion();
  const [val, setVal] = useState(to);
  const fromRef = useRef(to);
  const raf = useRef<number | null>(null);

  useEffect(() => {
    // prefers-reduced-motion: don't animate at all. Keep the ref in step (a ref write, not a
    // setState) and render `to` directly below, so there's no state to synchronize.
    if (reduce) { fromRef.current = to; return; }
    const from = fromRef.current;
    if (from === to) return;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / (duration * 1000));
      const eased = 1 - Math.pow(1 - t, 3);
      setVal(from + (to - from) * eased);
      if (t < 1) raf.current = requestAnimationFrame(tick);
      else fromRef.current = to;
    };
    raf.current = requestAnimationFrame(tick);
    return () => { if (raf.current) cancelAnimationFrame(raf.current); };
  }, [to, duration, reduce]);

  const shown = reduce ? to : val;   // derived, so the reduced-motion path needs no state
  return <>{prefix}{shown.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}{suffix}</>;
}
