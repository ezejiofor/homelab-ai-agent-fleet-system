"""
NemoClaw Agent Server
FastAPI wrapper around AgentScope DialogAgent with NVIDIA OpenShell security.
The OpenShell gateway sidecar handles sandbox isolation — this server
communicates with it via unix socket at /run/openscell/channel.sock.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import agentscope
from agentscope.agents import DialogAgent
from agentscope.message import Msg
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel
from starlette.responses import Response

# ── Config ────────────────────────────────────────────────────────────────────
LLM_BACKEND = os.getenv("LLM_BACKEND", "nvidia_nim")
NVIDIA_NIM_BASE_URL = os.getenv("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_NIM_MODEL = os.getenv("NVIDIA_NIM_MODEL", "nvidia/nemotron-3-super-120b-a12b")
NVIDIA_NIM_API_KEY = os.getenv("NVIDIA_NIM_API_KEY", "")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama.ollama.svc.cluster.local:11434/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:70b")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "/workspace"))
AUDIT_LOG_PATH = Path(os.getenv("AUDIT_LOG_PATH", "/workspace/logs/audit.jsonl"))
BLOCKED_PATTERNS = os.getenv("BLOCKED_TASK_PATTERNS", "").split(",")

SYSTEM_PROMPT_PATH = Path("/app/config/system_prompt.txt")

# ── Metrics ───────────────────────────────────────────────────────────────────
REQUEST_COUNT = Counter("nemoclaw_requests_total", "Total API requests", ["endpoint", "status"])
REQUEST_LATENCY = Histogram("nemoclaw_request_latency_seconds", "API latency", ["endpoint"])
TASK_COUNT = Counter("nemoclaw_tasks_total", "Total async tasks", ["status"])

# ── In-memory task store ──────────────────────────────────────────────────────
tasks: dict[str, dict[str, Any]] = {}

# ── Agent ─────────────────────────────────────────────────────────────────────
agent: DialogAgent | None = None


def build_model_config() -> list[dict]:
    if LLM_BACKEND == "nvidia_nim":
        return [{
            "config_name": "nvidia_nim",
            "model_type": "openai_chat",
            "model_name": NVIDIA_NIM_MODEL,
            "api_key": NVIDIA_NIM_API_KEY,
            "client_args": {"base_url": NVIDIA_NIM_BASE_URL},
            "generate_args": {"temperature": 0.7, "max_tokens": 4096},
        }]
    return [{
        "config_name": "ollama",
        "model_type": "openai_chat",
        "model_name": OLLAMA_MODEL,
        "api_key": "ollama",
        "client_args": {"base_url": OLLAMA_BASE_URL},
        "generate_args": {"temperature": 0.7, "max_tokens": 4096},
    }]


def load_system_prompt() -> str:
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text()
    return "You are NemoClaw, a hardened NVIDIA-secured autonomous AI agent."


def is_blocked(text: str) -> bool:
    for pattern in BLOCKED_PATTERNS:
        p = pattern.strip()
        if p and p.lower() in text.lower():
            return True
    return False


def write_audit(event: str, data: dict) -> None:
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "event": event, **data}
        with open(AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent
    agentscope.init(
        model_configs=build_model_config(),
        save_dir=str(WORKSPACE_DIR / "agentscope_runs"),
        project="nemoclaw",
    )
    agent = DialogAgent(
        name="NemoClaw",
        sys_prompt=load_system_prompt(),
        model_config_name=LLM_BACKEND if LLM_BACKEND == "nvidia_nim" else "ollama",
    )
    write_audit("agent_started", {"backend": LLM_BACKEND, "model": NVIDIA_NIM_MODEL if LLM_BACKEND == "nvidia_nim" else OLLAMA_MODEL})
    yield
    write_audit("agent_stopped", {})


app = FastAPI(title="NemoClaw Agent API", version="0.1.0", lifespan=lifespan)


# ── Auth helper ───────────────────────────────────────────────────────────────
def verify_api_key(x_api_key: str | None) -> None:
    if AGENT_API_KEY and x_api_key != AGENT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Models ────────────────────────────────────────────────────────────────────
class TaskRequest(BaseModel):
    prompt: str
    session_id: str | None = None

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "agent": "nemoclaw", "backend": LLM_BACKEND}


@app.get("/ready")
def ready():
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/agents")
def list_agents():
    return {
        "agents": [
            {"name": "NemoClaw", "role": "hardened-security-agent", "backend": LLM_BACKEND,
             "model": NVIDIA_NIM_MODEL if LLM_BACKEND == "nvidia_nim" else OLLAMA_MODEL,
             "sandbox": "openscell"},
        ]
    }


async def _run_task(task_id: str, prompt: str) -> None:
    tasks[task_id]["status"] = "running"
    try:
        write_audit("task_start", {"task_id": task_id, "prompt": prompt[:200]})
        msg = Msg(name="user", content=prompt, role="user")
        response = await asyncio.to_thread(agent, msg)
        tasks[task_id].update({"status": "done", "result": response.content})
        write_audit("task_done", {"task_id": task_id})
        TASK_COUNT.labels(status="done").inc()
    except Exception as e:
        tasks[task_id].update({"status": "error", "error": str(e)})
        write_audit("task_error", {"task_id": task_id, "error": str(e)})
        TASK_COUNT.labels(status="error").inc()


@app.post("/api/task")
async def submit_task(req: TaskRequest, background_tasks: BackgroundTasks,
                      x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    if is_blocked(req.prompt):
        raise HTTPException(status_code=400, detail="Task blocked by security policy")
    task_id = str(uuid.uuid4())[:8]
    tasks[task_id] = {"status": "pending", "prompt": req.prompt, "created_at": time.time()}
    background_tasks.add_task(_run_task, task_id, req.prompt)
    TASK_COUNT.labels(status="pending").inc()
    return {"task_id": task_id, "status": "pending"}


@app.get("/api/task/{task_id}")
def get_task(task_id: str, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return tasks[task_id]


@app.post("/api/chat")
async def chat(req: ChatRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    if is_blocked(req.message):
        raise HTTPException(status_code=400, detail="Message blocked by security policy")
    start = time.time()
    REQUEST_COUNT.labels(endpoint="/api/chat", status="ok").inc()
    write_audit("chat", {"message": req.message[:200]})
    msg = Msg(name="user", content=req.message, role="user")
    response = await asyncio.to_thread(agent, msg)
    REQUEST_LATENCY.labels(endpoint="/api/chat").observe(time.time() - start)
    return {"response": response.content, "agent": "NemoClaw", "backend": LLM_BACKEND}
