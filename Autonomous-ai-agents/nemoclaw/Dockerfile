# NemoClaw — NVIDIA OpenShell security wrapper for OpenClaw
# Multi-stage build: install NemoClaw CLI + Python agent server
FROM node:22-slim AS openscell-builder

WORKDIR /build

# Install NemoClaw CLI (includes OpenShell runtime)
RUN npm install -g nemoclaw@latest && \
    npm install -g openshell@latest && \
    # Verify install
    nemoclaw --version && \
    openscell --version

# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS agent-builder

WORKDIR /app

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl git && \
    rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS final

# Install runtime deps (Node.js for OpenShell gateway)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm curl && \
    rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd -r -g 1000 nemoclaw && \
    useradd -r -u 1000 -g 1000 -m -s /sbin/nologin nemoclaw

WORKDIR /app

# Copy Python deps from builder
COPY --from=agent-builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=agent-builder /usr/local/bin /usr/local/bin

# Copy OpenShell/NemoClaw CLI from builder
COPY --from=openscell-builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=openscell-builder /usr/local/bin/nemoclaw /usr/local/bin/nemoclaw
COPY --from=openscell-builder /usr/local/bin/openscell /usr/local/bin/openscell

# Copy application
COPY app/ /app/

# Workspace directory (PVC will mount here)
RUN mkdir -p /workspace/memory /workspace/logs /workspace/blueprint-state && \
    mkdir -p /run/openscell && \
    chown -R 1000:1000 /workspace /run/openscell /app

USER 1000

EXPOSE 8000 5000 12010

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
