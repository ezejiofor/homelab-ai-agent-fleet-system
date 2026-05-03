"""
ReMe Agent Server — Long-term Memory + RAG
Uses Qdrant as vector store with nomic-embed-text embeddings (Ollama).
Generation uses llama3.1:70b (Ollama) with retrieved context injected.

Endpoints:
  POST /api/remember     -> store a memory (text + metadata + tags)
  POST /api/recall       -> RAG: embed query, search Qdrant, generate answer
  POST /api/search       -> raw vector search (returns docs without generation)
  DELETE /api/forget/{id} -> delete a specific memory by ID
  POST /api/forget/bulk  -> delete memories by filter (tag, source, before date)
  GET  /api/stats        -> Qdrant collection stats
  POST /api/export       -> export all memories as JSONL to /workspace/exports/
  POST /api/chat         -> chat with memory context injected automatically
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import agentscope
import httpx
from agentscope.agents import DialogAgent
from agentscope.message import Msg
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter, FieldCondition,
    MatchValue, Range, SearchParams, HasIdCondition,
)
from starlette.responses import Response

# ── Config ────────────────────────────────────────────────────────────────────
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama.ollama.svc.cluster.local:11434/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:70b")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "http://ollama.ollama.svc.cluster.local:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))
QDRANT_HOST = os.getenv("QDRANT_HOST", "qdrant.reme.svc.cluster.local")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "reme_memories")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "/workspace"))
RECALL_TOP_K = int(os.getenv("RECALL_TOP_K", "5"))
RECALL_MIN_SCORE = float(os.getenv("RECALL_MIN_SCORE", "0.65"))
MAX_MEMORY_CHARS = int(os.getenv("MAX_MEMORY_CHARS", "8000"))
DEFAULT_TTL_DAYS = int(os.getenv("MEMORY_DEFAULT_TTL_DAYS", "365"))

SYSTEM_PROMPT_PATH = Path("/app/config/system_prompt.txt")

# ── Metrics ───────────────────────────────────────────────────────────────────
REMEMBER_TOTAL = Counter("reme_memories_stored_total", "Memories stored")
RECALL_TOTAL = Counter("reme_recalls_total", "Recall queries", ["status"])
RECALL_LATENCY = Histogram("reme_recall_latency_seconds", "Recall latency")
SEARCH_TOTAL = Counter("reme_searches_total", "Raw vector searches")
FORGET_TOTAL = Counter("reme_forgets_total", "Memories deleted")
MEMORY_COUNT = Gauge("reme_memory_count", "Total memories in Qdrant")

# ── Globals ───────────────────────────────────────────────────────────────────
qdrant: QdrantClient | None = None
agent: DialogAgent | None = None


# ── Embedding helper (calls Ollama /api/embeddings) ───────────────────────────
async def embed(text: str) -> list[float]:
    url = f"{EMBEDDING_BASE_URL}/api/embeddings"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json={"model": EMBEDDING_MODEL, "prompt": text})
        r.raise_for_status()
        data = r.json()
        return data["embedding"]


def ensure_collection():
    """Create Qdrant collection if it does not exist."""
    existing = [c.name for c in qdrant.get_collections().collections]
    if QDRANT_COLLECTION not in existing:
        qdrant.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
        print(f"[reme] Created Qdrant collection: {QDRANT_COLLECTION}")
    else:
        print(f"[reme] Using existing Qdrant collection: {QDRANT_COLLECTION}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global qdrant, agent

    # Connect to Qdrant
    qdrant_kwargs: dict[str, Any] = {"host": QDRANT_HOST, "port": QDRANT_PORT}
    if QDRANT_API_KEY:
        qdrant_kwargs["api_key"] = QDRANT_API_KEY
    qdrant = QdrantClient(**qdrant_kwargs)
    ensure_collection()

    # Init AgentScope + DialogAgent
    model_config_path = Path("/app/config/model_config.json")
    model_configs = json.loads(model_config_path.read_text()) if model_config_path.exists() else [{
        "config_name": "ollama-rag",
        "model_type": "openai_chat",
        "model_name": OLLAMA_MODEL,
        "api_key": "ollama",
        "client_args": {"base_url": OLLAMA_BASE_URL},
        "generate_args": {"temperature": 0.1, "max_tokens": 4096},
    }]
    agentscope.init(
        model_configs=model_configs,
        save_dir=str(WORKSPACE_DIR / "agentscope_runs"),
        project="reme",
    )
    sys_prompt = SYSTEM_PROMPT_PATH.read_text() if SYSTEM_PROMPT_PATH.exists() else "You are ReMe, a memory and RAG agent."
    agent = DialogAgent(name="ReMe", sys_prompt=sys_prompt, model_config_name="ollama-rag")

    # Update memory count gauge
    try:
        info = qdrant.get_collection(QDRANT_COLLECTION)
        MEMORY_COUNT.set(info.points_count or 0)
    except Exception:
        pass

    yield
    # Cleanup — close Qdrant connection
    try:
        qdrant.close()
    except Exception:
        pass


app = FastAPI(title="ReMe Memory + RAG Agent API", version="0.1.0", lifespan=lifespan)


def verify_api_key(key: str | None) -> None:
    if AGENT_API_KEY and key != AGENT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Request models ────────────────────────────────────────────────────────────
class MemoryRequest(BaseModel):
    text: str = Field(..., description="The memory content to store")
    source: str = Field(default="manual", description="Origin: agent name, URL, filename, etc.")
    tags: list[str] = Field(default_factory=list, description="Taxonomy tags: fact, decision, code, log, etc.")
    session_id: str | None = None
    ttl_days: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class RecallRequest(BaseModel):
    query: str
    top_k: int | None = None
    min_score: float | None = None
    filter_tags: list[str] | None = None
    filter_source: str | None = None
    generate_answer: bool = True

class SearchRequest(BaseModel):
    query: str
    top_k: int = 10
    min_score: float = 0.5
    filter_tags: list[str] | None = None

class BulkForgetRequest(BaseModel):
    filter_tags: list[str] | None = None
    filter_source: str | None = None
    before_timestamp: float | None = None
    dry_run: bool = True

class ChatRequest(BaseModel):
    message: str
    inject_memories: bool = True
    top_k: int = 3


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status": "ok",
        "agent": "reme",
        "qdrant": f"{QDRANT_HOST}:{QDRANT_PORT}",
        "collection": QDRANT_COLLECTION,
    }


@app.get("/ready")
def ready():
    if agent is None or qdrant is None:
        raise HTTPException(status_code=503, detail="Agent or Qdrant not ready")
    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/stats")
async def stats(x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    info = qdrant.get_collection(QDRANT_COLLECTION)
    MEMORY_COUNT.set(info.points_count or 0)
    return {
        "collection": QDRANT_COLLECTION,
        "total_memories": info.points_count,
        "vector_size": EMBEDDING_DIM,
        "embedding_model": EMBEDDING_MODEL,
        "llm_model": OLLAMA_MODEL,
        "qdrant_host": QDRANT_HOST,
    }


@app.get("/api/agents")
def list_agents():
    return {"agents": [{"name": "ReMe", "role": "memory-rag",
                        "collection": QDRANT_COLLECTION,
                        "embedding_model": EMBEDDING_MODEL}]}


@app.post("/api/remember")
async def remember(req: MemoryRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    if len(req.text) > MAX_MEMORY_CHARS:
        raise HTTPException(status_code=400, detail=f"Memory text exceeds {MAX_MEMORY_CHARS} chars")

    vector = await embed(req.text)
    memory_id = str(uuid.uuid4())
    now = time.time()
    expires_at = now + (req.ttl_days or DEFAULT_TTL_DAYS) * 86400

    payload = {
        "text": req.text,
        "source": req.source,
        "tags": req.tags,
        "session_id": req.session_id,
        "created_at": now,
        "expires_at": expires_at,
        "char_count": len(req.text),
        **req.metadata,
    }

    qdrant.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[PointStruct(id=memory_id, vector=vector, payload=payload)],
    )

    REMEMBER_TOTAL.inc()
    MEMORY_COUNT.inc()
    return {
        "memory_id": memory_id,
        "stored": True,
        "tags": req.tags,
        "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        "char_count": len(req.text),
    }


@app.post("/api/search")
async def search(req: SearchRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    SEARCH_TOTAL.inc()
    vector = await embed(req.query)

    qdrant_filter = None
    if req.filter_tags:
        qdrant_filter = Filter(
            must=[FieldCondition(key="tags", match=MatchValue(value=tag)) for tag in req.filter_tags]
        )

    results = qdrant.search(
        collection_name=QDRANT_COLLECTION,
        query_vector=vector,
        limit=req.top_k,
        score_threshold=req.min_score,
        query_filter=qdrant_filter,
        with_payload=True,
    )

    return {
        "query": req.query,
        "results": [
            {
                "id": str(r.id),
                "score": round(r.score, 4),
                "text": r.payload.get("text", ""),
                "tags": r.payload.get("tags", []),
                "source": r.payload.get("source", ""),
                "created_at": r.payload.get("created_at"),
            }
            for r in results
        ],
        "count": len(results),
    }


@app.post("/api/recall")
async def recall(req: RecallRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    start = time.time()
    top_k = req.top_k or RECALL_TOP_K
    min_score = req.min_score or RECALL_MIN_SCORE

    vector = await embed(req.query)

    qdrant_filter = None
    if req.filter_tags or req.filter_source:
        must_conditions = []
        if req.filter_tags:
            for tag in req.filter_tags:
                must_conditions.append(FieldCondition(key="tags", match=MatchValue(value=tag)))
        if req.filter_source:
            must_conditions.append(FieldCondition(key="source", match=MatchValue(value=req.filter_source)))
        qdrant_filter = Filter(must=must_conditions)

    results = qdrant.search(
        collection_name=QDRANT_COLLECTION,
        query_vector=vector,
        limit=top_k,
        score_threshold=min_score,
        query_filter=qdrant_filter,
        with_payload=True,
    )

    sources = [
        {
            "id": str(r.id),
            "score": round(r.score, 4),
            "preview": r.payload.get("text", "")[:120],
            "tags": r.payload.get("tags", []),
            "source": r.payload.get("source", ""),
        }
        for r in results
    ]

    if not req.generate_answer:
        RECALL_TOTAL.labels(status="search_only").inc()
        return {"query": req.query, "sources": sources, "generated_answer": None}

    if not results:
        RECALL_TOTAL.labels(status="no_memories").inc()
        return {
            "answer": "No relevant memories found for this query.",
            "confidence": "low",
            "sources": [],
            "memory_count_searched": 0,
        }

    # Build RAG context
    context_blocks = []
    for i, r in enumerate(results, 1):
        context_blocks.append(
            f"[Memory {i} | ID: {r.id} | Score: {r.score:.2f} | Tags: {r.payload.get('tags', [])}]
"
            f"{r.payload.get('text', '')}"
        )
    context = "

---

".join(context_blocks)

    rag_prompt = (
        f"Query: {req.query}

"
        f"Retrieved memories ({len(results)} results):

"
        f"{context}

"
        "Based ONLY on the retrieved memories above, answer the query. "
        "Cite the memory IDs you used. Rate your confidence (high/medium/low). "
        "Output a JSON object matching this schema exactly:
"
        '{"answer": "...", "confidence": "high|medium|low", '
        '"sources": [{"id": "...", "score": 0.0, "preview": "..."}], '
        '"memory_count_searched": 0}'
    )

    msg = Msg(name="user", content=rag_prompt, role="user")
    response = await asyncio.to_thread(agent, msg)
    raw = response.content.strip()

    try:
        start_i = raw.index("{")
        end_i = raw.rindex("}") + 1
        result = json.loads(raw[start_i:end_i])
    except (ValueError, json.JSONDecodeError):
        result = {"answer": raw, "confidence": "unknown", "sources": sources, "parse_error": True}

    result["memory_count_searched"] = len(results)
    elapsed = time.time() - start
    RECALL_TOTAL.labels(status="ok").inc()
    RECALL_LATENCY.observe(elapsed)
    return result


@app.delete("/api/forget/{memory_id}")
async def forget(memory_id: str, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    try:
        qdrant.delete(
            collection_name=QDRANT_COLLECTION,
            points_selector=HasIdCondition(has_id=[memory_id]),
        )
        FORGET_TOTAL.inc()
        MEMORY_COUNT.dec()
        return {"deleted": True, "memory_id": memory_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")


@app.post("/api/forget/bulk")
async def forget_bulk(req: BulkForgetRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    if not any([req.filter_tags, req.filter_source, req.before_timestamp]):
        raise HTTPException(status_code=400, detail="Provide at least one filter (filter_tags, filter_source, or before_timestamp)")

    # First search to show what would be deleted
    search_filter_conditions = []
    if req.filter_tags:
        for tag in req.filter_tags:
            search_filter_conditions.append(FieldCondition(key="tags", match=MatchValue(value=tag)))
    if req.filter_source:
        search_filter_conditions.append(FieldCondition(key="source", match=MatchValue(value=req.filter_source)))
    if req.before_timestamp:
        search_filter_conditions.append(FieldCondition(key="created_at", range=Range(lt=req.before_timestamp)))

    scroll_filter = Filter(must=search_filter_conditions) if search_filter_conditions else None
    records, _ = qdrant.scroll(
        collection_name=QDRANT_COLLECTION,
        scroll_filter=scroll_filter,
        limit=1000,
        with_payload=True,
    )
    ids = [str(r.id) for r in records]

    if req.dry_run:
        return {"dry_run": True, "would_delete_count": len(ids), "sample_ids": ids[:10]}

    if ids:
        qdrant.delete(
            collection_name=QDRANT_COLLECTION,
            points_selector=HasIdCondition(has_id=ids),
        )
        FORGET_TOTAL.inc(len(ids))
        MEMORY_COUNT.dec(len(ids))

    return {"deleted": True, "count": len(ids), "ids": ids}


@app.post("/api/export")
async def export_memories(x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)
    export_dir = WORKSPACE_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    filename = f"reme_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.jsonl"
    export_path = export_dir / filename

    records, next_offset = qdrant.scroll(
        collection_name=QDRANT_COLLECTION,
        limit=1000,
        with_payload=True,
        with_vectors=False,
    )
    all_records = list(records)
    while next_offset:
        records, next_offset = qdrant.scroll(
            collection_name=QDRANT_COLLECTION,
            limit=1000,
            offset=next_offset,
            with_payload=True,
            with_vectors=False,
        )
        all_records.extend(records)

    with open(export_path, "w") as f:
        for r in all_records:
            f.write(json.dumps({"id": str(r.id), **r.payload}) + "
")

    return {"exported": True, "count": len(all_records), "file": str(export_path)}


@app.post("/api/chat")
async def chat(req: ChatRequest, x_api_key: str | None = Header(default=None)):
    verify_api_key(x_api_key)

    content = req.message
    if req.inject_memories:
        try:
            vector = await embed(req.message)
            results = qdrant.search(
                collection_name=QDRANT_COLLECTION,
                query_vector=vector,
                limit=req.top_k,
                score_threshold=RECALL_MIN_SCORE,
                with_payload=True,
            )
            if results:
                context = "
".join(
                    f"[Memory score={r.score:.2f}] {r.payload.get('text','')[:300]}"
                    for r in results
                )
                content = f"Relevant memories:
{context}

---

User message: {req.message}"
        except Exception:
            pass  # Degraded mode — answer without memory context

    msg = Msg(name="user", content=content, role="user")
    response = await asyncio.to_thread(agent, msg)
    return {"response": response.content, "agent": "ReMe", "memories_injected": req.inject_memories}
