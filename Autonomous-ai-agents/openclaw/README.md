# OpenClaw — General-Purpose Autonomous AI Agent

> **Part of the homelab Autonomous AI Agent fleet** | Framework: [AgentScope](https://github.com/modelscope/agentscope)

## Overview

OpenClaw is a **general-purpose autonomous AI agent** built on the AgentScope framework.
It uses OpenAI-compatible APIs — defaulting to a local **Ollama** instance for zero-cost
inference, with optional **OpenAI API** fallback for tasks requiring more powerful models.

```
┌──────────────────────────────────────────────────────────────────┐
│                      openclaw namespace                           │
│                                                                    │
│  ┌─────────────────────┐     ┌──────────────────────────────┐    │
│  │   FastAPI REST API  │     │     AgentScope Studio UI     │    │
│  │   port 8000         │     │     port 5000                │    │
│  │  POST /api/task     │     │  http://openclaw:5000        │    │
│  │  POST /api/chat     │     │  (browser web interface)     │    │
│  └────────┬────────────┘     └──────────────────────────────┘    │
│           │                                                        │
│  ┌────────▼───────────────────────────────────────────────────┐  │
│  │              DialogAgent (AgentScope)                       │  │
│  │   Tools: web_search · code_executor · file_io ·            │  │
│  │           http_request · k8s_query                         │  │
│  └────────┬───────────────────────────────────────────────────┘  │
│           │                                                        │
│  ┌────────▼──────────┐   ┌─────────────────────────────────────┐ │
│  │  PVC: workspace   │   │  ConfigMap: model_config + prompt   │ │
│  │  ceph-block 10Gi  │   │  Vault: API keys (sidecar inject)   │ │
│  └───────────────────┘   └─────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
         │ LLM inference
         ├──► Ollama (in-cluster)  http://ollama.ollama.svc:11434/v1
         └──► OpenAI API           https://api.openai.com/v1
```

## Agent Fleet

| Agent | Namespace | Role | Model Backend |
|-------|-----------|------|---------------|
| **OpenClaw** | `openclaw` | General-purpose orchestrator | Ollama / OpenAI |
| NemoClaw | `nemoclaw` | Structured reasoning + NeMo | Nvidia NeMo |
| HiClaw | `hiclaw` | Task decomposition + planning | Ollama |
| QwenPaw | `qwenpaw` | Language + multilingual tasks | Qwen2.5 |
| ReMe | `reme` | Long-term memory + RAG | Ollama + Qdrant |

## File Structure

```
openclaw/
├── Dockerfile              Multi-stage Python 3.12 image
├── kustomization.yaml      Kustomize entry point
├── namespace.yaml          openclaw namespace (Istio injection enabled)
├── serviceaccount.yaml     K8s SA (Vault role: openclaw)
├── rbac.yaml               Role + RoleBinding (pod/cm read access)
├── configmap.yaml          LLM model config + system prompt + env vars
├── secret.yaml             Placeholder (real values from Vault)
├── pvc.yaml                ceph-block 10Gi workspace PVC
├── deployment.yaml         Main agent Deployment (Recreate strategy)
├── service.yaml            ClusterIP: ports 8000 (API), 5000 (Studio), 12010 (RPC)
├── hpa.yaml                HPA: 1-3 replicas on CPU/memory pressure
├── virtualservice.yaml     Istio VirtualService + AuthorizationPolicy + NetworkPolicy
└── app/
    ├── main.py             FastAPI + AgentScope server
    ├── requirements.txt    Python deps
    └── entrypoint.sh       Start Studio + API server
```

## Prerequisites

| Dependency | Namespace | Required |
|-----------|-----------|----------|
| Rook-Ceph (`ceph-block` StorageClass) | `rook-ceph` | ✅ PVC |
| Istio | `istio-system` | ✅ Ingress + mTLS |
| Vault (unsealed) | `vault` | ✅ Secret injection |
| Ollama | `ollama` | ✅ Default LLM backend |

## Deploy

```bash
# 1. Add Vault secret for API keys
vault kv put secret/homelab/ai/openclaw \
  openai_api_key="sk-..." \
  openai_org_id="org-..." \
  agent_api_key="$(openssl rand -hex 32)" \
  serper_api_key="" \
  github_token=""

# 2. Add Vault auth role for openclaw ServiceAccount
vault write auth/kubernetes/role/openclaw \
  bound_service_account_names=openclaw \
  bound_service_account_namespaces=openclaw \
  token_policies=app-read \
  token_ttl=3600

# 3. Build and push image
docker build -t ghcr.io/<your-user>/openclaw:latest .
docker push ghcr.io/<your-user>/openclaw:latest

# 4. Deploy
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/openclaw/

# 5. Verify
kubectl get all -n agents-system
kubectl logs -n agents-system deploy/openclaw -f
```

## API Usage

### Submit an async task
```bash
curl -X POST https://openclaw.georgehomelab.com/api/task \
  -H "X-API-Key: <your-agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Search the web for the latest Kubernetes release notes and summarize them"}'

# Response: {"task_id": "a1b2c3d4", "status": "pending"}
```

### Poll task result
```bash
curl https://openclaw.georgehomelab.com/api/task/a1b2c3d4 \
  -H "X-API-Key: <your-agent-api-key>"
```

### Synchronous chat
```bash
curl -X POST https://openclaw.georgehomelab.com/api/chat \
  -H "X-API-Key: <your-agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the status of the Vault pods?"}'
```

### Studio UI
Open **https://openclaw.georgehomelab.com** in your browser — no API key required for the Studio UI.

## Configuration

### Switch LLM backend at runtime
```bash
# Switch to OpenAI (requires OPENAI_API_KEY in Vault)
kubectl set env deploy/openclaw -n agents-system LLM_BACKEND=openai

# Switch back to Ollama
kubectl set env deploy/openclaw -n agents-system LLM_BACKEND=ollama

# Change Ollama model
kubectl set env deploy/openclaw -n agents-system OLLAMA_MODEL=llama3.3:70b
```

### Rotate TLS cert
```bash
ansible-playbook playbooks/vault-init-unseal.yml -e vault_tls_rotate=true
```

## Observability

```bash
# Logs
kubectl logs -n agents-system deploy/openclaw --follow

# Metrics (Prometheus)
curl http://openclaw.agents-system.svc.cluster.local:8000/metrics

# AgentScope run history
kubectl exec -n agents-system deploy/openclaw -- ls /workspace/agentscope_runs/
```

## Next Agents

After OpenClaw is running:

```bash
# Deploy NemoClaw (Nvidia NeMo reasoning agent)
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/nemoclaw/

# Deploy QwenPaw (Qwen language agent)
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/QwenPaw/
```

