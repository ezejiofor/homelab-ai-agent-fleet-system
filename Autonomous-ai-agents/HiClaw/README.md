# HiClaw - Task Decomposition and Planning Agent

> Part of the homelab Autonomous AI Agent fleet

## Role

HiClaw is the **planning specialist** of the agent fleet. It:
1. Breaks complex goals into ordered, dependency-aware subtasks
2. Assigns each subtask to the most suitable fleet agent
3. Identifies the critical path and blockers
4. Delegates subtasks to fleet agents via /api/delegate

Model: llama3.1:70b (Ollama, in-cluster) with temperature=0.3 for deterministic planning.

## Key Endpoints

| Endpoint | Purpose |
|----------|--------|
| POST /api/plan | Generate structured JSON plan with subtasks + critical path |
| GET  /api/plan/{id} | Retrieve stored plan |
| POST /api/delegate | Forward a subtask to a fleet agent |
| POST /api/chat | Free-form planning conversation |

## Deploy

```bash
vault kv put secret/homelab/ai/hiclaw \
  agent_api_key='$(openssl rand -hex 32)' \
  openclaw_api_key='<key>' nemoclaw_api_key='<key>' \
  qwenpaw_api_key='<key>' reme_api_key='<key>'

vault write auth/kubernetes/role/hiclaw \
  bound_service_account_names=hiclaw \
  bound_service_account_namespaces=hiclaw \
  token_policies=app-read token_ttl=3600

docker build -t ghcr.io/<user>/hiclaw:latest . && docker push ghcr.io/<user>/hiclaw:latest
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/HiClaw/
```

## Example

```bash
curl -X POST https://hiclaw.georgehomelab.com/api/plan \
  -H 'X-API-Key: <key>' -H 'Content-Type: application/json' \
  -d '{"goal": "Set up monitoring for the Vault cluster"}'
```

Returns structured JSON with T1..Tn subtasks, dependencies, agent assignments,
and the critical path.
