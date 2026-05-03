# ── Stage 1: Builder ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="OpenClaw"
LABEL org.opencontainers.image.description="General-purpose AgentScope autonomous AI agent"
LABEL org.opencontainers.image.source="https://github.com/georgehenderson/homelab-proxmox-k8s-terraform-ansible-gitops"

# Non-root user
RUN groupadd -r openclaw --gid=1000 && \
    useradd -r -g openclaw --uid=1000 --home-dir=/app openclaw

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ .

# Workspace (overridden by PVC mount in K8s)
RUN mkdir -p /workspace/memory /workspace/logs /workspace/tools && \
    chown -R openclaw:openclaw /workspace /app

USER openclaw

EXPOSE 8000 5000 12010

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

ENTRYPOINT ["/app/entrypoint.sh"]

