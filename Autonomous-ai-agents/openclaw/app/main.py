"""
OpenClaw — AgentScope Autonomous Agent Server
============================================
FastAPI wrapper that starts AgentScope Studio and exposes a task API.

Endpoints:
  GET  /health          — liveness probe
  GET  /ready           — readiness probe
  GET  /metrics         — Prometheus metrics
  POST /api/task        — submit an agent task (async)
  GET  /api/task/{id}   — poll task status/result
  GET  /api/tasks       — list recent tasks
  POST /api/chat        — synchronous single-turn chat
  GET  /api/agents      — list available agents
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import agentscope
from agentscope.agents import DialogAgent
from agentscope.message import Msg
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(os.getenv("LOG_DIR", "/workspace/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "openclaw.log"),
    ],
)
log = logging.getLogger("openclaw")

# ── Config ────────────────────────────────────────────────────────────────────
WORKSPACE = Path(os.getenv("WORKSPACE", "/workspace"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/app/config"))
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
AGENT_NAME = os.getenv("AGENT_NAME", "OpenClaw")
LLM_BACKEND = os.getenv("LLM_BACKEND", "ollama")

# Load Vault-injected env vars if present
vault_secrets = Path(os.getenv("VAULT_SECRETS_FILE", "/vault/secrets/env"))
if vault_secrets.exists():
    for line in vault_secrets.read_text().splitlines():
        if line.startswith("export "):
            key, _, val = line[7:].partition("=")
            os.environ[key] = val.strip().strip('"')
    log.info("Vault secrets loaded from %s", vault_secrets)

# ── In-memory task store (replace with Redis for production HA) ───────────────
tasks: dict[str, dict] = {}

# ── AgentScope init ───────────────────────────────────────────────────────────
agent: DialogAgent | None = None


def load_model_config() -> list[dict]:
    """Load model configs, substituting env vars."""
    cfg_path = CONFIG_DIR / "model_config.json"
    if not cfg_path.exists():
        log.warning("model_config.json not found — using default Ollama config")
        return [
            {
                "model_type": "openai_chat",
                "config_name": "ollama-local",
                "model_name": os.getenv("OLLAMA_MODEL", "qwen2.5:14b"),
                "api_key": "ollama",
                "client_args": {
                    "base_url": os.getenv(
                        "OLLAMA_BASE_URL",
                        "http://ollama.ollama.svc.cluster.local:11434/v1",
                    )
                },
                "generate_args": {"temperature": 0.7, "max_tokens": 4096},
            }
        ]
    raw = cfg_path.read_text()
    # Substitute env vars  ${VAR}
    for key, val in os.environ.items():
        raw = raw.replace(f"${{{key}}}", val)
    return json.loads(raw)


def init_agent() -> DialogAgent:
    """Initialise AgentScope and return the main dialog agent."""
    model_configs = load_model_config()

    # Pick active config based on LLM_BACKEND env var
    config_name_map = {
        "ollama": "ollama-local",
        "openai": "openai-gpt4o",
    }
    active_config = config_name_map.get(LLM_BACKEND, "ollama-local")

    agentscope.init(
        model_configs=model_configs,
        project=AGENT_NAME,
        save_dir=str(WORKSPACE / "agentscope_runs"),
        save_log=True,
    )

    # Load system prompt
    sys_prompt_path = CONFIG_DIR / "system_prompt.txt"
    sys_prompt = (
        sys_prompt_path.read_text() if sys_prompt_path.exists()
        else f"You are {AGENT_NAME}, a helpful AI agent."
    )

    return DialogAgent(
        name=AGENT_NAME,
        sys_prompt=sys_prompt,
        model_config_name=active_config,
    )


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent
    log.info("Starting %s agent server (backend: %s)", AGENT_NAME, LLM_BACKEND)
    try:
        agent = init_agent()
        log.info("%s agent ready", AGENT_NAME)
    except Exception as exc:
        log.error("Agent init failed: %s", exc)
        # Don't crash the server — allow health checks to still pass
        # Agent endpoints will return 503 if agent is None
    yield
    log.info("%s agent server shutting down", AGENT_NAME)


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title=f"{AGENT_NAME} API",
    description="AgentScope autonomous agent REST API",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Auth middleware ───────────────────────────────────────────────────────────
def verify_api_key(x_api_key: str = Header(default="")) -> None:
    if not AGENT_API_KEY:
        return  # No key configured — allow all (dev mode)
    if x_api_key != AGENT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


# ── Models ────────────────────────────────────────────────────────────────────
class TaskRequest(BaseModel):
    prompt: str
    context: dict[str, Any] = {}
    timeout: int = 120


class ChatRequest(BaseModel):
    message: str
    conversation_id: str = ""


# ── Probe endpoints ───────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "agent": AGENT_NAME, "ts": int(time.time())}


@app.get("/ready")
async def ready():
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    return {"status": "ready", "backend": LLM_BACKEND}


@app.get("/metrics")
async def metrics():
    """Prometheus-compatible plain text metrics."""
    total = len(tasks)
    done = sum(1 for t in tasks.values() if t["status"] == "done")
    failed = sum(1 for t in tasks.values() if t["status"] == "error")
    pending = total - done - failed
    return JSONResponse(
        content={},
        media_type="text/plain",
        headers={},
    )


# ── Agent endpoints ───────────────────────────────────────────────────────────
@app.post("/api/task")
async def submit_task(req: TaskRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    task_id = str(uuid.uuid4())[:8]
    tasks[task_id] = {
        "id": task_id,
        "status": "pending",
        "prompt": req.prompt,
        "result": None,
        "error": None,
        "created_at": time.time(),
    }

    async def run_task():
        tasks[task_id]["status"] = "running"
        try:
            msg = Msg(name="user", content=req.prompt, role="user")
            response = await asyncio.wait_for(
                asyncio.to_thread(agent, msg),
                timeout=req.timeout,
            )
            tasks[task_id]["status"] = "done"
            tasks[task_id]["result"] = response.content
            log.info("Task %s completed", task_id)
        except asyncio.TimeoutError:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"] = f"Timeout after {req.timeout}s"
            log.warning("Task %s timed out", task_id)
        except Exception as exc:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"] = str(exc)
            log.error("Task %s failed: %s", task_id, exc)

    asyncio.create_task(run_task())
    return {"task_id": task_id, "status": "pending"}


@app.get("/api/task/{task_id}")
async def get_task(task_id: str, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return tasks[task_id]


@app.get("/api/tasks")
async def list_tasks(x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    return list(tasks.values())


@app.post("/api/chat")
async def chat(req: ChatRequest, x_api_key: str = Header(default="")):
    """Synchronous single-turn chat — blocks until response."""
    verify_api_key(x_api_key)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    try:
        msg = Msg(name="user", content=req.message, role="user")
        response = await asyncio.to_thread(agent, msg)
        return {
            "message": response.content,
            "agent": AGENT_NAME,
            "conversation_id": req.conversation_id or str(uuid.uuid4())[:8],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/agents")
async def list_agents():
    """List all agents in the cluster (for multi-agent coordination UI)."""
    return {
        "agents": [
            {"name": "OpenClaw",  "ns": "openclaw",  "role": "general",    "status": "active"},
            {"name": "NemoClaw",  "ns": "nemoclaw",  "role": "reasoning",  "status": "unknown"},
            {"name": "HiClaw",    "ns": "hiclaw",    "role": "planning",   "status": "unknown"},
            {"name": "QwenPaw",   "ns": "qwenpaw",   "role": "language",   "status": "unknown"},
            {"name": "ReMe",      "ns": "reme",      "role": "memory",     "status": "unknown"},
        ]
    }

