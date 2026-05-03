"""
SkillsHub — Autonomous Agent Skill Learning and Registry

Endpoints (agents use AGENT_API_KEY, admins use ADMIN_API_KEY):

  POST /api/skills/learn           -> submit a workflow; get a structured, validated skill
  GET  /api/skills/search          -> semantic search for skills by description
  GET  /api/skills/suggest         -> auto-suggest skills for current task context
  GET  /api/skills/{id}            -> retrieve a specific skill
  GET  /api/skills                 -> list all skills (with filters)
  PUT  /api/skills/{id}/approve    -> [ADMIN] approve a draft skill
  PUT  /api/skills/{id}/deprecate  -> [ADMIN] deprecate a skill
  POST /api/skills/outcome         -> record execution outcome (improves stats)
  DELETE /api/skills/{id}          -> [ADMIN] delete a skill permanently
  GET  /api/skills/{id}/versions   -> list all versions of a skill
  POST /api/skills/export          -> export all skills as JSONL
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter,
    FieldCondition, MatchValue, HasIdCondition,
)
from starlette.responses import Response

from skill_schema import (
    Skill, SkillLearnRequest, SkillOutcomeRequest, SkillApproveRequest,
    TrustLevel, SkillCategory,
)
from skill_validator import validate_skill

# ── Config ────────────────────────────────────────────────────────────────────
QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant.reme.svc.cluster.local")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
QDRANT_COLLECTION = os.getenv("QDRANT_SKILLS_COLLECTION", "agent_skills")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "http://ollama.ollama.svc.cluster.local:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama.ollama.svc.cluster.local:11434/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:70b")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
SKILLS_DIR = Path(os.getenv("SKILLS_DIR", "/skills"))
AUDIT_LOG = Path(os.getenv("AUDIT_LOG", "/skills/audit/audit.jsonl"))
AUTO_APPROVE = os.getenv("AUTO_APPROVE_ENABLED", "false").lower() == "true"

STRUCTURING_PROMPT_PATH = Path("/app/config/skill_structuring_prompt.txt")

# ── Metrics ───────────────────────────────────────────────────────────────────
SKILLS_LEARNED = Counter("skillshub_skills_learned_total", "Skills submitted for learning", ["status"])
SKILLS_APPROVED = Counter("skillshub_skills_approved_total", "Skills approved by humans")
SKILLS_SEARCHED = Counter("skillshub_searches_total", "Skill searches")
OUTCOMES_RECORDED = Counter("skillshub_outcomes_total", "Execution outcomes recorded", ["success"])
SKILL_COUNT = Gauge("skillshub_skill_count", "Total skills", ["trust_level"])
LEARN_LATENCY = Histogram("skillshub_learn_latency_seconds", "Skill learning latency")

# ── Globals ───────────────────────────────────────────────────────────────────
qdrant: QdrantClient | None = None


def ensure_dirs():
    for d in ["catalog", "audit", "versions", "drafts", "approved"]:
        (SKILLS_DIR / d).mkdir(parents=True, exist_ok=True)


def ensure_collection():
    existing = [c.name for c in qdrant.get_collections().collections]
    if QDRANT_COLLECTION not in existing:
        qdrant.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
        print(f"[skillshub] Created Qdrant collection: {QDRANT_COLLECTION}")


def audit(event: str, data: dict):
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "event": event, **data}
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "
")
    except Exception:
        pass


def save_skill(skill: Skill):
    """Save skill JSON to PVC — both in catalog/ and drafts/ or approved/."""
    skill_dict = skill.to_storage_dict()
    catalog_path = SKILLS_DIR / "catalog" / f"{skill.id}.json"
    version_path = SKILLS_DIR / "versions" / f"{skill.id}_v{skill.version}.json"
    trust_path = SKILLS_DIR / skill.trust_level.value / f"{skill.id}.json"

    catalog_path.write_text(json.dumps(skill_dict, indent=2))
    version_path.write_text(json.dumps(skill_dict, indent=2))
    trust_path.write_text(json.dumps(skill_dict, indent=2))


def load_skill(skill_id: str) -> Skill | None:
    path = SKILLS_DIR / "catalog" / f"{skill_id}.json"
    if not path.exists():
        return None
    return Skill(**json.loads(path.read_text()))


async def embed(text: str) -> list[float]:
    url = f"{EMBEDDING_BASE_URL}/api/embeddings"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json={"model": EMBEDDING_MODEL, "prompt": text})
        r.raise_for_status()
        return r.json()["embedding"]


async def llm_complete(prompt: str) -> str:
    url = f"{OLLAMA_BASE_URL}/chat/completions"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


async def structure_skill(req: SkillLearnRequest) -> dict:
    """Use LLM to convert workflow description to structured skill JSON."""
    base_prompt = STRUCTURING_PROMPT_PATH.read_text() if STRUCTURING_PROMPT_PATH.exists() else ""
    prompt = (
        base_prompt + "

"
        f"Submitted by agent: {req.submitted_by}
"
        + (f"Category hint: {req.category_hint}
" if req.category_hint else "")
        + (f"Tags hint: {', '.join(req.tags_hint)}
" if req.tags_hint else "")
        + (f"Context: {req.context}
" if req.context else "")
        + f"
Workflow description:
{req.workflow_description}

"
        "Output ONLY the JSON object:"
    )
    raw = await llm_complete(prompt)
    # Extract JSON block
    start = raw.index("{")
    end = raw.rindex("}") + 1
    return json.loads(raw[start:end])


@asynccontextmanager
async def lifespan(app: FastAPI):
    global qdrant
    ensure_dirs()
    kw: dict[str, Any] = {"host": QDRANT_HOST, "port": QDRANT_PORT}
    if QDRANT_API_KEY:
        kw["api_key"] = QDRANT_API_KEY
    qdrant = QdrantClient(**kw)
    ensure_collection()
    # Refresh gauges
    _refresh_skill_count()
    yield
    try:
        qdrant.close()
    except Exception:
        pass


def _refresh_skill_count():
    for level in ["draft", "reviewed", "approved", "deprecated"]:
        count = len(list((SKILLS_DIR / level).glob("*.json")))
        SKILL_COUNT.labels(trust_level=level).set(count)


app = FastAPI(title="SkillsHub — Agent Skill Registry", version="0.1.0", lifespan=lifespan)


# ── Auth ──────────────────────────────────────────────────────────────────────
def verify_agent(key: str | None):
    valid = {k for k in [AGENT_API_KEY, ADMIN_API_KEY] if k}
    if valid and key not in valid:
        raise HTTPException(status_code=401, detail="Invalid API key")

def verify_admin(key: str | None):
    if ADMIN_API_KEY and key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Admin API key required")


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "skillshub", "qdrant": f"{QDRANT_HOST}:{QDRANT_PORT}"}

@app.get("/ready")
def ready():
    if qdrant is None:
        raise HTTPException(status_code=503, detail="Qdrant not connected")
    return {"status": "ready"}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── LEARN ─────────────────────────────────────────────────────────────────────
@app.post("/api/skills/learn")
async def learn_skill(req: SkillLearnRequest, x_api_key: str | None = Header(default=None)):
    """
    Agent submits a workflow description → LLM structures it → SecurityValidator
    → stored as draft skill in Qdrant + JSON file → returns skill with trust_level=draft.
    Human must call /api/skills/{id}/approve before agents can auto-execute it.
    """
    verify_agent(x_api_key)
    start = time.time()

    # 1. Structure via LLM
    try:
        structured = await structure_skill(req)
    except Exception as e:
        SKILLS_LEARNED.labels(status="structure_failed").inc()
        audit("learn_failed", {"submitted_by": req.submitted_by, "error": str(e)})
        raise HTTPException(status_code=422, detail=f"Skill structuring failed: {e}")

    # 2. Build Skill object (Pydantic validates tool enum, field lengths, etc.)
    try:
        skill = Skill(
            **{**structured, "created_by": req.submitted_by, "trust_level": TrustLevel.draft}
        )
    except Exception as e:
        SKILLS_LEARNED.labels(status="schema_invalid").inc()
        raise HTTPException(status_code=422, detail=f"Structured skill failed schema validation: {e}")

    # 3. Security validation
    validation = validate_skill(skill)
    skill.security_score = round(validation.score, 3)
    skill.security_flags = validation.flags

    if not validation.passed:
        SKILLS_LEARNED.labels(status="security_rejected").inc()
        audit("learn_rejected", {
            "submitted_by": req.submitted_by,
            "skill_name": skill.name,
            "flags": validation.flags,
            "score": skill.security_score,
        })
        raise HTTPException(status_code=400, detail={
            "error": "Skill rejected by security validator",
            "security_score": skill.security_score,
            "flags": validation.flags,
        })

    # 4. Embed description for semantic search
    embedding = await embed(f"{skill.name}. {skill.description}. Tags: {', '.join(skill.tags)}")

    # 5. Store in Qdrant
    qdrant.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[PointStruct(
            id=skill.id,
            vector=embedding,
            payload={
                "name": skill.name,
                "description": skill.description,
                "category": skill.category,
                "tags": skill.tags,
                "trust_level": skill.trust_level,
                "created_by": skill.created_by,
                "created_at": skill.created_at,
                "security_score": skill.security_score,
                "step_count": len(skill.steps),
            },
        )],
    )

    # 6. Save JSON to PVC
    save_skill(skill)

    # 7. Audit
    elapsed = time.time() - start
    SKILLS_LEARNED.labels(status="ok").inc()
    LEARN_LATENCY.observe(elapsed)
    _refresh_skill_count()
    audit("skill_learned", {
        "skill_id": skill.id,
        "name": skill.name,
        "submitted_by": req.submitted_by,
        "trust_level": skill.trust_level,
        "security_score": skill.security_score,
        "latency_seconds": round(elapsed, 3),
    })

    return {
        "skill_id": skill.id,
        "name": skill.name,
        "trust_level": skill.trust_level,
        "security_score": skill.security_score,
        "security_flags": skill.security_flags,
        "step_count": len(skill.steps),
        "message": (
            "Skill stored as DRAFT. A human admin must approve it at "
            f"PUT /api/skills/{skill.id}/approve before agents can auto-execute it."
        ),
        "skill": skill.to_storage_dict(),
    }


# ── SEARCH ────────────────────────────────────────────────────────────────────
@app.get("/api/skills/search")
async def search_skills(
    q: str = Query(..., min_length=3),
    top_k: int = Query(default=5, ge=1, le=20),
    category: SkillCategory | None = None,
    trust_level: TrustLevel | None = None,
    min_score: float = Query(default=0.5, ge=0.0, le=1.0),
    x_api_key: str | None = Header(default=None),
):
    verify_agent(x_api_key)
    SKILLS_SEARCHED.inc()
    vector = await embed(q)

    qdrant_filter = None
    conditions = []
    if category:
        conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
    if trust_level:
        conditions.append(FieldCondition(key="trust_level", match=MatchValue(value=trust_level)))
    if conditions:
        from qdrant_client.models import Filter
        qdrant_filter = Filter(must=conditions)

    results = qdrant.search(
        collection_name=QDRANT_COLLECTION,
        query_vector=vector,
        limit=top_k,
        score_threshold=min_score,
        query_filter=qdrant_filter,
        with_payload=True,
    )

    return {
        "query": q,
        "results": [
            {
                "skill_id": str(r.id),
                "score": round(r.score, 4),
                "name": r.payload.get("name"),
                "description": r.payload.get("description", "")[:200],
                "category": r.payload.get("category"),
                "tags": r.payload.get("tags", []),
                "trust_level": r.payload.get("trust_level"),
                "security_score": r.payload.get("security_score"),
                "step_count": r.payload.get("step_count"),
                "created_by": r.payload.get("created_by"),
            }
            for r in results
        ],
        "count": len(results),
    }


# ── SUGGEST ───────────────────────────────────────────────────────────────────
@app.get("/api/skills/suggest")
async def suggest_skills(
    context: str = Query(..., description="Current task or situation description"),
    top_k: int = Query(default=3, ge=1, le=10),
    approved_only: bool = Query(default=False),
    x_api_key: str | None = Header(default=None),
):
    """Given current agent context, suggest the most relevant skills."""
    verify_agent(x_api_key)
    vector = await embed(context)

    qdrant_filter = None
    if approved_only:
        qdrant_filter = Filter(
            must=[FieldCondition(key="trust_level", match=MatchValue(value="approved"))]
        )

    results = qdrant.search(
        collection_name=QDRANT_COLLECTION,
        query_vector=vector,
        limit=top_k,
        score_threshold=0.55,
        query_filter=qdrant_filter,
        with_payload=True,
    )

    suggestions = []
    for r in results:
        skill = load_skill(str(r.id))
        if not skill:
            continue
        suggestions.append({
            "skill_id": str(r.id),
            "relevance_score": round(r.score, 4),
            "name": skill.name,
            "description": skill.description[:200],
            "trust_level": skill.trust_level,
            "requires_approval": skill.trust_level != TrustLevel.approved,
            "steps_preview": [{"id": s.id, "title": s.title} for s in skill.steps[:3]],
            "estimated_duration_minutes": skill.estimated_duration_minutes,
        })

    return {
        "context_summary": context[:100],
        "suggestions": suggestions,
        "note": "Skills with trust_level=draft require human approval before auto-execution.",
    }


# ── GET SKILL ─────────────────────────────────────────────────────────────────
@app.get("/api/skills/{skill_id}")
def get_skill(skill_id: str, x_api_key: str | None = Header(default=None)):
    verify_agent(x_api_key)
    skill = load_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill.to_storage_dict()


# ── LIST ──────────────────────────────────────────────────────────────────────
@app.get("/api/skills")
def list_skills(
    trust_level: TrustLevel | None = None,
    category: SkillCategory | None = None,
    limit: int = Query(default=50, le=500),
    x_api_key: str | None = Header(default=None),
):
    verify_agent(x_api_key)
    catalog = SKILLS_DIR / "catalog"
    skills = []
    for f in sorted(catalog.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            s = Skill(**json.loads(f.read_text()))
            if trust_level and s.trust_level != trust_level:
                continue
            if category and s.category != category:
                continue
            skills.append({
                "skill_id": s.id,
                "name": s.name,
                "category": s.category,
                "trust_level": s.trust_level,
                "security_score": s.security_score,
                "step_count": len(s.steps),
                "execution_count": s.execution_count,
                "success_rate": s.success_rate(),
                "created_by": s.created_by,
                "created_at": s.created_at,
            })
            if len(skills) >= limit:
                break
        except Exception:
            continue
    return {"skills": skills, "count": len(skills)}


# ── APPROVE [ADMIN] ───────────────────────────────────────────────────────────
@app.put("/api/skills/{skill_id}/approve")
async def approve_skill(
    skill_id: str,
    req: SkillApproveRequest,
    x_api_key: str | None = Header(default=None),
):
    """Human admin approves a draft skill. Only approved skills can be auto-executed."""
    verify_admin(x_api_key)
    skill = load_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.trust_level == TrustLevel.deprecated:
        raise HTTPException(status_code=400, detail="Cannot approve a deprecated skill")

    # Remove from old trust-level folder
    old_path = SKILLS_DIR / skill.trust_level.value / f"{skill_id}.json"
    if old_path.exists():
        old_path.unlink()

    skill.trust_level = TrustLevel.approved
    skill.approved_by = req.approved_by
    skill.approved_at = time.time()
    skill.updated_at = time.time()

    save_skill(skill)

    # Update Qdrant payload
    qdrant.set_payload(
        collection_name=QDRANT_COLLECTION,
        payload={"trust_level": "approved", "approved_by": req.approved_by},
        points=[skill_id],
    )

    SKILLS_APPROVED.inc()
    _refresh_skill_count()
    audit("skill_approved", {
        "skill_id": skill_id,
        "name": skill.name,
        "approved_by": req.approved_by,
        "notes": req.notes,
    })

    return {"approved": True, "skill_id": skill_id, "name": skill.name, "approved_by": req.approved_by}


# ── DEPRECATE [ADMIN] ─────────────────────────────────────────────────────────
@app.put("/api/skills/{skill_id}/deprecate")
def deprecate_skill(skill_id: str, x_api_key: str | None = Header(default=None)):
    verify_admin(x_api_key)
    skill = load_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    old_path = SKILLS_DIR / skill.trust_level.value / f"{skill_id}.json"
    if old_path.exists():
        old_path.unlink()

    skill.trust_level = TrustLevel.deprecated
    skill.updated_at = time.time()
    save_skill(skill)

    qdrant.set_payload(
        collection_name=QDRANT_COLLECTION,
        payload={"trust_level": "deprecated"},
        points=[skill_id],
    )

    audit("skill_deprecated", {"skill_id": skill_id, "name": skill.name})
    return {"deprecated": True, "skill_id": skill_id}


# ── OUTCOME ───────────────────────────────────────────────────────────────────
@app.post("/api/skills/outcome")
def record_outcome(req: SkillOutcomeRequest, x_api_key: str | None = Header(default=None)):
    """Agent reports back execution result → updates skill stats for continuous improvement."""
    verify_agent(x_api_key)
    skill = load_skill(req.skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    skill.execution_count += 1
    if req.success:
        skill.success_count += 1
    else:
        skill.failure_count += 1
    skill.last_executed_at = time.time()
    skill.last_executed_by = req.executed_by
    skill.updated_at = time.time()

    save_skill(skill)
    OUTCOMES_RECORDED.labels(success=str(req.success)).inc()
    audit("skill_outcome", {
        "skill_id": req.skill_id,
        "executed_by": req.executed_by,
        "success": req.success,
        "duration_seconds": req.duration_seconds,
        "failed_at_step": req.failed_at_step,
        "notes": req.notes,
    })

    return {
        "recorded": True,
        "skill_id": req.skill_id,
        "success_rate": skill.success_rate(),
        "execution_count": skill.execution_count,
    }


# ── VERSIONS ──────────────────────────────────────────────────────────────────
@app.get("/api/skills/{skill_id}/versions")
def list_versions(skill_id: str, x_api_key: str | None = Header(default=None)):
    verify_agent(x_api_key)
    versions_dir = SKILLS_DIR / "versions"
    versions = []
    for f in sorted(versions_dir.glob(f"{skill_id}_v*.json")):
        try:
            s = Skill(**json.loads(f.read_text()))
            versions.append({"version": s.version, "trust_level": s.trust_level,
                             "created_at": s.created_at, "file": f.name})
        except Exception:
            continue
    return {"skill_id": skill_id, "versions": versions}


# ── EXPORT ────────────────────────────────────────────────────────────────────
@app.post("/api/skills/export")
def export_skills(x_api_key: str | None = Header(default=None)):
    verify_admin(x_api_key)
    import datetime
    export_path = SKILLS_DIR / f"skills_export_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.jsonl"
    count = 0
    with open(export_path, "w") as fout:
        for f in (SKILLS_DIR / "catalog").glob("*.json"):
            fout.write(f.read_text().replace("
", " ") + "
")
            count += 1
    return {"exported": True, "count": count, "file": str(export_path)}


# ── STATS ─────────────────────────────────────────────────────────────────────
@app.get("/api/skills/stats/summary")
def stats_summary(x_api_key: str | None = Header(default=None)):
    verify_agent(x_api_key)
    summary = {}
    for level in ["draft", "reviewed", "approved", "deprecated"]:
        summary[level] = len(list((SKILLS_DIR / level).glob("*.json")))
    return {
        "total": sum(summary.values()),
        "by_trust_level": summary,
        "collection": QDRANT_COLLECTION,
        "qdrant_host": QDRANT_HOST,
    }
