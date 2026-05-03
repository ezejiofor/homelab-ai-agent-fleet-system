# Autonomous AI Agent Fleet — Implementation Plan

> **Updated:** 2026-04-29
> **Author:** george + Claude
> **Status:** Phase 0 — namespace consolidation in progress

---

## TL;DR

Build the fleet as a **two-tier system inside one shared namespace** (`agent-system`), driven by community + technical evidence:

1. **Orchestrators** (do work) — `openclaw` (Gateway-first, strong-model, multi-channel) + `hermes` (runtime-first, local-model, learning-loop enabled).
2. **Sandboxed runtime** (mandatory security boundary) — `openshell` ephemeral exec pods. *Required* because ClawHub upstream registry has a 12% malware rate (341 / 2,857 skills).
3. **Shared skill registry** — RWX CephFS volume, AgentSkills-standard `SKILL.md` format. **Both Hermes and OpenClaw read/write the same store** (the format is portable across both — confirmed by Trilogy AI deep-dive).
4. **Observer/learner layer** (Hermes/ReMe writing skills from orchestrator trajectories) — **deferred to Phase 2.** Hermes' built-in opt-in skill auto-creation may be sufficient alone.
5. **Drop** `NemoClaw`, `QwenPaw`, `HiClaw` as separate pods — model choice is config, not architecture; capability-wrappers are features, not services.

This plan supersedes the original AgentScope-only fleet (101 files, 6 agents, 6 namespaces) which was built before Hermes existed and before the SKILL.md standard converged.

---

## Why this design (the evidence trail)

| Source | Finding | Implication |
|---|---|---|
| Trilogy AI deep-dive | Both Hermes and OpenClaw use the same `SKILL.md` format (AgentSkills standard) | Shared skill registry is technically real, not aspirational |
| r/LocalLLaMA (1.5k+ comments) | OpenClaw struggles with small/local models, shines with strong cloud models (GLM-5, Claude). Hermes shines with local SLMs | Run **both**, not one — they're complementary, not redundant |
| Sathish Raju on Medium | "OpenClaw as multi-channel orchestration layer. Hermes as execution agent for workflow types where accumulated learning matters." | Validated production hybrid pattern; matches our orchestrator/learner intuition |
| Sathish Raju (security data) | ClawHub: 341 malicious skills out of 2,857 (12% malware rate). Hermes: zero CVEs (younger) | Sandboxed runtime is **mandatory**, not optional |
| Sathish Raju (cost data) | Hermes + Claude Sonnet on $5 VPS = $30–65/month; OpenClaw self-hosted = $40–80/month | Skill caching alone won't justify the build — measure |
| Sathish Raju (perf data) | Hermes after 20+ self-generated skills: 40% task-time reduction on domain-similar tasks | Skill-library payoff is real *at sufficient task volume* |
| Hermes README | Self-learning is **opt-in** — disabled by default | Must explicitly enable in `configmap.yaml` |
| Trilogy AI architecture | OpenClaw = gateway-first (Node.js, persistent process, named agents). Hermes = runtime-first (Python, isolated Profiles per agent) | Different stacks, different failure modes — keep them isolated |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       agent-system namespace                                  │
│                                                                               │
│  ── Orchestrators (do work) ─────────────────────────────────                │
│                                                                               │
│  ┌──────────────────┐                       ┌──────────────────┐             │
│  │     openclaw     │                       │      hermes      │             │
│  │ Gateway-first    │                       │ Runtime-first    │             │
│  │ TS/Node.js       │                       │ Python           │             │
│  │ Strong models    │                       │ Local-friendly   │             │
│  │ Multi-channel    │                       │ Skill auto-      │             │
│  │ "front door"     │                       │ creation (opt-in)│             │
│  └────────┬─────────┘                       └────────┬─────────┘             │
│           │                                          │                        │
│           │     SKILL.md (AgentSkills standard)      │                        │
│           ▼                                          ▼                        │
│  ┌─────────────────────────────────────────────────────────┐                 │
│  │              shared-skills (CephFS RWX PVC)              │                 │
│  │  • Both agents read/write                                │                 │
│  │  • Vetting gate: draft → reviewed → published            │                 │
│  │  • NO direct ClawHub pulls into runtime (12% malware)    │                 │
│  └─────────────────────────────────────────────────────────┘                 │
│                                                                               │
│  ── Sandboxed runtime (security boundary) ─────────────────                  │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────┐                 │
│  │                       openshell                          │                 │
│  │  Ephemeral pods per task · Landlock + seccomp · netns    │                 │
│  │  Both orchestrators delegate code-exec here              │                 │
│  └─────────────────────────────────────────────────────────┘                 │
│                                                                               │
│  ── Shared infra (in-cluster) ─────────────────────────────                  │
│                                                                               │
│   • Ollama (ollama ns) — local Qwen / Llama for Hermes                       │
│   • OpenRouter / Anthropic / OpenAI (external) — strong models for OpenClaw  │
│   • Vault — secret injection per ServiceAccount                              │
│   • CNPG + pgvector — skill embeddings (Phase 2)                             │
│   • NATS — event bus for observer/learner pattern (Phase 2)                  │
│                                                                               │
│  ── DEFERRED to Phase 2 (do NOT build yet) ────────────────                  │
│                                                                               │
│   • ReMe observer (Hermes' built-in observer may suffice)                    │
│   • Skill-vetter (bandit/semgrep static-analysis service)                    │
│   • Cross-agent NATS event bus                                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### What goes away vs. the old plan

| Old | New | Why |
|---|---|---|
| 6 namespaces (one per agent) | **1 namespace** (`agent-system`) | Cross-agent RPC is simpler; fleet visualization clearer; per-agent SAs + Vault roles still distinct |
| `nemoclaw` (NVIDIA Nemotron wrapper) | **Removed** | Model choice is config. Hermes and OpenClaw both route to multiple models. Only re-add if you have NeMo-specific GPU workloads. |
| `QwenPaw` (Qwen wrapper) | **Removed** | Same reason. Hermes runs Qwen *better* than QwenPaw does (per r/LocalLLaMA threads). |
| `HiClaw` (planner agent) | **Removed** | Both Hermes and OpenClaw decompose tasks natively. No need for a separate planner pod. |
| `ReMe` (memory agent) | **Deferred** | Hermes has FTS5 session search + Gemini Flash summarization built in. Add ReMe only if Hermes' memory proves insufficient. |
| Custom `SkillsHub` Python service | **Replaced** with shared CephFS PVC + SKILL.md files | Both agents already speak SKILL.md natively. A custom service adds a hop and a moving part. Add an HTTP-fronted registry only when 2+ agents fight over file locks. |
| AgentScope as the framework | **Removed dependency** | OpenClaw and Hermes are first-class agents with their own runtimes. AgentScope was the substrate when nothing else existed. |
| 6 separate `Vault` roles | **Kept** (per-agent SAs + roles) | Even in a shared namespace, secret blast-radius stays per-agent. |
| `:8000` FastAPI per agent | **Replaced** with each agent's native API | OpenClaw uses its Gateway port; Hermes uses dashboard `:9119` + optional API server `:9120`. |

---

## Phasing

### Phase 0 — namespace consolidation (this week)

- [x] Create `agent-system/namespace.yaml` (single shared namespace)
- [x] Scaffold `hermes/` k8s manifests (port of upstream docker-compose)
- [ ] Update `openclaw/` manifests: namespace → `agent-system`, fix Service DNS
- [ ] Move `openshell` (currently inside nemoclaw) into its own dir at fleet root
- [ ] Add fleet-level `kustomization.yml` (this directory) referencing only the active agents
- [ ] Add `shared-skills/pvc.yaml` (CephFS RWX, 50Gi)
- [ ] Mount `shared-skills` into both Hermes and OpenClaw at `/opt/skills` (Hermes) and `/workspace/skills` (OpenClaw)
- [ ] Bootstrap Hermes via `kubectl exec ... hermes setup`
- [ ] Verify: both agents can read each other's skills

### Phase 1 — use it (next 2 weeks)

- [ ] Telegram bot routed to OpenClaw Gateway
- [ ] Hermes dashboard fronted by oauth2-proxy + Keycloak (matches existing SSO pattern for argocd / rancher / vault)
- [ ] Track in a Grafana dashboard:
  - tokens/day per agent
  - skill registry size
  - skill hit rate (how often a retrieved skill is actually used)
  - task success rate (self-reported, with caveat)
- [ ] **Decision gate at end of week 2:** Does the skill registry have measurable hit rate > 15%? If no — stop. If yes — proceed to Phase 2.

### Phase 2 — observer/learner (only if Phase 1 justifies it)

- [ ] NATS deployment in `agent-system` for `task.start` / `task.complete` / `task.failed` events
- [ ] OpenClaw + Hermes emit events on task lifecycle
- [ ] Skill-vetter service (Python, bandit + semgrep + LLM-judge) before promote `draft → published`
- [ ] pgvector schema in CNPG for skill embeddings
- [ ] Optional: ReMe as second observer (only if Hermes' built-in observer is insufficient)

### Phase 3 — quality + retirement (ongoing)

- [ ] Skill hit rate / win rate tracking
- [ ] Auto-retirement of low-hit skills
- [ ] Per-agent provenance + kill switch (revert all skills authored by agent X)
- [ ] Trajectory export for fine-tuning datasets

**Critical rule:** Do not build Phase 2 until Phase 1 measurements justify it. Do not build Phase 3 until Phase 2 is stable. Single-user homelab task volume may not be enough to justify Phase 2 at all — and that's fine.

---

## Operational concerns (bake in from day 1)

### Hermes opt-in flags
Self-learning is OFF by default in upstream Hermes. Set in `hermes/configmap.yaml`:

```yaml
HERMES_PERSISTENT_MEMORY: "true"
HERMES_AUTO_SKILLS: "true"
```

Otherwise the much-touted learning loop is dormant.

### Storage class — RWX is required for shared skills

Current per-agent PVCs use `ceph-block` (RWO). The **shared skill registry must be `ceph-fs` (CephFS RWX)** so both agents can read concurrently. Per-agent state (Hermes config DB, OpenClaw memory) stays on `ceph-block`.

```
PVC                       StorageClass    Access   Mounted by
hermes-data               ceph-block      RWO      hermes only (Hermes config + memory DB)
openclaw-workspace        ceph-block      RWO      openclaw only (OpenClaw memory + logs)
shared-skills             ceph-fs         RWX      hermes + openclaw  ← NEW
openshell-workspace       ceph-block      RWO      openshell pods (per-task, ephemeral)
```

### Vault roles (kept per-agent)

Even with one shared namespace, each agent has its own ServiceAccount + Vault role. Blast radius of a leaked credential stays local to one agent.

| ServiceAccount | Vault role | Vault path |
|---|---|---|
| `hermes` (in `agent-system`) | `hermes` | `secret/homelab/ai/hermes` |
| `openclaw` (in `agent-system`) | `openclaw` | `secret/homelab/ai/openclaw` |
| `openshell` (in `agent-system`) | `openshell` | `secret/homelab/ai/openshell` |

### Security: ClawHub upstream is NOT trusted

Per the data: 12% of ClawHub skills are malicious. Rules:

1. **Never auto-pull from clawhub.ai into the live registry.**
2. **All untrusted skill execution goes through OpenShell** (Landlock + seccomp + netns).
3. **Phase 2 adds a `skill-vetter`** (bandit/semgrep + LLM-judge) before any external skill becomes `published`.
4. **In Phase 0, accept only skills written by your own agents** — no public registry mirroring yet.

### What we are NOT building (and why)

- **Custom SkillsHub Python service** — both agents speak SKILL.md natively; a service adds a hop. Re-evaluate when 2+ agents start fighting over file locks.
- **Per-model agent pods (NemoClaw, QwenPaw)** — model choice is config.
- **HiClaw planner** — both orchestrators decompose tasks natively.
- **Cross-agent ACP/MCP from day 1** — start with file-based shared skills; add MCP server in front of the registry only when retrieval needs ranking.
- **AgentScope substrate** — OpenClaw and Hermes are first-class agents now.

---

## Hostname policy (locked)

The fleet exposes **exactly two** external hostnames. Anything else is internal-only and reached via in-cluster DNS (`<svc>.<namespace>.svc.cluster.local:<port>`), not Istio.

| Hostname | Backing service | Phase |
|---|---|---|
| `openclaw.georgehomelab.com` | `openclaw.openclaw.svc:5000` (Studio UI) + `:8000` (API) | 0 |
| `hermes.georgehomelab.com` | `hermes.hermes.svc:9119` (dashboard) | 0 |
| `agent-skills.georgehomelab.com` | `skill-vetter.agent-system.svc:8000` | **2 (only when built)** |
| ~~`agents.georgehomelab.com`~~ | optional landing page | skip |

**Never** create VirtualServices for:

- Storage classes (`ceph-fs`, `ceph-block`) — CSI drivers, no HTTP.
- Namespaces (`agent-system`) — not a network endpoint.
- In-cluster-only services (`openshell`) — pods reach them by cluster DNS.
- Skill files / volumes (`skills`) — accessed via PVC mount, not HTTP.

The two valid VirtualServices live at:

- [`openclaw/virtualservice.yaml`](./openclaw/virtualservice.yaml)
- [`hermes/virtualservice.yaml`](./hermes/virtualservice.yaml)

---

## GitOps deployment (ArgoCD)

The fleet deploys via an **ApplicationSet** (not app-of-apps — that pattern is
bootstrap-only and doesn't scale). One AppProject (`georgehomelab`) covers the
entire homelab; ApplicationSets carry per-fleet boundaries via labels.

The ArgoCD GitOps config lives **outside** the bootstrap path so an ArgoCD
version bump can't drift the project / repo / appset config:

```
kubernetes-addons/
├── 0-boostrap/gitops/argocd/                 ← installs ArgoCD itself (Helm)
└── 0-georgehomelab-argocd-projects/          ← ongoing GitOps config (this section)
    ├── kustomization.yaml                    Top-level: include all 3 subdirs
    ├── 0-argocd-projects/
    │   └── georgehomelab-projects.yaml       ONE file holds ALL AppProjects
    │                                         (currently: `georgehomelab`)
    ├── 1-argocd-secrets-oidc/
    │   └── repo-homelab-github-app.yaml      Repository Secret — GitHub App
    │                                         (OIDC-flavored federated identity)
    └── 2-argocd-applicationsets/
        └── agent-fleet-system.yaml           ApplicationSet — list generator,
                                              fully parameterized per element:
                                              cluster · repoURL · revision · path ·
                                              namespace · syncWave
```

### Why each piece exists

| Resource | Why |
|---|---|
| **AppProject `georgehomelab`** (single) | Outer envelope for the whole homelab. ApplicationSets reference `project: georgehomelab` and label children with `app.kubernetes.io/part-of: <fleet>` to differentiate. Stricter projects (e.g., `georgehomelab-secure` for sensitive fleets) can be added to the same `georgehomelab-projects.yaml` file. |
| **Repository Secret (GitHub App)** | The repo is private. ArgoCD authenticates via a GitHub App (App ID + Installation ID + private key) — rotation-friendly and per-repo scoped. Lives in `1-argocd-secrets-oidc/` because GitHub App auth is OIDC-flavored federated identity. |
| **ApplicationSet `agent-fleet-system`** | One Application per fleet member, generated from a list. Each list element fully parameterizes its child — `cluster`, `repoURL`, `revision`, `path`, `namespace`, `syncWave` — so the *same* ApplicationSet can target different folders, different repos, different clusters, different environments. Adding a fleet member or a new cluster is one list entry. |

### Why ApplicationSet (not app-of-apps)

| | app-of-apps | ApplicationSet |
|---|---|---|
| Scaling | one YAML per app | one ApplicationSet generates N |
| Multi-cluster | duplicate every Application | parameterize `cluster` in elements |
| Multi-repo | duplicate every Application | parameterize `repoURL` in elements |
| Multi-env | duplicate every Application | matrix generator (env × app) |
| Sync waves | per-Application annotation | per-element field |
| Discovery | manual | git directory / cluster / SCM generators |
| Use today | bootstrap only | everything ongoing |

### GitHub auth — clarifying "GITHUB OIDC"

GitHub OIDC (Workload Identity Federation) is for federating *GitHub Actions*
to cloud providers. ArgoCD does **not** use it. ArgoCD authenticates to GitHub
via **GitHub App credentials** (App ID + Installation ID + PEM private key)
stored in the Repository Secret. Three things to do once:

1. Create a GitHub App in the org → install on this repo only → grant `Contents: Read`.
2. Note the App ID + Installation ID; download the private key PEM.
3. Store all three in Vault, then render and apply the Secret.

After that the secret can be re-rendered from Vault (one-time) or auto-synced
via External Secrets Operator (commented template provided in the Secret).

### Sync waves (ordering)

| Wave | Member | Why first |
|---|---|---|
| 0 | `_shared` *(Phase 0b)* | Creates `agent-system` namespace + RWX skills PVC; everything else mounts the PVC |
| 1 | `openshell` *(Phase 1)* | Sandbox runtime; orchestrators delegate code-exec to it |
| 2 | `openclaw` + `hermes` | Run last so all dependencies exist |

Phase 0 only deploys wave 2 (the orchestrators in their existing per-agent
namespaces). Waves 0 and 1 are commented in the ApplicationSet, ready to
uncomment when those directories exist.

### Bootstrap order (one-time)

```bash
# 1. Install ArgoCD itself (bootstrap — Helm chart + namespace + ingress)
kubectl apply -k kubernetes-addons/0-boostrap/gitops/argocd

# 2. Populate the GitHub App Secret in Vault
vault kv put secret/homelab/argocd/repo-homelab-github-app \
  github_app_id="<APP_ID>" \
  github_app_installation_id="<INSTALLATION_ID>" \
  github_app_private_key="$(cat homelab-argocd.private-key.pem)"

# Render the placeholder Secret with real values, then apply
# (Or wire up External Secrets Operator — commented template in the Secret file).

# 3. Apply the ongoing GitOps config — projects, repo Secrets, ApplicationSets.
#    Lives outside the bootstrap tree on purpose.
kubectl apply -k kubernetes-addons/0-georgehomelab-argocd-projects

# 4. ArgoCD picks up the AppProject + ApplicationSet on its next reconcile.
#    Watch the fleet come up:
argocd app list --project georgehomelab
argocd app sync agent-openclaw agent-hermes

# 5. Verify
kubectl get applications -n argocd -l app.kubernetes.io/part-of=agent-fleet-system
```

---

## Decision log

| Date | Decision | Reasoning |
|---|---|---|
| 2026-04-29 | Consolidate to one `agent-system` namespace | Cross-agent RPC + shared skill volume; per-agent SAs preserve isolation |
| 2026-04-29 | Hybrid OpenClaw + Hermes (not one-or-the-other) | Community evidence: complementary strengths (strong-model orchestration vs. local-model + learning loop) |
| 2026-04-29 | Drop NemoClaw / QwenPaw / HiClaw as separate pods | Model choice is config; planners exist natively in both orchestrators |
| 2026-04-29 | Defer ReMe to Phase 2 | Hermes' built-in observer may suffice; measure first |
| 2026-04-29 | Shared skill registry as CephFS RWX (not custom service) | SKILL.md is portable; service is over-engineering for Phase 0 |
| 2026-04-29 | OpenShell mandatory (not optional) | 12% ClawHub malware rate forces sandbox |
| 2026-04-29 | Use Kustomize at fleet root for one-shot apply | Matches existing repo pattern (cnpg, gitops, etc.) |
| 2026-04-29 | Two hostnames only (`openclaw`, `hermes`) — never `ceph-fs`/`agent-system`/etc. | VirtualServices are for external HTTPS UIs only; storage/namespaces/internal services use cluster DNS |
| 2026-04-29 | ArgoCD ApplicationSet (list generator) over plain Application | Per-agent sync waves; deterministic (archived dirs not scanned); adding a member is one list entry |
| 2026-04-29 | GitHub App auth (not PAT or SSH) for private repo | Per-repo permissions, rotation-friendly, survives personnel changes |
| 2026-04-30 | **Single AppProject `georgehomelab`** for the whole homelab (not per-fleet) | One place to audit RBAC + sourceRepos + destinations; ApplicationSets carry the fleet boundary via labels. Stricter projects can coexist in the same file when blast-radius needs it. |
| 2026-04-30 | **ApplicationSets** primary pattern; **app-of-apps** demoted to bootstrap-only | App-of-apps doesn't scale (one YAML per app); ApplicationSet generates N from a single manifest, supports multi-cluster / multi-repo / multi-env via element parameters. |
| 2026-04-30 | Each list element is **fully parameterized** (`cluster`, `repoURL`, `revision`, `path`, `namespace`, `syncWave`) | Same ApplicationSet can target different folders, repos, clusters, environments — no duplication when scaling out. |
| 2026-04-30 | Move ArgoCD GitOps config **outside** `0-boostrap/gitops/argocd/` to top-level `0-georgehomelab-argocd-projects/` | Bootstrap (install ArgoCD) and ongoing GitOps config (projects/secrets/appsets) have different lifecycles. Separating them prevents ArgoCD version bumps from drifting fleet config. |

---

## File layout (target)

```
Autonomous-ai-agents/
├── kustomization.yml                      ← fleet root (this directory)
├── README.md
├── AUTONOMOUS-AGENT-FLEET-PLAN.md         ← this file
│
├── _shared/                               ← cross-agent resources
│   ├── namespace.yaml                     (agent-system)
│   ├── shared-skills-pvc.yaml             (CephFS RWX, 50Gi)
│   └── kustomization.yaml
│
├── openclaw/                              orchestrator (Gateway-first)
│   ├── deployment.yaml                    namespace: agent-system
│   ├── configmap.yaml
│   ├── secret.yaml
│   ├── pvc.yaml                           (RWO, openclaw-workspace)
│   ├── service.yaml
│   ├── serviceaccount.yaml
│   ├── rbac.yaml
│   ├── virtualservice.yaml
│   ├── hpa.yaml
│   └── kustomization.yaml
│
├── hermes/                                orchestrator (runtime-first, learning loop)
│   ├── deployment.yaml                    namespace: agent-system, opt-in flags ON
│   ├── configmap.yaml
│   ├── secret.yaml
│   ├── pvc.yaml                           (RWO, hermes-data)
│   ├── service.yaml
│   ├── serviceaccount.yaml
│   ├── rbac.yaml
│   ├── virtualservice.yaml
│   └── kustomization.yaml
│
├── openshell/                             ← NEW: shared sandbox runtime
│   ├── deployment.yaml                    namespace: agent-system
│   ├── service.yaml
│   ├── serviceaccount.yaml
│   ├── rbac.yaml
│   └── kustomization.yaml
│
└── _archived/                             ← old AgentScope-era agents
    ├── nemoclaw/                          (kept for reference, not deployed)
    ├── QwenPaw/
    ├── HiClaw/
    ├── ReMe/
    └── skills/
```

`_archived/` is symbolic — we'll keep the dirs in place but exclude them from the fleet `kustomization.yml`. They're kept on disk for reference (and so the work isn't lost) but are not applied.

---

## Decision gate at end of Phase 1

Two weeks of real use. Then answer honestly:

| Metric | Action if low | Action if high |
|---|---|---|
| Skill registry hit rate < 15% | **Stop.** Don't build Phase 2 — there's no signal to amplify. | Proceed to Phase 2. |
| Hermes auto-skill creation produces useful skills | Disable it. Keep manual. | Keep it on. |
| OpenClaw + Hermes overlap > 80% (you only use one) | Drop the unused one. | Keep both. |
| Token cost > $80/month | Reconsider scope or move more to local models. | Continue. |

The honest version of this plan accepts that a single-user homelab may not generate enough task volume to justify the full fleet. That's a valid outcome — you'd end up running just Hermes + OpenShell, which is still a real win over the old 6-pod plan.
