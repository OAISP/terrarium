# Dependency holds

Two dev dependencies are deliberately held back. `bun outdated` will keep listing them.

| Package | Held at | Latest | Why |
|---|---|---|---|
| `@types/node` | 22.x | 26.x | Must match the runtime, and `web/Dockerfile` runs `node:22-slim`. Typing against Node 26 APIs the container doesn't have is a false green: `tsc` passes, production throws. Bump this **with** the base image, never before it. |
| `eslint` | 9.x | 10.x | `eslint-plugin-react` (pulled in by `eslint-config-next`) calls the removed `context.getFilename()` API, so every lint run dies with `TypeError: contextOrFilename.getFilename is not a function` before reporting a single finding. Nothing in this repo can fix that; it needs a plugin release. Retry after `eslint-config-next` ships an eslint-10-compatible dependency tree. |

`typescript` is on 6.x; 7.x is the native port and Next.js 16 has not declared support. Not
attempted — a compiler swap is its own change with its own verification, not a line in a
dependency bump.
