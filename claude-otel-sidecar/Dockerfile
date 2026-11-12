FROM node:22-alpine

WORKDIR /app

# No dependencies to install — the service is dependency-free Node.
COPY package.json ./
COPY src ./src
# Rollout assets served at GET /install.sh and GET /cc-usage-statusline.mjs
COPY scripts/install-hosted.sh scripts/cc-usage-statusline.mjs ./scripts/

ENV NODE_ENV=production \
    PORT=8080 \
    HOST=0.0.0.0

# Cloud Run sends SIGTERM on scale-to-zero; the sidecar flushes queues on it.
EXPOSE 8080

USER node

CMD ["node", "src/server.mjs"]
