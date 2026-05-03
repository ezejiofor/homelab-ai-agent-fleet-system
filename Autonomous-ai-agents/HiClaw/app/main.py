"""
HiClaw Agent Server — Task Decomposition + Planning
Unique endpoints:
  POST /api/plan   -> structured JSON plan with subtasks + critical path
  POST /api/delegate -> execute one subtask by forwarding to the right fleet agent
  POST /api/chat   -> free-form planning conversation
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
import httpx
from agentscope.agents import DialogAgent
from agentscope.message import Msg
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel
from starlette.responses import Response

# ── Config ────────────────────────────────────────────────────────────────────
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama.ollama.svc.cluster.local:11434/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:70b")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "/workspace"))
MAX_SUBTASKS = int(os.getenv("MAX_SUBTASKS", "20"))

FLEET = {
    "openclaw": os.getenv("OPENCLAW_API_URL", "http://openclaw.openclaw.svc.cluster.local:8000"),
    "nemoclaw": os.getenv("NEMOCLAW_API_URL", "http://nemoclaw.nemoclaw.svc.cluster.local:8000"),
    "qwenpaw":  os.getenv("QWENPAW_API_URL",  "http://qwenpaw.qwenpaw.svc.cluster.local:8000"),
    "reme":     os.getenv("REME_API_URL",      "http://reme.reme.svc.cluster.local:8000"),
}
FLEET_KEYS = {
    "openclaw": os.getenv("OPENCLAW_API_KEY", ""),
    "nemoclaw": os.getenv("NEMOCLAW_API_KEY", ""),
    "qwenpaw":  os.getenv("QWENPAW_API_KEY", ""),
    "reme":     os.getenv("REME_API_KEY", ""),
}

SYSTEM_PROMPT_PATH = Path("/app/config/system_prompt.txt")

# ── Metrics ───────────────────────────────────────────────────────────────────
PLANS_TOTAL = Counter("hiclaw_plans_total", "Plans generated", ["status"])
PLAN_LATENCY = Histogram("hiclaw_plan_latency_seconds", "Plan generation latency")
DELEGATE_TOTAL = Counter("hiclaw_delegate_total", "Delegations to fleet agents", ["agent", "status"])

tasks: dict[str, dict[str, Any]] = {}
agent: DialogAgent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent
    model_config_path = Path("/app/config/model_config.json")
    model_configs = json.loads(model_config_path.read_text()) if model_config_path.exists() else [{
        "config_name": "ollama-planning",
        "model_type": "openai_chat",
        "model_name": OLLAMA_MODEL,
        "api_key": "ollama",
        "client_args": {"base_url": OLLAMA_BASE_URL},
        "generate_args": {"temperature": 0.3, "max_tokens": 8192},
    }]
    agentscope.init(
        model_configs=model_configs,
        save_dir=str(WORKSPACE_DIR / "agentscope_runs"),
        project="hiclaw",
    )
    sys_prompt = SYSTEM_PROMPT_PATH.read_text() if SYSTEM_PROMPT_PATH.exists() else "You are HiClaw, a task planning agent."
    agent = DialogAgent(name="HiClaw", sys_prompt=sys_prompt, model_config_name="ollama-planning")
    yield


app = FastAPI(title="HiClaw Planning Agent API", version="0.1.0", lifespan=lifespan)


def verify_api_key(key: str | None) -> None:
    if AGENT_API_KEY and key != AGENT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def save_plan(plan_id: str, plan: dict) -> None:
    plan_dir = WORKSPACE_DIR / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / f"{plan_id}.json").write_text(json.dumps(plan, indent=2))


class PlanRequest(BaseModel):
    goal: str
    context: str | None = None
    max_subtasks: int | None = None

class DelegateRequest(BaseModel):
    agent: str
    task: str
    session_id: str | None = None

class ChatRequest(BaseModel):
    message: str


@app.get("/health")
def health():
    return {"status": "ok", "agent": "hiclaw", "role": "task-decomposition-planner"}

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
    return {"agents": [{"name": "HiClaw", "role": "planner", "fleet": list(FLEET.keys())}]}


@app.post("/api/plan")
async def create_plan(req: PlanRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    start = time.time()
    max_sub = req.max_subtasks or MAX_SUBTASKS
    prompt = (
        f"Goal: {req.goal}
"
        + (f"Context: {req.context}
" if req.context else "")
        + f"Max subtasks: {max_sub}

"
        "Produce a structured JSON plan. Output ONLY valid JSON matching the schema, then a newline, "
        "then a plain English summary."
    )
    msg = Msg(name="user", content=prompt, role="user")
    response = await asyncio.to_thread(agent, msg)
    raw = response.content

    # Try to extract JSON block
    plan_json: dict = {}
    try:
        start_idx = raw.index("{")
        end_idx = raw.rindex("}") + 1
        plan_json = json.loads(raw[start_idx:end_idx])
    except (ValueError, json.JSONDecodeError):
        plan_json = {"goal": req.goal, "raw_response": raw, "parse_error": True}

    plan_id = str(uuid.uuid4())[:8]
    plan_json["plan_id"] = plan_id
    plan_json["created_at"] = time.time()
    save_plan(plan_id, plan_json)

    PLAN_LATENCY.observe(time.time() - start)
    PLANS_TOTAL.labels(status="ok" if not plan_json.get("parse_error") else "parse_error").inc()
    return {"plan_id": plan_id, "plan": plan_json}


@app.get("/api/plan/{plan_id}")
def get_plan(plan_id: str, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    plan_file = WORKSPACE_DIR / "plans" / f"{plan_id}.json"
    if not plan_file.exists():
        raise HTTPException(status_code=404, detail="Plan not found")
    return json.loads(plan_file.read_text())


@app.post("/api/delegate")
async def delegate(req: DelegateRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    if req.agent not in FLEET:
        raise HTTPException(status_code=400, detail=f"Unknown agent: {req.agent}. Valid: {list(FLEET.keys())}")
    url = f"{FLEET[req.agent]}/api/chat"
    headers = {"Content-Type": "application/json"}
    key = FLEET_KEYS.get(req.agent, "")
    if key:
        headers["X-API-Key"] = key
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json={"message": req.task}, headers=headers)
            r.raise_for_status()
            DELEGATE_TOTAL.labels(agent=req.agent, status="ok").inc()
            return {"agent": req.agent, "result": r.json()}
    except Exception as e:
        DELEGATE_TOTAL.labels(agent=req.agent, status="error").inc()
        raise HTTPException(status_code=502, detail=f"Delegation to {req.agent} failed: {e}")


@app.post("/api/chat")
async def chat(req: ChatRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    msg = Msg(name="user", content=req.message, role="user")
    response = await asyncio.to_thread(agent, msg)
    return {"response": response.content, "agent": "HiClaw"}
