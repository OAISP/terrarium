import next from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

// Next.js's recommended flat config (core-web-vitals + typescript). Kept intentionally
// small — the codebase is already clean, so this is a guardrail against regressions, not a
// churn engine. eslint-config-next@16 ships a native flat config, so no FlatCompat shim.
const config = [
  ...next,
  ...nextTs,
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  {
    rules: {
      // Next 16 ships this new rule as an error. The codebase's synchronous setState calls
      // in effects are deliberate (resetting derived state when a key like sessionId flips,
      // reduced-motion snap) — keep it as a warning so it surfaces without failing the gate.
      "react-hooks/set-state-in-effect": "warn",
    },
  },
];

export default config;
