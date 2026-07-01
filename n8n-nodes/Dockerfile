FROM n8nio/n8n:latest

USER root

WORKDIR /opt/custom-nodes/n8n-nodes
COPY package.json package-lock.json* ./
RUN npm install --include=dev

COPY . .
RUN npm run build 2>/dev/null || true

ENV N8N_CUSTOM_EXTENSIONS=/opt/custom-nodes

USER node
