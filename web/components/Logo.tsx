import * as React from "react";

/**
 * Terrarium brand mark ("layered cradle" — nested isolation shells around an emerging
 * sprout, with a teal inner ring echoing --c-tool). Theme-adaptive: colours follow the
 * accent/agent/tool tokens, so it darkens for contrast in the light theme. Colours are set
 * via `style` (CSS custom properties don't resolve in SVG presentation attributes).
 * The standalone favicon (app/icon.svg) keeps its own fixed palette.
 */
// The mark's optical centre is NOT the middle of its 64×64 grid. Measured from the path
// geometry — including stroke half-widths and round caps — the ink spans y 17.09..57.20, so
// it centres on 37.14, over five units below the box centre. (x spans 11.80..52.20 and
// centres on exactly 32, so only y needs correcting.) Rendered in a plain `0 0 64 64`
// viewBox the mark therefore sits low inside any badge that centres it. Shifting the viewBox
// window down by the same amount re-centres it without touching a path coordinate.
// app/icon.svg encodes the identical constant; keep the two in step.
const ART_CENTER_Y = 37.14;
const VIEWBOX = `0 ${ART_CENTER_Y - 32} 64 64`;

export function Logo({ size = 28, title = "Terrarium", className, ...props }: { size?: number; title?: string; className?: string } & React.SVGProps<SVGSVGElement>) {
  const gold = { stroke: "var(--accent)" } as const;
  return (
    <svg width={size} height={size} viewBox={VIEWBOX} fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label={title} className={className} {...props}>
      <path d="M13 37a19 19 0 0 0 38 0" style={gold} strokeWidth="2.4" strokeLinecap="round" />
      <path d="M20 37a12 12 0 0 0 24 0" style={gold} strokeWidth="2.1" strokeLinecap="round" opacity="0.78" />
      <path d="M26.5 37a5.5 5.5 0 0 0 11 0" style={{ stroke: "var(--c-tool)" }} strokeWidth="2" strokeLinecap="round" opacity="0.9" />
      <path d="M32 37V20.5" style={gold} strokeWidth="2.3" strokeLinecap="round" />
      <path d="M32 27c-.7-4.7-3.9-7.1-8.4-6.9.2 4.5 3.6 7.1 8.4 6.9Z" style={{ fill: "var(--accent)" }} />
      <path d="M32 24c.7-4.7 3.9-7.1 8.4-6.9-.2 4.5-3.6 7.1-8.4 6.9Z" style={{ fill: "var(--c-agent)" }} />
    </svg>
  );
}

/** The brand mark in a rounded, theme-adaptive tile with a faint accent rim — the app badge.
 *
 *  Centring lives on an INNER element on purpose. `className` is merged into the outer span so
 *  callers can position and responsively hide the badge, and any display utility they pass —
 *  `md:block`, `md:flex`, `inline-block` — would silently defeat a `place-items-center` sitting
 *  on that same element. That is not hypothetical: `hidden md:block` in the dock turned the
 *  tile into a block box and dropped the glyph into its top-left corner. The outer span owns
 *  the box and stays clobberable; the inner one owns the centring and cannot be reached. */
export function LogoBadge({ size = 44, className, title = "Terrarium" }: { size?: number; className?: string; title?: string }) {
  return (
    <span
      className={`flex-none overflow-hidden rounded-xl ${className ?? ""}`}
      style={{ width: size, height: size, background: "var(--bg)", border: "1px solid color-mix(in oklch, var(--accent) 30%, transparent)" }}
      title={title}
    >
      <span className="grid size-full place-items-center">
        <Logo size={Math.round(size * 0.74)} title={title} />
      </span>
    </span>
  );
}
