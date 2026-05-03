#!/bin/sh
set -e
if [ -f "${VAULT_SECRETS_FILE:-/vault/secrets/env}" ]; then
    . "${VAULT_SECRETS_FILE:-/vault/secrets/env}"
fi
mkdir -p "${WORKSPACE_DIR:-/workspace}/translations" "${WORKSPACE_DIR:-/workspace}/logs"
agentscope studio &
echo "[qwenpaw] Studio started"
exec uvicorn main:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8000}" --workers 1 --log-level info
