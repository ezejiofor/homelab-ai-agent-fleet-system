# Hermes Agent — Kubernetes deployment

Direct port of the upstream [`docker-compose.yml`](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/docker-compose.yml)
to Kubernetes manifests, using the official image `docker.io/nousresearch/hermes-agent:latest`
(no local build required — plugins are baked into the image).

## Compose → Kubernetes mapping

| docker-compose | Kubernetes |
|---|---|
| `services.gateway` (`["gateway", "run"]`) | `deployment.yaml` container `gateway` |
| `services.dashboard` (`["dashboard", "--host", "127.0.0.1", "--no-open"]`) | `deployment.yaml` container `dashboard` (bound 0.0.0.0, fronted by Istio + auth) |
| `network_mode: host` (shared localhost between gateway + dashboard) | Both containers in the **same Pod** (shared net namespace) |
| `~/.hermes:/opt/data` | PVC `hermes-data` (`pvc.yaml`) mounted at `/opt/data` in both containers |
| `HERMES_UID=${HERMES_UID:-10000}` / `HERMES_GID` | `ConfigMap hermes-config` keys, plus `fsGroup: 10000` |
| `depends_on: [gateway]` | Containers start in spec order; gateway listed first |
| `restart: unless-stopped` | Deployment + `Recreate` strategy |
| Optional `API_SERVER_HOST` / `API_SERVER_KEY` | Commented in `configmap.yaml` + `secret.yaml`; uncomment Service port too |

## First-time bootstrap

The image expects an interactive `hermes setup` to write `/opt/data/config.toml`.
Run it once against the live pod, then traffic will flow:

```bash
kubectl -n agents-system apply -k .
kubectl -n agents-system wait --for=condition=Ready pod -l app.kubernetes.io/name=hermes --timeout=300s
kubectl -n agents-system exec -it deploy/hermes -c gateway -- hermes setup
```

The wizard pick-list (model provider, messaging platforms, etc.) writes to the
PVC and persists across pod restarts. Re-run with `hermes config set <key> <val>`
or `hermes setup` to reconfigure.

## Secrets

Vault Agent sidecar injects `/vault/secrets/env` from `secret/homelab/ai/hermes`.
Seed it with:

```bash
vault kv put secret/homelab/ai/hermes \
  openrouter_api_key="sk-or-..." \
  openai_api_key="sk-..." \
  anthropic_api_key="sk-ant-..." \
  nous_portal_api_key="..." \
  telegram_bot_token="..." \
  discord_bot_token="..." \
  slack_bot_token="xoxb-..." \
  elevenlabs_api_key="..." \
  api_server_key="$(openssl rand -hex 32)"
```

## Access

- Dashboard: <https://hermes.georgehomelab.com> (gated by oauth2-proxy + Keycloak)
- Gateway: outbound-only — talks to Telegram / Discord / Slack / Signal / Email
  using the bot tokens from Vault.

## Known caveats

- Single replica only — RWO PVC, agent state in SQLite (FTS5).
- Containers must start as root briefly so the upstream entrypoint can
  `usermod` to `HERMES_UID/GID` then `gosu` down. `runAsNonRoot: false` is
  intentional; capabilities are still dropped to `CHOWN, SETUID, SETGID`.
- Dashboard binds `0.0.0.0` inside the pod (vs. `127.0.0.1` in compose) so the
  Service can reach it. **Never expose without auth** — it stores API keys.