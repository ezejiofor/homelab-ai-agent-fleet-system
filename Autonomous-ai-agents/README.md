# Autonomous AI Agent Fleet

> **Homelab Kubernetes — hybrid orchestrator + learner, with a sandboxed runtime and a shared skill library**
> Stack: OpenClaw (Node.js, Gateway-first) · Hermes (Python, runtime-first) · OpenShell (sandboxed exec) · CephFS RWX shared skills · Kustomize

See [`AUTONOMOUS-AGENT-FLEET-PLAN.md`](./AUTONOMOUS-AGENT-FLEET-PLAN.md) for the full design rationale, evidence trail, and phasing.

---

## What this is (short version)

Two orchestrator agents — **OpenClaw** and **Hermes** — share one Kubernetes namespace (`agent-system`), one sandboxed runtime (**OpenShell**), and one skill library on a CephFS RWX volume. They speak the same `SKILL.md` format (AgentSkills standard), so any skill written by one agent can be used by the other.

OpenClaw fronts you (multi-channel: Telegram, Discord, Slack — Gateway-first). Hermes runs specialised workflows where memory and accumulated learning matter (runtime-first, Python, opt-in skill auto-creation). OpenShell sandboxes everything that runs untrusted code — **mandatory** because the upstream ClawHub registry has a 12% malware rate.

This is a **hybrid pattern**, not a winner-take-all choice. Reddit / Medium / Trilogy AI deep-dives all converge on running both — they're complementary, not redundant.

---

## Fleet (Phase 0 — what's actually deployed)

| Agent | Role | Image | Storage | Notes |
|---|---|---|---|---|
| [**openclaw**](./openclaw/) | Gateway-first orchestrator (multi-channel, strong-model) | `ghcr.io/georgehenderson/openclaw:latest` | `ceph-block` RWO (private) + `ceph-fs` RWX (shared skills) | Telegram / Discord front door, GLM-5 / Claude / GPT-4o |
| [**hermes**](./hermes/) | Runtime-first specialist (Python, learning loop) | `docker.io/nousresearch/hermes-agent:latest` | `ceph-block` RWO (private) + `ceph-fs` RWX (shared skills) | Local Qwen via Ollama, opt-in auto-skill creation |
| [**openshell**](./openshell/) | Sandboxed exec runtime (mandatory security boundary) | (built locally — Landlock + seccomp + netns) | `ceph-block` RWO ephemeral | Both orchestrators delegate untrusted code-exec here |

All three live in **one namespace**: `agent-system`.

### Deferred (Phase 2 — measure first, then build)

These are kept on disk under their original directory names but **excluded from the fleet `kustomization.yml`** until Phase 1 measurements justify them:

- **`ReMe`** — long-memory observer. Hermes' built-in observer may suffice; add ReMe only if it doesn't.
- **`skills/`** — the old custom AgentScope `SkillsHub` Python service. Replaced in Phase 0 by the shared CephFS PVC + portable `SKILL.md` files. Re-evaluate when 2+ agents start contending for file locks.
- **`nemoclaw`** — NVIDIA Nemotron wrapper. Re-add only if you have a NeMo-specific GPU workload.
- **`QwenPaw`** — Qwen wrapper. Hermes runs Qwen better; redundant.
- **`HiClaw`** — planner. Both orchestrators decompose tasks natively.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       agent-system namespace                                  │
│                                                                               │
│  ── Orchestrators ─────────────────────────────────────────────              │
│                                                                               │
│  ┌──────────────────┐                       ┌──────────────────┐             │
│  │     openclaw     │ ◄── Telegram/Discord  │      hermes      │             │
│  │ Gateway-first    │     Slack/WhatsApp    │ Runtime-first    │             │
│  │ TS/Node.js       │                       │ Python           │             │
│  │ Strong models    │                       │ Local-friendly   │             │
│  └────────┬─────────┘                       └────────┬─────────┘             │
│           │  read/write SKILL.md (AgentSkills format) │                       │
│           ▼                                           ▼                       │
│  ┌─────────────────────────────────────────────────────────┐                 │
│  │              shared-skills (CephFS RWX, 50Gi)            │                 │
│  │  • Both agents read/write                                │                 │
│  │  • Vetting gate: draft → reviewed → published            │                 │
│  │  • NO direct ClawHub pulls (12% upstream malware)        │                 │
│  └─────────────────────────────────────────────────────────┘                 │
│                                                                               │
│  ── Sandboxed runtime (mandatory) ─────────────────────────────              │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────┐                 │
│  │  openshell  · Landlock + seccomp + netns                │                 │
│  │  Ephemeral pods per task; both orchestrators delegate    │                 │
│  └─────────────────────────────────────────────────────────┘                 │
│                                                                               │
│  ── Shared infra (in-cluster, separate namespaces) ────────                  │
│   • Ollama         (ollama ns)    — local Qwen / Llama for Hermes            │
│   • Vault          (vault ns)     — secret injection per ServiceAccount      │
│   • CNPG+pgvector  (cnpg ns)      — skill embeddings (Phase 2)               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Why this design

The full evidence trail and decision log live in [`AUTONOMOUS-AGENT-FLEET-PLAN.md`](./AUTONOMOUS-AGENT-FLEET-PLAN.md). Short version:

| Source | Finding |
|---|---|
| Trilogy AI deep-dive | OpenClaw + Hermes share the same `SKILL.md` format — shared registry is real, not aspirational |
| r/LocalLLaMA threads | OpenClaw shines with strong cloud models; Hermes shines with local SLMs — they're complementary |
| Sathish Raju on Medium | "OpenClaw as orchestration layer + Hermes as execution agent for workflows where learning matters" — validated production pattern |
| ClawHub security data | **12% malware rate (341 / 2,857 skills)** — sandboxed runtime is mandatory |
| Hermes README | Self-learning is opt-in; must be enabled in `configmap.yaml` |
| Sathish Raju (perf) | Hermes after 20+ self-skills: **40% task-time reduction** on similar tasks (caveat: requires task volume) |

The old plan (NemoClaw + QwenPaw + HiClaw + ReMe + custom Python SkillsHub, all in separate namespaces) was correct for the AgentScope-only era. Hermes and the AgentSkills standard didn't exist yet. They do now, and the design simplifies dramatically.

---

## Hostname policy

Two hostnames, that's it:

- `openclaw.georgehomelab.com` → OpenClaw Studio + API
- `hermes.georgehomelab.com` → Hermes dashboard

Anything else (storage classes, namespaces, in-cluster services, skill volumes) stays internal — reached via `<svc>.<namespace>.svc.cluster.local:<port>`. See [`AUTONOMOUS-AGENT-FLEET-PLAN.md` → Hostname policy](./AUTONOMOUS-AGENT-FLEET-PLAN.md#hostname-policy-locked).

---

## Deploy via ArgoCD (recommended)

The fleet is a GitOps app — ArgoCD owns it. The ArgoCD config lives **outside** the bootstrap tree at [`../../0-georgehomelab-argocd-projects/`](../../0-georgehomelab-argocd-projects/) so an ArgoCD version bump can't drift the project / repo / appset config:

```
kubernetes-addons/
├── 0-boostrap/gitops/argocd/        ← installs ArgoCD itself (Helm chart)
└── 0-georgehomelab-argocd-projects/ ← ongoing GitOps config (this fleet uses)
    ├── 0-argocd-projects/           AppProjects (single file: georgehomelab-projects.yaml)
    ├── 1-argocd-secrets-oidc/       Repo Secrets (GitHub App = OIDC-flavored)
    └── 2-argocd-applicationsets/    ApplicationSets (one per fleet)
```

| File | What |
|---|---|
| `0-argocd-projects/georgehomelab-projects.yaml` | **Single** `AppProject` (`georgehomelab`) for the whole homelab. ApplicationSets carry per-fleet boundaries via labels. Add stricter projects (e.g., `georgehomelab-secure`) to the same file when needed. |
| `1-argocd-secrets-oidc/repo-homelab-github-app.yaml` | Repository `Secret` — private GitHub via GitHub App (App ID + Installation ID + PEM). OIDC-flavored federated identity, hence the OIDC folder. |
| `2-argocd-applicationsets/agent-fleet-system.yaml` | `ApplicationSet` (list generator). Each element fully parameterizes its child Application — `cluster` · `repoURL` · `revision` · `path` · `namespace` · `syncWave` — so one ApplicationSet handles multi-cluster, multi-repo, multi-env without duplication. |

**ApplicationSet > app-of-apps for ongoing management.** App-of-apps is bootstrap-only — every new app is a new YAML file and a new git push. ApplicationSets reconcile a generator (list, git-directory, cluster, matrix) into N child Applications from a single manifest.

### One-time bootstrap

```bash
# 1. Install ArgoCD itself (bootstrap)
kubectl apply -k kubernetes-addons/0-boostrap/gitops/argocd

# 2. Create a GitHub App for this repo, install it, download the private key.
#    Store credentials in Vault:
vault kv put secret/homelab/argocd/repo-homelab-github-app \
  github_app_id="<APP_ID>" \
  github_app_installation_id="<INSTALLATION_ID>" \
  github_app_private_key="$(cat homelab-argocd.private-key.pem)"

# 3. Render the placeholder Secret from Vault values + apply
#    (or wire up External Secrets Operator — see commented template
#    in 1-argocd-secrets-oidc/repo-homelab-github-app.yaml).

# 4. Apply the ongoing GitOps config (projects + secrets + applicationsets)
kubectl apply -k kubernetes-addons/0-georgehomelab-argocd-projects

# 5. ArgoCD reconciles — watch the fleet come up
argocd app list --project georgehomelab
kubectl get applications -n argocd -l app.kubernetes.io/part-of=agent-fleet-system
```

After this, every commit to `main` that touches `kubernetes-addons/Autonomous-ai-agents/` auto-syncs into the cluster (prune + selfHeal both on).

> **Note on "GitHub OIDC":** that's for GitHub Actions ↔ cloud federation, not ArgoCD ↔ GitHub. ArgoCD authenticates to GitHub using **GitHub App credentials directly** (App ID + Installation ID + private key in the Repository Secret). See the secret file's header comment for setup details.

---

## Deploy directly (alternative — not GitOps)

```bash
# 1. Pull Ollama models for Hermes' local-model path
kubectl exec -n ollama deploy/ollama -- ollama pull qwen2.5:14b
kubectl exec -n ollama deploy/ollama -- ollama pull nomic-embed-text

# 2. Seed Vault secrets
vault kv put secret/homelab/ai/hermes \
  openrouter_api_key="sk-or-..." \
  openai_api_key="sk-..." \
  anthropic_api_key="sk-ant-..." \
  telegram_bot_token="..." \
  api_server_key="$(openssl rand -hex 32)"

vault kv put secret/homelab/ai/openclaw \
  openai_api_key="sk-..." \
  openrouter_api_key="sk-or-..." \
  agent_api_key="$(openssl rand -hex 32)"

vault kv put secret/homelab/ai/openshell \
  agent_api_key="$(openssl rand -hex 32)"

# 3. Deploy the whole fleet in one shot via fleet-level Kustomize
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/

# 4. Bootstrap Hermes (interactive setup wizard writes to PVC)
kubectl -n agent-system wait --for=condition=Ready pod \
  -l app.kubernetes.io/name=hermes --timeout=300s
kubectl -n agent-system exec -it deploy/hermes -c gateway -- hermes setup

# 5. Verify
kubectl get all -n agent-system -l app.kubernetes.io/part-of=agent-system
```

The fleet `kustomization.yml` ([here](./kustomization.yml)) applies in this order:

1. `_shared/` — namespace + CephFS RWX skill volume (must come first)
2. `openshell/` — sandbox runtime (orchestrators delegate to it)
3. `openclaw/` and `hermes/` — orchestrators (no order between them)

Per-agent kustomizations are still valid and applied automatically:

```bash
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/openclaw/
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/hermes/
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/openshell/
```

---

## Required Hermes opt-in flags

Hermes' learning loop is **OFF by default**. The `hermes/configmap.yaml` must include:

```yaml
HERMES_PERSISTENT_MEMORY: "true"
HERMES_AUTO_SKILLS: "true"
```

Otherwise it behaves like a stateless agent. Most reviewers who say "Hermes self-learning is overhyped" forgot to flip this switch.

---

## Storage class — RWX is required for shared skills

| PVC | StorageClass | Access | Mounted by |
|---|---|---|---|
| `hermes-data` | `ceph-block` | RWO | hermes only (config + memory DB) |
| `openclaw-workspace` | `ceph-block` | RWO | openclaw only (memory + logs) |
| `shared-skills` | **`ceph-fs`** | **RWX** | hermes + openclaw |
| `openshell-workspace` | `ceph-block` | RWO | openshell pods (ephemeral) |

The `ceph-fs` RWX class is the new requirement. Verify it's available before applying:

```bash
kubectl get storageclass | grep ceph-fs
```

---

## Decision gate at end of Phase 1 (2 weeks of real use)

| Metric | Action |
|---|---|
| Skill registry hit rate < 15% | **Stop.** Phase 2 is not justified. |
| Hermes auto-skill creation produces useful skills | Keep on. Otherwise disable. |
| OpenClaw / Hermes overlap > 80% (one always wins) | Drop the unused one. |
| Token cost > $80/month | Reconsider scope or shift more to local models. |

The honest possibility: a single-user homelab may not have enough task volume for Phase 2 to pay off. **Hermes + OpenShell alone is still a real win** over the old 6-pod plan, and that's a perfectly valid endpoint.

---

## Observability

```bash
# Pods
kubectl get pods -n agent-system -l app.kubernetes.io/part-of=agent-system

# Logs
kubectl logs -n agent-system deploy/openclaw -f
kubectl logs -n agent-system deploy/hermes -c gateway -f
kubectl logs -n agent-system deploy/hermes -c dashboard -f

# Hermes dashboard (gated by oauth2-proxy + Keycloak via Istio VirtualService)
open https://hermes.georgehomelab.com

# OpenClaw dashboard
open https://openclaw.georgehomelab.com

# Skill registry contents (mounted on either agent)
kubectl exec -n agent-system deploy/hermes -c gateway -- ls /opt/data/skills
kubectl exec -n agent-system deploy/openclaw -- ls /workspace/skills
```

---

## File layout

```
Autonomous-ai-agents/
├── kustomization.yml                 ← fleet root (kubectl apply -k .)
├── README.md                         ← you are here
├── AUTONOMOUS-AGENT-FLEET-PLAN.md    ← design + evidence + phasing
│
├── _shared/                          ← namespace + CephFS RWX skills PVC
│
├── openclaw/                         ← Gateway-first orchestrator (active)
├── hermes/                           ← Runtime-first specialist (active)
├── openshell/                        ← Sandboxed exec runtime (active)
│
├── nemoclaw/                         ← archived (excluded from fleet kustomize)
├── QwenPaw/                          ← archived
├── HiClaw/                           ← archived
├── ReMe/                             ← deferred to Phase 2
└── skills/                           ← old AgentScope SkillsHub — replaced by shared CephFS PVC
```

---

## License

Apache 2.0 — see [LICENSE](../../LICENSE)
