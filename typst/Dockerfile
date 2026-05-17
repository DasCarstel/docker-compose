FROM node:20-alpine AS base

FROM base AS deps
WORKDIR /app
RUN apk add --no-cache git
RUN git clone --depth 1 https://github.com/Mapaor/typst-online-editor.git .
RUN npm ci

FROM base AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app .
RUN printf '%s\n' 'import type { NextConfig } from "next";' '' 'const nextConfig: NextConfig = {' '  reactCompiler: true,' '  output: "standalone",' '};' '' 'export default nextConfig;' > next.config.ts
RUN npm run build

FROM base AS run
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/public ./public
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
