# NemoClaw - NVIDIA OpenShell Security Wrapper for OpenClaw

> Part of the homelab Autonomous AI Agent fleet

## What is NemoClaw?

NemoClaw is NVIDIA's open-source reference stack that runs OpenClaw inside a hardened
NVIDIA OpenShell sandbox. It adds layered protection on top of the OpenClaw agent:

| Layer | Technology | What it does |
|-------|-----------|-------------|
| Filesystem isolation | Linux Landlock LSM | Restricts file-system access to declared paths only |
| Syscall filtering | seccomp | Custom allow-list drops 200+ dangerous syscalls |
| Network isolation | netns + egress proxy | All outbound traffic through OpenShell gateway only |
| Channel messaging | Unix socket | Host-sandbox comms via structured, size-limited messages |
| Blueprint lifecycle | Snapshot + restore | Versioned sandbox state; rollback on crash |
| Routed inference | NVIDIA NIM | API calls proxied - no raw key in agent process |

## Architecture

```
nemoclaw pod
|- initContainer: init-workspace       (mkdir /workspace/* dirs)
|- initContainer: validate-blueprint   (assert blueprint.yaml exists)
|- container: openscell-gateway        (NVIDIA OpenShell daemon - Landlock/seccomp/netns)
|   L unix socket: /run/openscell/channel.sock
L  container: nemoclaw                 (FastAPI + AgentScope - communicates via socket)
    |- POST /api/task   (async, audited)
    |- POST /api/chat   (sync)
    |- GET  /health
    L  GET  /metrics
```

Inference routing:
- Primary: NVIDIA NIM -> nvidia/nemotron-3-super-120b-a12b
- Fallback: Ollama (in-cluster) -> llama3.1:70b

## File Structure

```
nemoclaw/
|- Dockerfile           Multi-stage: Node22 (OpenShell CLI) + Python3.12 (agent)
|- kustomization.yaml
|- namespace.yaml       PSA restricted + Istio injection enabled
|- serviceaccount.yaml  Vault role: nemoclaw
|- rbac.yaml
|- configmap.yaml       blueprint.yaml + seccomp profile + env vars
|- secret.yaml          Placeholder (Vault Agent overwrites at runtime)
|- pvc.yaml             ceph-block 20Gi
|- deployment.yaml      openscell-gateway sidecar + nemoclaw agent
|- service.yaml         ClusterIP: 8000/5000/12010/12020
|- hpa.yaml             maxReplicas: 1 (RWO PVC)
|- virtualservice.yaml  Istio VS + AuthorizationPolicy + NetworkPolicy
L  app/
    |- main.py          FastAPI + AgentScope + audit log + blocked-pattern filter
    |- requirements.txt
    L  entrypoint.sh    Wait for gateway socket -> Studio -> uvicorn
```

## Prerequisites

| Dependency | Required for |
|-----------|-------------|
| Rook-Ceph ceph-block StorageClass | PVC |
| Istio | Ingress + mTLS |
| Vault (unsealed) | Secret injection |
| Ollama | Fallback inference |
| OpenClaw deployed | Peer agent calls |

## Deploy

```bash
# 1. Vault secret
vault kv put secret/homelab/ai/nemoclaw \
  nvidia_nim_api_key='nvapi-...' \
  agent_api_key='$(openssl rand -hex 32)' \
  openscell_gateway_token='$(openssl rand -hex 32)' \
  openclaw_api_key='<openclaw-agent-api-key>' \
  github_token=''

# 2. Vault K8s auth role
vault write auth/kubernetes/role/nemoclaw \
  bound_service_account_names=nemoclaw \
  bound_service_account_namespaces=nemoclaw \
  token_policies=app-read \
  token_ttl=3600

# 3. Build and push
docker build -t ghcr.io/<your-user>/nemoclaw:latest .
docker push ghcr.io/<your-user>/nemoclaw:latest

# 4. Deploy
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/nemoclaw/

# 5. Verify
kubectl get all -n nemoclaw
kubectl logs -n nemoclaw deploy/nemoclaw -c openscell-gateway -f
kubectl logs -n nemoclaw deploy/nemoclaw -c nemoclaw -f
```

## Security Controls

| Control | Implementation |
|---------|---------------|
| Filesystem isolation | Landlock LSM (blueprint.yaml) |
| Syscall filter | Custom seccomp allow-list (configmap.yaml) |
| Network isolation | netns + OpenShell egress proxy |
| API authentication | X-API-Key (Istio AP + app layer) |
| Secret handling | Vault Agent sidecar |
| Non-root execution | runAsUser: 1000 + no privilege escalation |
| Pod security | PSA restricted namespace label |
| Audit log | JSONL trail at /workspace/logs/audit.jsonl |
| Blocked tasks | Regex deny-list before agent execution |

## Switch Inference Backend

```bash
# Use Ollama fallback
kubectl set env deploy/nemoclaw -n nemoclaw LLM_BACKEND=ollama

# Back to NVIDIA NIM
kubectl set env deploy/nemoclaw -n nemoclaw LLM_BACKEND=nvidia_nim
```

## Next Agents

- HiClaw - task decomposition and planning
- QwenPaw - multilingual language tasks
- ReMe - long-term memory and RAG
