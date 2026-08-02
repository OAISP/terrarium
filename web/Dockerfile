# Web console image — Next.js standalone server.
# Build context is web/:  docker build -f web/Dockerfile -t terrarium-console web
FROM oven/bun:1 AS build
WORKDIR /app
COPY package.json bun.lock* ./
RUN bun install --frozen-lockfile
COPY . .
RUN bun run build

FROM node:22-slim AS run
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3737 \
    HOSTNAME=0.0.0.0
# Next standalone output: server.js + traced node_modules, plus static + public.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
EXPOSE 3737
USER 1000
CMD ["node", "server.js"]
