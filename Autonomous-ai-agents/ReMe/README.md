# ReMe - Long-term Memory + RAG Agent

> Part of the homelab Autonomous AI Agent fleet

## Role

ReMe is the **memory and knowledge retrieval specialist** of the agent fleet.
It provides persistent, searchable long-term memory for all other agents via
vector embeddings (nomic-embed-text) stored in an in-cluster Qdrant database.

## Stack

| Component | Technology | Purpose |
|-----------|-----------|--------|
| Vector DB | Qdrant v1.9 (StatefulSet, 50Gi PVC) | Stores 768-dim embeddings |
| Embeddings | nomic-embed-text via Ollama | Converts text to vectors |
| Generation | llama3.1:70b via Ollama (temp=0.1) | Grounds answers in memories |
| Framework | AgentScope DialogAgent | Agent orchestration |
| API | FastAPI | REST interface for fleet agents |

## Key Endpoints

| Endpoint | Purpose |
|----------|--------|
| POST /api/remember | Store a memory with tags + metadata + TTL |
| POST /api/recall | RAG: search memories then generate grounded answer |
| POST /api/search | Raw vector search (returns docs, no generation) |
| DELETE /api/forget/{id} | Delete a specific memory by UUID |
| POST /api/forget/bulk | Bulk delete by tag/source/date (dry_run=true first) |
| POST /api/export | Export all memories as JSONL to /workspace/exports/ |
| GET  /api/stats | Qdrant collection stats (total memories, model info) |
| POST /api/chat | Chat with top-k memories auto-injected as context |

## Architecture

```
reme namespace
|- qdrant StatefulSet (50Gi ceph-block PVC)
|   L Service: qdrant:6333 (HTTP) + qdrant:6334 (gRPC)
L  reme Deployment
    |- initContainer: wait-for-qdrant (polls /readyz)
    |- initContainer: init-workspace
    L  container: reme
         |- POST /api/remember -> embed() -> Qdrant.upsert()
         |- POST /api/recall   -> embed() -> Qdrant.search() -> LLM(context)
         |- DELETE /api/forget -> Qdrant.delete()
         L  POST /api/chat     -> embed() -> Qdrant.search() -> inject -> LLM
```

## Memory Taxonomy Tags

`fact` `decision` `code` `log` `config` `error` `procedure` `person` `system` `event`

## Deploy

```bash
# Pull embedding model into Ollama
kubectl exec -n ollama deploy/ollama -- ollama pull nomic-embed-text
kubectl exec -n ollama deploy/ollama -- ollama pull llama3.1:70b

# Vault secret
vault kv put secret/homelab/ai/reme \
  agent_api_key='$(openssl rand -hex 32)' \
  qdrant_api_key=''

# Vault K8s auth role
vault write auth/kubernetes/role/reme \
  bound_service_account_names=reme \
  bound_service_account_namespaces=reme \
  token_policies=app-read token_ttl=3600

# Build and push
docker build -t ghcr.io/<user>/reme:latest . && docker push ghcr.io/<user>/reme:latest

# Deploy (Qdrant StatefulSet + ReMe agent)
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/ReMe/

# Verify
kubectl get all -n reme
kubectl logs -n reme deploy/reme -f
```

## Example Usage

```bash
API='https://reme.georgehomelab.com'
KEY='<your-agent-api-key>'

# Store a memory
curl -X POST $API/api/remember \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"text": "The Vault root token was rotated on 2026-04-22. New token stored in Bitwarden.",
       "source": "ops-log", "tags": ["fact", "decision", "system"]}'

# Recall with RAG
curl -X POST $API/api/recall \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"query": "When was the Vault root token last rotated?"}'

# Bulk forget old logs (dry run first)
curl -X POST $API/api/forget/bulk \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"filter_tags": ["log"], "before_timestamp": 1700000000, "dry_run": true}'
```

## Fleet Integration

Other agents can store/recall memories:

```bash
# From HiClaw: store a plan decision
curl -X POST http://reme.reme.svc.cluster.local:8000/api/remember \
  -H "X-API-Key: $REME_API_KEY" -H 'Content-Type: application/json' \
  -d '{"text": "Decided to use CephFS for QwenPaw to enable RWX scaling.",
       "source": "hiclaw", "tags": ["decision", "config"]}'
```
