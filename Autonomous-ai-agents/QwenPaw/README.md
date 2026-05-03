# QwenPaw - Multilingual Language Specialist Agent

> Part of the homelab Autonomous AI Agent fleet

## Role

QwenPaw is the **language and multilingual specialist** of the agent fleet.
Powered by Qwen2.5-72B (via Ollama in-cluster, or Alibaba DashScope API).

Capabilities:
- Translation between 30+ languages
- Language detection (langdetect fast path + LLM fallback)
- Multilingual summarization (summarize in target language)
- CJK text processing (Chinese/Japanese/Korean)
- Cross-lingual Q&A

## Key Endpoints

| Endpoint | Purpose |
|----------|--------|
| POST /api/translate | Translate text to any supported language |
| POST /api/detect | Detect language with confidence score |
| POST /api/summarize | Summarize text in requested output language |
| GET  /api/languages | List all 30+ supported languages |
| POST /api/chat | Free-form multilingual conversation |

## Inference Backends

| Backend | Model | Config |
|---------|-------|--------|
| Ollama (default) | qwen2.5:72b | In-cluster, no API key |
| DashScope | qwen2.5-72b-instruct | Alibaba API, requires DASHSCOPE_API_KEY |

## Deploy

```bash
# Pull Qwen2.5 into Ollama first
kubectl exec -n ollama deploy/ollama -- ollama pull qwen2.5:72b

vault kv put secret/homelab/ai/qwenpaw \
  agent_api_key='$(openssl rand -hex 32)' \
  dashscope_api_key='CHANGE-ME-or-empty'

vault write auth/kubernetes/role/qwenpaw \
  bound_service_account_names=qwenpaw \
  bound_service_account_namespaces=qwenpaw \
  token_policies=app-read token_ttl=3600

docker build -t ghcr.io/<user>/qwenpaw:latest . && docker push ghcr.io/<user>/qwenpaw:latest
kubectl apply -k kubernetes-addons/Autonomous-ai-agents/QwenPaw/
```

## Example

```bash
# Translate English to Japanese
curl -X POST https://qwenpaw.georgehomelab.com/api/translate \
  -H 'X-API-Key: <key>' -H 'Content-Type: application/json' \
  -d '{"text": "Deploy the monitoring stack", "target_lang": "ja"}'

# Detect language
curl -X POST https://qwenpaw.georgehomelab.com/api/detect \
  -H 'X-API-Key: <key>' -H 'Content-Type: application/json' \
  -d '{"text": "Bonjour, comment allez-vous?"}'
```

## Switch to DashScope backend

```bash
kubectl set env deploy/qwenpaw -n qwenpaw LLM_BACKEND=dashscope
# Ensure DASHSCOPE_API_KEY is set in Vault secret
```
