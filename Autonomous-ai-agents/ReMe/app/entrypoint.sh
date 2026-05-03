#!/bin/sh
set -e

if [ -f "${VAULT_SECRETS_FILE:-/vault/secrets/env}" ]; then
    . "${VAULT_SECRETS_FILE:-/vault/secrets/env}"
fi

mkdir -p "${WORKSPACE_DIR:-/workspace}/memory" \
         "${WORKSPACE_DIR:-/workspace}/logs" \
         "${WORKSPACE_DIR:-/workspace}/exports"

agentscope studio &
echo "[reme] AgentScope Studio started"

echo "[reme] Starting ReMe API server on :${API_PORT:-8000}"
exec uvicorn main:app \
    --host "${API_HOST:-0.0.0.0}" \
    --port "${API_PORT:-8000}" \
    --workers 1 \
    --log-level info \
    --access-log
