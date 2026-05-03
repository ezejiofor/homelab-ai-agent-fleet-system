#!/usr/bin/env bash
# Start AgentScope Studio in background, then start FastAPI server
set -euo pipefail
echo "[openclaw] Starting AgentScope Studio on port ${STUDIO_PORT:-5000}..."
agentscope studio --host 0.0.0.0 --port "${STUDIO_PORT:-5000}" &
echo "[openclaw] Starting OpenClaw API server on port ${API_PORT:-8000}..."
exec uvicorn main:app \
  --host 0.0.0.0 \
  --port "${API_PORT:-8000}" \
  --workers 1 \
  --log-level "${LOG_LEVEL:-info}" \
  --access-log
