FROM python:3.12-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl git && rm -rf /var/lib/apt/lists/*
COPY app/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim AS final
RUN groupadd -r -g 1000 hiclaw && useradd -r -u 1000 -g 1000 -m -s /sbin/nologin hiclaw
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY app/ /app/
RUN mkdir -p /workspace/plans /workspace/logs && chown -R 1000:1000 /workspace /app
USER 1000
EXPOSE 8000 5000 12010
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD curl -f http://localhost:8000/health || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
