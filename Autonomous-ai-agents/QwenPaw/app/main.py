"""
QwenPaw Agent Server — Multilingual Language Specialist
Model: Qwen2.5-72B (Ollama in-cluster or Alibaba DashScope)

Unique endpoints:
  POST /api/translate        -> translate text to target language
  POST /api/detect           -> detect language with confidence
  POST /api/summarize        -> summarize in requested language
  POST /api/chat             -> multilingual free-form conversation
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import agentscope
from agentscope.agents import DialogAgent
from agentscope.message import Msg
from fastapi import FastAPI, HTTPException, Header
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from starlette.responses import Response

try:
    from langdetect import detect as langdetect_detect, detect_langs
    HAS_LANGDETECT = True
except ImportError:
    HAS_LANGDETECT = False

# ── Config ────────────────────────────────────────────────────────────────────
LLM_BACKEND = os.getenv("LLM_BACKEND", "ollama")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama.ollama.svc.cluster.local:11434/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:72b")
DASHSCOPE_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
DASHSCOPE_MODEL = os.getenv("DASHSCOPE_MODEL", "qwen2.5-72b-instruct")
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "/workspace"))
DEFAULT_OUTPUT_LANG = os.getenv("DEFAULT_OUTPUT_LANG", "en")
MAX_CHARS = int(os.getenv("MAX_TRANSLATION_CHARS", "50000"))

SYSTEM_PROMPT_PATH = Path("/app/config/system_prompt.txt")

LANG_NAMES = {
    "zh": "Chinese (Mandarin)", "ja": "Japanese", "ko": "Korean",
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian", "ar": "Arabic",
    "hi": "Hindi", "tr": "Turkish", "vi": "Vietnamese", "th": "Thai",
    "id": "Indonesian", "nl": "Dutch", "pl": "Polish", "uk": "Ukrainian",
    "sv": "Swedish", "no": "Norwegian", "da": "Danish", "fi": "Finnish",
    "el": "Greek", "he": "Hebrew", "fa": "Persian", "bn": "Bengali",
    "ur": "Urdu", "ta": "Tamil", "te": "Telugu", "ms": "Malay",
    "tl": "Filipino", "sw": "Swahili",
}

# ── Metrics ───────────────────────────────────────────────────────────────────
TRANSLATE_TOTAL = Counter("qwenpaw_translations_total", "Translations", ["target_lang"])
TRANSLATE_LATENCY = Histogram("qwenpaw_translation_latency_seconds", "Translation latency")
DETECT_TOTAL = Counter("qwenpaw_detections_total", "Language detections")
CHAT_TOTAL = Counter("qwenpaw_chats_total", "Chat requests")

agent: DialogAgent | None = None


def build_model_config():
    if LLM_BACKEND == "dashscope" and DASHSCOPE_API_KEY:
        return [{"config_name": "qwen-dashscope", "model_type": "openai_chat",
                 "model_name": DASHSCOPE_MODEL, "api_key": DASHSCOPE_API_KEY,
                 "client_args": {"base_url": DASHSCOPE_BASE_URL},
                 "generate_args": {"temperature": 0.3, "max_tokens": 8192}}]
    return [{"config_name": "qwen-ollama", "model_type": "openai_chat",
             "model_name": OLLAMA_MODEL, "api_key": "ollama",
             "client_args": {"base_url": OLLAMA_BASE_URL},
             "generate_args": {"temperature": 0.3, "max_tokens": 4096}}]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent
    model_config_path = Path("/app/config/model_config.json")
    if model_config_path.exists():
        raw = json.loads(model_config_path.read_text())
        # Inject DashScope key at runtime
        for cfg in raw:
            if cfg.get("config_name") == "qwen-dashscope":
                cfg["api_key"] = DASHSCOPE_API_KEY or cfg.get("api_key", "")
        model_configs = raw
    else:
        model_configs = build_model_config()

    agentscope.init(
        model_configs=model_configs,
        save_dir=str(WORKSPACE_DIR / "agentscope_runs"),
        project="qwenpaw",
    )
    sys_prompt = SYSTEM_PROMPT_PATH.read_text() if SYSTEM_PROMPT_PATH.exists() else "You are QwenPaw, a multilingual agent."
    config_name = "qwen-dashscope" if (LLM_BACKEND == "dashscope" and DASHSCOPE_API_KEY) else "qwen-ollama"
    agent = DialogAgent(name="QwenPaw", sys_prompt=sys_prompt, model_config_name=config_name)
    yield


app = FastAPI(title="QwenPaw Multilingual Agent API", version="0.1.0", lifespan=lifespan)


def verify_api_key(key: str | None) -> None:
    if AGENT_API_KEY and key != AGENT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Request models ────────────────────────────────────────────────────────────
class TranslateRequest(BaseModel):
    text: str
    target_lang: str = Field(default="en", description="BCP-47 language code, e.g. zh, ja, fr")
    source_lang: str | None = Field(default=None, description="Optional source language hint")
    preserve_formatting: bool = True

class DetectRequest(BaseModel):
    text: str

class SummarizeRequest(BaseModel):
    text: str
    output_lang: str = Field(default="en", description="Language for the summary")
    max_words: int = 150

class ChatRequest(BaseModel):
    message: str
    output_lang: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "agent": "qwenpaw", "backend": LLM_BACKEND, "model": OLLAMA_MODEL if LLM_BACKEND == "ollama" else DASHSCOPE_MODEL}

@app.get("/ready")
def ready():
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    return {"status": "ready"}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/api/languages")
def list_languages():
    return {"supported_languages": LANG_NAMES, "total": len(LANG_NAMES)}

@app.get("/api/agents")
def list_agents():
    return {"agents": [{"name": "QwenPaw", "role": "multilingual-specialist",
                        "backend": LLM_BACKEND, "languages": len(LANG_NAMES)}]}


@app.post("/api/translate")
async def translate(req: TranslateRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    if len(req.text) > MAX_CHARS:
        raise HTTPException(status_code=400, detail=f"Text exceeds max {MAX_CHARS} characters")

    target_name = LANG_NAMES.get(req.target_lang, req.target_lang)
    src_hint = f" (source language: {LANG_NAMES.get(req.source_lang, req.source_lang)})" if req.source_lang else ""

    prompt = (
        f"Translate the following text to {target_name}{src_hint}.
"
        + ("Preserve all formatting, line breaks, and special characters.
" if req.preserve_formatting else "")
        + "Output ONLY the translated text, nothing else.

"
        + f"Text to translate:
{req.text}"
    )

    start = time.time()
    msg = Msg(name="user", content=prompt, role="user")
    response = await asyncio.to_thread(agent, msg)
    elapsed = time.time() - start

    TRANSLATE_TOTAL.labels(target_lang=req.target_lang).inc()
    TRANSLATE_LATENCY.observe(elapsed)

    return {
        "translated_text": response.content.strip(),
        "source_lang": req.source_lang or "auto-detected",
        "target_lang": req.target_lang,
        "target_lang_name": target_name,
        "latency_seconds": round(elapsed, 3),
        "char_count": len(req.text),
    }


@app.post("/api/detect")
async def detect_language(req: DetectRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    DETECT_TOTAL.inc()

    # Fast path: use langdetect if available
    if HAS_LANGDETECT and len(req.text) >= 10:
        try:
            langs = detect_langs(req.text)
            top = langs[0]
            return {
                "language": top.lang,
                "language_name": LANG_NAMES.get(top.lang, top.lang),
                "confidence": round(top.prob, 4),
                "alternatives": [{"lang": l.lang, "prob": round(l.prob, 4)} for l in langs[:3]],
                "method": "langdetect",
            }
        except Exception:
            pass

    # Fallback: ask the LLM
    prompt = (
        "Detect the language of the following text. "
        'Output ONLY a JSON object: {"language": "<BCP47 code>", "confidence": <0-1>, "script": "<script name>"}.

'
        f"Text:
{req.text[:500]}"
    )
    msg = Msg(name="user", content=prompt, role="user")
    response = await asyncio.to_thread(agent, msg)
    try:
        result = json.loads(response.content.strip())
        result["language_name"] = LANG_NAMES.get(result.get("language", ""), result.get("language", ""))
        result["method"] = "llm"
        return result
    except json.JSONDecodeError:
        return {"raw_response": response.content, "method": "llm", "parse_error": True}


@app.post("/api/summarize")
async def summarize(req: SummarizeRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    lang_name = LANG_NAMES.get(req.output_lang, req.output_lang)
    prompt = (
        f"Summarize the following text in {lang_name} in approximately {req.max_words} words. "
        "Focus on the key points. Output ONLY the summary.

"
        f"Text:
{req.text}"
    )
    msg = Msg(name="user", content=prompt, role="user")
    response = await asyncio.to_thread(agent, msg)
    return {
        "summary": response.content.strip(),
        "output_lang": req.output_lang,
        "output_lang_name": lang_name,
        "original_chars": len(req.text),
    }


@app.post("/api/chat")
async def chat(req: ChatRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    CHAT_TOTAL.inc()
    content = req.message
    if req.output_lang and req.output_lang != DEFAULT_OUTPUT_LANG:
        lang_name = LANG_NAMES.get(req.output_lang, req.output_lang)
        content = f"[Please respond in {lang_name}]

{req.message}"
    msg = Msg(name="user", content=content, role="user")
    response = await asyncio.to_thread(agent, msg)
    return {"response": response.content, "agent": "QwenPaw", "backend": LLM_BACKEND}
