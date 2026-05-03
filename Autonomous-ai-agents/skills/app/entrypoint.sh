#!/bin/sh
set -e
if [ -f "${VAULT_SECRETS_FILE:-/vault/secrets/env}" ]; then
    . "${VAULT_SECRETS_FILE:-/vault/secrets/env}"
fi
mkdir -p "${SKILLS_DIR:-/skills}/catalog" "${SKILLS_DIR:-/skills}/audit"          "${SKILLS_DIR:-/skills}/versions" "${SKILLS_DIR:-/skills}/drafts"          "${SKILLS_DIR:-/skills}/approved"
echo "[skillshub] Starting SkillsHub on :${API_PORT:-8000}"
exec uvicorn main:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8000}" --workers 1 --log-level info
