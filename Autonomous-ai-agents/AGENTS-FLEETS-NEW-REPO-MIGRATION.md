# Agent Fleet — Migration to a Dedicated Repository

> Status: **planned**, not started. Drafted 2026-05-03 after fleet end-to-end
> went live (Hermes + OpenClaw + Open WebUI all responsive via Keycloak SSO).

## Why split the agent fleet out

This repository (`homelab-proxmox-k8s-terraform-ansible-gitops`) is the
**cluster bootstrap**: Proxmox, RKE2, Istio, Vault, Keycloak, Rook-Ceph,
ArgoCD, networking. It changes quarterly and is owned by the homelab
infra operator (one person).

Agent fleet management is a **workload** on top of that platform:

- It will scale to dozens of agents (SRE team × 10 OpenClaw, AI-SMA team
  × 10 OpenClaw, Hermes for skill authoring, …).
- Per-team RBAC, Vault paths, Keycloak groups, namespaces, ServiceAccounts.
- Different change cadence — agent prompts, IDENTITY.md, skills, model
  versions all change daily. Istio configs do not.
- Different audience — SRE/AI engineers PR'ing agent definitions don't
  need to learn Proxmox/Terraform/Ansible.

Concerns split → repo splits.

## Target repository

```
homelab-ai-agent-fleet           (new repo, planned)
├── platform/                    ArgoCD AppProject, ApplicationSets,
│                                root agent-fleet bootstrap
├── identity/                    Keycloak realms / groups for the fleet,
│                                generated with gen_realms.py
├── agents/
│   ├── _shared/                 namespace, NetworkPolicy baseline, PSA
│   │                            labels, PeerAuthentication, default
│   │                            Vault auth, default ServiceAccount
│   ├── teams/
│   │   ├── sre/                 team-level Keycloak group, Vault prefix,
│   │   │   │                    project-scoped ArgoCD AppProject
│   │   │   ├── openclaw-01/     per-agent overlay (model, skills,
│   │   │   ├── openclaw-02/     IDENTITY.md, USER.md, sandbox)
│   │   │   ├── …
│   │   │   └── openclaw-10/
│   │   ├── ai-sma/
│   │   │   └── openclaw-{01..10}/
│   │   └── platform/
│   │       └── hermes-skill-author/   single Hermes — writes skills
│   │                                  consumed by the openclaw fleet
│   └── overlay-templates/       kustomize bases (openclaw-base/,
│                                hermes-base/) used by team overlays
├── skills/                      shared Hermes-authored skill library
│                                versioned, consumed by all openclaw agents
├── sandboxes/                   OpenShell sandbox images per team
│                                (kubectl, terraform, gh CLI, helm, etc.)
│                                with scoped ServiceAccount + kubeconfig
└── ops/
    ├── README.md                operator runbook
    ├── ONBOARDING.md            "how to add a new team / new agent"
    └── INCIDENTS.md             past quota / SSO / sandbox failure recipes
```

## Naming conventions

To pick **before** the migration:

- **Agent identity**: `<team>-openclaw-<NN>` (DNS-safe, kebab) — e.g.
  `sre-openclaw-01`, `aisma-openclaw-03`, `platform-hermes-01`.
- **Service hostname**: `<team>-openclaw-<NN>.georgehomelab.com` —
  edge-routed via Istio; oauth2-proxy realm per team OR per agent.
- **Namespace per team**: `agents-<team>` (e.g. `agents-sre`,
  `agents-aisma`, `agents-platform`). One ServiceAccount per agent,
  scoped to its own namespace.
- **Vault path**: `secret/agents/<team>/<agent>/...` — a Vault
  ServiceAccount per team can read its own subtree only.
- **Keycloak**: open question — see below.

## Keycloak realm strategy (decision needed)

| Option | Pro | Con |
|---|---|---|
| **One realm, team groups** (`team:sre`, `team:aisma`) mapped to ArgoCD project / Vault policy / agent RBAC | Simpler ops, one client per agent, single SSO login | Cross-team isolation depends on group claim plumbing |
| **Realm per team** | Stronger isolation, tenants can't see each other's clients | 5-20 realms to maintain; gen_realms.py becomes more complex |

**Lean**: one realm, team groups. Simpler matches our scale (10s of
agents, single homelab). Re-evaluate if isolation requirements harden.

## Migration steps (tomorrow)

1. **Create the new repo** `homelab-ai-agent-fleet` (GitHub, private),
   add `.gitignore`, `README.md`, MIT license.

2. **Move agent code preserving git history** from this repo to new repo
   using `git filter-repo`:
   ```bash
   # In a clone of the existing repo:
   git filter-repo --path kubernetes-addons/Autonomous-ai-agents/ --path-rename kubernetes-addons/Autonomous-ai-agents/:agents/
   git filter-repo --path kubernetes-addons/0-boostrap/oauth2-proxy/overlays/openclaw/ --path-rename …:agents/teams/_legacy/openclaw/
   git filter-repo --path kubernetes-addons/0-boostrap/oauth2-proxy/overlays/hermes/ --path-rename …:agents/teams/_legacy/hermes/
   git filter-repo --path kubernetes-addons/0-boostrap/oauth2-proxy/overlays/open-webui/ --path-rename …:agents/_shared/open-webui-overlay/
   git filter-repo --path kubernetes-addons/0-boostrap/keycloak/realms/openclaw-realm.json --path-rename …:identity/realms/legacy-openclaw-realm.json
   git filter-repo --path kubernetes-addons/0-boostrap/keycloak/realms/hermes-realm.json --path-rename …:identity/realms/legacy-hermes-realm.json
   git filter-repo --path kubernetes-addons/0-boostrap/keycloak/realms/open-webui-realm.json --path-rename …:identity/realms/legacy-open-webui-realm.json
   ```

3. **Restructure** the moved tree into the target layout above. Convert
   the existing single `openclaw/` and `hermes/` folders into
   `kustomize bases` under `agents/overlay-templates/`. Each team folder
   then becomes thin overlays referencing the base + per-agent
   IDENTITY.md / USER.md / model.

4. **Delete the agent code from the old repo** in a separate commit so
   the platform repo stops being the source of truth. Update the
   ApplicationSet `agent-fleet-system.yaml` to either:
   - Be deleted (move to new repo), OR
   - Be turned into a thin pointer to the new repo's ApplicationSet.

5. **Wire the existing ArgoCD on the cluster** to watch the new repo:
   - Add new repo URL to `argocd-cm` (or via `kubectl create secret` of
     type `repository`).
   - Create a new top-level Application `agent-fleet-platform` pointing
     at `homelab-ai-agent-fleet/platform/` — the "app of apps" that
     spawns per-team ApplicationSets.

6. **Re-test end-to-end**:
   - One agent per team (sre/openclaw-01, aisma/openclaw-01,
     platform/hermes-01) loads, signs in via Keycloak, responds.
   - Cross-team RBAC: SRE user can see only `agents-sre/*` apps in
     ArgoCD, not `agents-aisma/*`.
   - Vault path scoping: `agents-sre/openclaw-01` SA can read
     `secret/agents/sre/*` but not `secret/agents/aisma/*`.

7. **Cutover** — point DNS / oauth2-proxy at the new agent endpoints,
   delete the legacy `agents-system` namespace once nothing references
   it, archive the old `Autonomous-ai-agents/` directory.

## What stays in THIS repo after migration

- The platform: Proxmox, RKE2, Vault, Keycloak, Rook-Ceph, Istio,
  ArgoCD, networking, monitoring.
- The shared `oauth2-proxy/base/` (referenced by the new repo's
  overlays — *or* copied into the new repo if we want full
  independence; lean: copy, no cross-repo dependency).
- The shared `gen_realms.py` (or move it to the new repo since the
  fleet realms grow there; platform's argocd/rancher/vault/ceph realms
  could stay here).

Decision to make: **does `gen_realms.py` move with the fleet?** The
platform realms (argocd, rancher, vault, ceph) are tied to
infra-team SSO; the fleet realms (per agent / per team) are tied to
the fleet. Cleanest split: **two `gen_realms.py` instances**, one in
each repo, each generating its own realm set against the same
Keycloak instance.

## Open questions / pre-migration decisions

These will be revisited at the start of the migration session:

1. Naming convention: `<team>-openclaw-<NN>` vs `<team>/openclaw/<NN>`?
2. Keycloak: one realm with groups, or realm-per-team?
3. Vault: HCL policies generated from agent metadata, or hand-written
   per team and copy-pasted from a template?
4. Sandbox images (OpenShell): one image with everything, or per-team
   images (sre image with kubectl/helm; aisma image with cuda/torch)?
5. Open WebUI: stay in the platform repo (one shared frontend) or move
   to the fleet repo? **Lean: stay here** — it's frontend infrastructure
   serving multiple agent backends.
6. Ansible role for OpenShell sandbox image build pipeline — platform
   repo or fleet repo? **Lean: fleet repo** since image content is
   fleet-specific.

## Reference: where the live state is right now

Before migration, the fleet lives at:

| Component | Path in this repo |
|---|---|
| Hermes | `kubernetes-addons/Autonomous-ai-agents/hermes/` |
| OpenClaw | `kubernetes-addons/Autonomous-ai-agents/openclaw/` |
| OpenShell | `kubernetes-addons/Autonomous-ai-agents/openshell/` |
| Open WebUI | `kubernetes-addons/Autonomous-ai-agents/open-webui/` |
| Shared agent base | `kubernetes-addons/Autonomous-ai-agents/_shared/` |
| Sandbox controller | `kubernetes-addons/Autonomous-ai-agents/agent-sandbox-controller/` |
| oauth2-proxy overlays | `kubernetes-addons/0-boostrap/oauth2-proxy/overlays/{openclaw,hermes,open-webui}/` |
| Keycloak realm JSON | `kubernetes-addons/0-boostrap/keycloak/realms/{openclaw,hermes,open-webui}-realm.json` |
| Realm generator | `kubernetes-addons/0-boostrap/keycloak/realms/gen_realms.py` |
| Vault TF for agent secrets | `terraform/vault-config/modules/secrets/ai-agents-secrets.tf` |
| Vault tfvars | `terraform/vault-config/environment/prod/.secrets.auto.tfvars` (gitignored) |
| ArgoCD ApplicationSet | `kubernetes-addons/0-georgehomelab-argocd-projects/2-argocd-applicationsets/agent-fleet-system.yaml` |

Operator runbooks already in place (will move with the migration):

- `kubernetes-addons/Autonomous-ai-agents/openclaw/SSO-AND-DEVICE-PAIRING.md`
- `kubernetes-addons/Autonomous-ai-agents/open-webui/SSO-AND-ARCHITECTURE.md`

## Bug history captured (so the new repo doesn't re-discover)

A condensed list of issues we hit getting the fleet live, so the new
repo can ship with these baked-in from day 1:

1. **Keycloak realm import via admin API doesn't resolve `${GOOGLE_CLIENT_ID}`
   placeholders** — only file-mount import on pod startup does. → Always
   import via configmap + StatefulSet restart, never via admin API directly.
2. **OpenClaw config rollback** — gateway reverts `openclaw.json` to
   `.last-good` if cm-pushed copy fails meta-checksum. → Init container
   deletes `.last-good` and `.bak` after copying from cm.
3. **Istio outbound vhost matches by Host header** — oauth2-proxy
   forwarding the public Host meant Envoy fell to PassthroughCluster
   (no mTLS). → `OAUTH2_PROXY_PASS_HOST_HEADER=false`.
4. **Service port needs `appProtocol: http`** — without it Istio creates
   only a TCP filter chain on inbound mTLS, blocking HTTP requests with
   `filter_chain_not_found`.
5. **DNS flake from istio-proxy** — aiohttp doesn't retry transient DNS
   failures. → `hostAliases` pin for stable Service IPs.
6. **`$(VAR)` env substitution requires earlier definition** — list a
   Secret-derived env var BEFORE referencing it via `$(VAR)`.
7. **Open WebUI 401 spinner** — oauth2-proxy was injecting
   `Authorization: Bearer <id-token>` upstream, which Open WebUI tried
   to validate as its session cookie. → Disable
   `PASS_AUTHORIZATION_HEADER` / `SET_AUTHORIZATION_HEADER` for apps
   that have their own local auth.
8. **OpenClaw model dropdown duplicate** — `openclaw` and
   `openclaw/default` both route to the same agent. → Filter via
   `MODEL_FILTER_LIST=hermes-agent;openclaw/default`.
9. **Gemini free-tier quota=0 for stable models on these projects** —
   `gemini-2.5-flash-lite` and `gemini-2.0-flash` return 429 with
   `limit:0`. Only `gemini-flash-latest` (currently aliased to
   `gemini-3-flash-preview`) works on free tier.
10. **Per-minute Gemini rate-limit (~10 req/min on free tier)** —
    medium/high reasoning_effort burns through this in one chat turn,
    leaving the next turn stuck on "loading". → Pin both agents to
    minimal/low reasoning_effort.
11. **Agents auto-create placeholder IDENTITY.md / USER.md** which the
    `[ ! -s file ]` seed-check thinks is "user content". → Match against
    placeholder marker phrases instead.
12. **OpenShell has no UI** — gRPC sandbox controller (port 8080,
    appProtocol: grpc), don't try to oauth2-proxy it.
13. **Sandboxing rule belongs in the persona file**, not in agent
    config — neither agent's stable config supports routing terminal
    calls through OpenShell. The LLM follows persona instructions
    reliably; treat that as the enforcement layer until OpenShell
    becomes an MCP tool.

## Next session — kickoff checklist

When you start the migration tomorrow, in this order:

- [ ] Confirm the 6 pre-migration decisions above.
- [ ] Create `homelab-ai-agent-fleet` on GitHub (private).
- [ ] Run `git filter-repo` on a clone of THIS repo to extract agent
      paths preserving history; push to new repo.
- [ ] Restructure into the target layout in a single PR.
- [ ] Wire ArgoCD on the cluster to the new repo (add as repo, add
      `agent-fleet-platform` Application, watch it sync).
- [ ] Bring up first-team agents in parallel namespace (`agents-sre`)
      so the existing `agents-system` keeps running until cutover.
- [ ] Smoke-test SSO end-to-end on one new agent.
- [ ] Cutover DNS, then archive `kubernetes-addons/Autonomous-ai-agents/`
      in this repo (rename to `*.archive` rather than delete, for
      one release cycle, in case rollback is needed).
- [ ] Update `CLAUDE.md` / repo README in BOTH repos with
      cross-references so the next operator (or future-you) knows
      where to look for what.