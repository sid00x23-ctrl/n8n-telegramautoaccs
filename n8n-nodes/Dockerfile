FROM n8nio/n8n:latest

USER root

WORKDIR /opt/custom-nodes/n8n-nodes
COPY package.json package-lock.json* ./
RUN npm install

COPY . .
RUN npm run build

ENV N8N_CUSTOM_EXTENSIONS=/opt/custom-nodes

USER node
