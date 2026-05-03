"""
SkillsClient — drop-in library for all fleet agents.

Usage in any agent (e.g. hiclaw/app/main.py):
    from skills_client import SkillsClient
    skills = SkillsClient()

    # Learn a new skill from a workflow description
    result = await skills.learn(
        workflow="1. Check ceph-block exists 2. helm install vault ...",
        submitted_by="hiclaw",
    )

    # Search for relevant skills before acting
    matches = await skills.search("deploy vault helm chart")

    # Get suggestions for current context
    hints = await skills.suggest("I need to set up monitoring for a new namespace")

    # Record outcome after execution
    await skills.record_outcome(skill_id, success=True, executed_by="openclaw")
"""
from __future__ import annotations
import os
import httpx


class SkillsClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or os.getenv(
            "SKILLSHUB_URL", "http://skillshub.skillshub.svc.cluster.local:8000"
        )).rstrip("/")
        self.api_key = api_key or os.getenv("SKILLSHUB_API_KEY", "")
        self.timeout = timeout

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    async def learn(
        self,
        workflow: str,
        submitted_by: str,
        category: str | None = None,
        tags: list[str] | None = None,
        context: str | None = None,
    ) -> dict:
        """Submit a workflow description and get back a structured, validated skill (draft)."""
        payload = {
            "workflow_description": workflow,
            "submitted_by": submitted_by,
        }
        if category:
            payload["category_hint"] = category
        if tags:
            payload["tags_hint"] = tags
        if context:
            payload["context"] = context

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.base_url}/api/skills/learn",
                json=payload,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def search(
        self,
        query: str,
        top_k: int = 5,
        category: str | None = None,
        approved_only: bool = False,
    ) -> list[dict]:
        """Semantic search for skills matching a description."""
        params: dict = {"q": query, "top_k": top_k}
        if category:
            params["category"] = category
        if approved_only:
            params["trust_level"] = "approved"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{self.base_url}/api/skills/search",
                params=params,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json().get("results", [])

    async def suggest(
        self,
        context: str,
        top_k: int = 3,
        approved_only: bool = False,
    ) -> list[dict]:
        """Get skill suggestions based on current agent context/task."""
        params: dict = {"context": context, "top_k": top_k, "approved_only": str(approved_only).lower()}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{self.base_url}/api/skills/suggest",
                params=params,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json().get("suggestions", [])

    async def get(self, skill_id: str) -> dict:
        """Retrieve full skill definition by ID."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{self.base_url}/api/skills/{skill_id}",
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def record_outcome(
        self,
        skill_id: str,
        success: bool,
        executed_by: str,
        notes: str | None = None,
        duration_seconds: float | None = None,
        failed_at_step: int | None = None,
    ) -> dict:
        """Record execution outcome — improves skill stats for the fleet."""
        payload = {
            "skill_id": skill_id,
            "executed_by": executed_by,
            "success": success,
        }
        if notes:
            payload["notes"] = notes
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        if failed_at_step is not None:
            payload["failed_at_step"] = failed_at_step

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.base_url}/api/skills/outcome",
                json=payload,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def stats(self) -> dict:
        """Get skill registry statistics."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{self.base_url}/api/skills/stats/summary",
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()
