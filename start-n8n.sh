#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NODES_DIR="$SCRIPT_DIR/n8n-nodes"

# n8n may be installed under ~/.npm-global
export PATH="$HOME/.npm-global/bin:$PATH"

# Check n8n
if ! command -v n8n &>/dev/null; then
  echo "Installing n8n globally..."
  npm config set prefix "$HOME/.npm-global"
  npm install -g n8n --cache /tmp/npm-cache
fi

# Build custom nodes if dist/ is missing or source is newer
if [ ! -d "$NODES_DIR/dist" ] || \
   find "$NODES_DIR/nodes" "$NODES_DIR/credentials" -newer "$NODES_DIR/dist" -name "*.ts" | grep -q .; then
  echo "Building botrassilka nodes..."
  (cd "$NODES_DIR" && npm run build)
fi

# n8n scans N8N_CUSTOM_EXTENSIONS recursively, so point it to a clean
# directory that contains ONLY our package — not the whole project tree.
CUSTOM_EXT_DIR="$SCRIPT_DIR/.n8n-extensions"
mkdir -p "$CUSTOM_EXT_DIR"
# Symlink our package into the clean dir (idempotent)
ln -sfn "$NODES_DIR" "$CUSTOM_EXT_DIR/n8n-nodes"

export N8N_CUSTOM_EXTENSIONS="$CUSTOM_EXT_DIR"
export N8N_PORT="${N8N_PORT:-5678}"
export N8N_PROTOCOL="http"
export N8N_HOST="localhost"
export N8N_EDITOR_BASE_URL="http://localhost:${N8N_PORT}"

# Disable telemetry
export N8N_DIAGNOSTICS_ENABLED=false
export N8N_VERSION_NOTIFICATIONS_ENABLED=false

# Allow HTTP requests to localhost from workflows
export N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES=false
export NODES_INCLUDE=
export N8N_RUNNERS_ENABLED=false

echo ""
echo "  n8n starting at http://localhost:${N8N_PORT}"
echo "  Botrassilka nodes loaded from: $NODES_DIR"
echo ""

n8n start
