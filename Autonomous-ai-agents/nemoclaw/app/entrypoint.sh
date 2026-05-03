#!/bin/sh
set -e

# Source Vault-injected secrets if present
if [ -f "${VAULT_SECRETS_FILE:-/vault/secrets/env}" ]; then
    . "${VAULT_SECRETS_FILE:-/vault/secrets/env}"
fi

# Ensure workspace dirs
mkdir -p "${WORKSPACE_DIR:-/workspace}/logs" "${WORKSPACE_DIR:-/workspace}/blueprint-state"

# Wait for OpenShell gateway socket to be ready (started by sidecar container)
echo "[nemoclaw] Waiting for OpenShell gateway..."
MAX_WAIT=60
WAITED=0
while [ ! -S /run/openscell/channel.sock ] && [ "$WAITED" -lt "$MAX_WAIT" ]; do
    sleep 2
    WAITED=$((WAITED + 2))
done

if [ ! -S /run/openscell/channel.sock ]; then
    echo "[nemoclaw] WARNING: OpenShell gateway socket not found after ${MAX_WAIT}s"
    echo "[nemoclaw] Continuing without OpenShell channel (degraded mode)"
else
    echo "[nemoclaw] OpenShell gateway ready"
fi

# Start AgentScope Studio in background
agentscope studio &
STUDIO_PID=$!
echo "[nemoclaw] AgentScope Studio started (pid=${STUDIO_PID})"

# Start FastAPI as PID 1 (exec — receives SIGTERM from K8s)
echo "[nemoclaw] Starting NemoClaw API server on :${API_PORT:-8000}"
exec uvicorn main:app \
    --host "${API_HOST:-0.0.0.0}" \
    --port "${API_PORT:-8000}" \
    --workers 1 \
    --log-level info \
    --access-log
