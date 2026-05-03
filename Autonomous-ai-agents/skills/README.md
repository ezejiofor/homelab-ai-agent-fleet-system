# SkillsHub - Autonomous Agent Skill Learning Registry

> The central skill learning and registry service for the autonomous AI agent fleet.

## What is a Skill?

A **Skill** is a structured, versioned, machine-executable workflow that an agent has
learned. It contains:
- **Steps** with allowed tools only (k8s_query, vault_read, http_get, etc.)
- **Prerequisites** and **trigger conditions**
- **Success criteria** and optional rollback steps
- **Trust level** (draft → reviewed → approved)
- **Execution stats** (count, success rate) — improves over time

## How Agents Learn Skills

```
Agent                        SkillsHub                      Human Admin
  |                              |                               |
  |-- POST /api/skills/learn --> |                               |
  |   (workflow description)     |                               |
  |                              |--> LLM structure workflow      |
  |                              |--> SecurityValidator check     |
  |                              |--> embed description           |
  |                              |--> store in Qdrant + PVC       |
  |<-- skill_id (trust=draft) ---|                               |
  |                              |                               |
  |                              |<-- PUT /approve (ADMIN key) --|  
  |                              |--> trust_level = approved      |
  |                              |                               |
  |-- GET /api/skills/suggest -> |                               |
  |<-- [approved skills]  -------|                               |
  |                              |                               |
  |-- POST /api/skills/outcome ->|  (records success/failure)    |
```

## Security Model

| Control | Detail |
|---------|-------|
| Tool allowlist | Steps can only use 11 approved tools (no raw shell/eval/exec) |
| Blocked patterns | 25+ regex patterns block dangerous commands in all fields |
| Trust levels | draft → reviewed → approved; agents cannot auto-execute drafts |
| Human approval | Admin must call PUT /approve with ADMIN_API_KEY |
| Security score | 0.0-1.0 score; < 0.7 → skill rejected outright |
| Full audit log | Every learn/approve/execute/outcome logged to JSONL |
| PSA restricted | Namespace runs at pod-security restricted level |

## API Reference

| Method | Endpoint | Who | Purpose |
|--------|----------|-----|---------|
| POST | /api/skills/learn | Agents | Submit workflow, get structured skill (draft) |
| GET  | /api/skills/search?q= | Agents | Semantic search over skill descriptions |
| GET  | /api/skills/suggest?context= | Agents | Auto-suggest skills for current task |
| GET  | /api/skills/{id} | Agents | Get full skill definition |
| GET  | /api/skills | Agents | List all skills with filters |
| PUT  | /api/skills/{id}/approve | ADMIN | Approve draft → auto-executable |
| PUT  | /api/skills/{id}/deprecate | ADMIN | Retire a skill |
| POST | /api/skills/outcome | Agents | Record execution result |
| GET  | /api/skills/{id}/versions | Agents | Version history |
| POST | /api/skills/export | ADMIN | Export all skills as JSONL |
| GET  | /api/skills/stats/summary | Agents | Registry statistics |

## Deploy

```bash
# 1. ReMe must be deployed first (shares its Qdrant)
kubectl get sts qdrant -n reme

# 2. Vault secret
vault kv put secret/homelab/ai/skillshub \
  admin_api_key='$(openssl rand -hex 32)' \
  agent_api_key='$(openssl rand -hex 32)' \
  qdrant_api_key=''

# 3. Vault K8s auth role
vault write auth/kubernetes/role/skillshub \
  bound_service_account_names=skillshub \
  bound_service_account_namespaces=skillshub \
  token_policies=app-read token_ttl=3600

# 4. Build and push
docker build -t ghcr.io/<user>/skillshub:latest . && docker push ghcr.io/<user>/skillshub:latest

# 5. Deploy (requires ReMe/Qdrant already running)
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/skills/

# 6. Verify
kubectl get all -n skillshub
curl https://skillshub.georgehomelab.com/health
```

## Example — Agent Learns a Skill

```bash
# Agent submits a workflow
curl -X POST https://skillshub.georgehomelab.com/api/skills/learn \
  -H 'X-API-Key: <agent-key>' -H 'Content-Type: application/json' \
  -d '{
    workflow_description: To deploy Vault: check ceph-block, helm install vault HA,
    wait for pods, vault operator init, unseal with 3 keys, export root token,
    "submitted_by": "hiclaw",
    "category_hint": "infrastructure"
  }'

# Returns: {skill_id, trust_level: draft, security_score: 0.95, steps: [...]}

# Admin approves it
curl -X PUT https://skillshub.georgehomelab.com/api/skills/<id>/approve \
  -H 'X-API-Key: <admin-key>' -H 'Content-Type: application/json' \
  -d '{"approved_by": "george", "notes": "Reviewed steps - looks correct"}'

# Agent searches for it later
curl 'https://skillshub.georgehomelab.com/api/skills/search?q=deploy+vault+helm' \
  -H 'X-API-Key: <agent-key>'

# Agent gets suggestions before acting
curl 'https://skillshub.georgehomelab.com/api/skills/suggest?context=I+need+to+set+up+vault+HA' \
  -H 'X-API-Key: <agent-key>'
```

## Using SkillsClient in Agent Code

```python
from skills_client import SkillsClient

skills = SkillsClient()  # SKILLSHUB_URL + SKILLSHUB_API_KEY from env

# Before starting a task, check for existing skills
hints = await skills.suggest('deploy monitoring stack to new namespace')
for hint in hints:
    if hint['trust_level'] == 'approved':
        print(f'Found approved skill: {hint["name"]}')
        skill = await skills.get(hint['skill_id'])
        # ... execute skill steps ...

# After completing a novel task, teach it to the fleet
result = await skills.learn(
    workflow='Step 1: kubectl apply -f ... Step 2: wait for pods ...',
    submitted_by='openclaw',
    category='infrastructure',
)
print(f'Skill learned: {result["skill_id"]} (pending human approval)')
```
