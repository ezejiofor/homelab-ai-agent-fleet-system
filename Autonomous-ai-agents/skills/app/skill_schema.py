"""
Skill schema — the canonical data model for an agent skill.
All skills start as "draft"; humans promote to "approved".
"""
from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field
import time
import uuid


class TrustLevel(str, Enum):
    draft = "draft"
    reviewed = "reviewed"
    approved = "approved"
    deprecated = "deprecated"


class FailureAction(str, Enum):
    abort = "abort"
    retry = "retry"
    skip = "skip"
    delegate = "delegate"


class AllowedTool(str, Enum):
    k8s_query = "k8s_query"
    k8s_apply = "k8s_apply"
    helm_install = "helm_install"
    vault_read = "vault_read"
    vault_write = "vault_write"
    http_get = "http_get"
    http_post = "http_post"
    file_read = "file_read"
    file_write = "file_write"
    ollama_generate = "ollama_generate"
    wait_condition = "wait_condition"
    delegate_to_agent = "delegate_to_agent"


class SkillCategory(str, Enum):
    infrastructure = "infrastructure"
    security = "security"
    monitoring = "monitoring"
    deployment = "deployment"
    data = "data"
    networking = "networking"
    storage = "storage"
    ai = "ai"
    general = "general"


class SkillStep(BaseModel):
    id: int
    title: str = Field(..., max_length=100)
    description: str = Field(..., max_length=500)
    tool: AllowedTool
    params: dict[str, Any] = Field(default_factory=dict)
    expected_output: str = ""
    on_failure: FailureAction = FailureAction.abort


class Skill(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    version: int = 1
    name: str = Field(..., max_length=60)
    description: str = Field(..., max_length=2000)
    category: SkillCategory = SkillCategory.general
    tags: list[str] = Field(default_factory=list)
    trigger_conditions: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    steps: list[SkillStep] = Field(..., min_length=1, max_length=15)
    success_criteria: str = ""
    estimated_duration_minutes: int = 5
    reversible: bool = True
    rollback_steps: list[SkillStep] = Field(default_factory=list)

    # Trust + audit
    trust_level: TrustLevel = TrustLevel.draft
    created_by: str = "unknown"          # agent name or "human"
    approved_by: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    approved_at: float | None = None

    # Security validation results
    security_score: float = 0.0         # 0.0 (unsafe) to 1.0 (clean)
    security_flags: list[str] = Field(default_factory=list)

    # Execution stats
    execution_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_executed_at: float | None = None
    last_executed_by: str | None = None

    def success_rate(self) -> float:
        if self.execution_count == 0:
            return 0.0
        return round(self.success_count / self.execution_count, 3)

    def to_storage_dict(self) -> dict:
        return self.model_dump()


class SkillLearnRequest(BaseModel):
    workflow_description: str = Field(
        ...,
        min_length=50,
        max_length=10000,
        description="Natural language description of the workflow/process to learn",
    )
    submitted_by: str = Field(..., description="Agent name or 'human'")
    category_hint: SkillCategory | None = None
    tags_hint: list[str] = Field(default_factory=list)
    context: str | None = Field(None, description="Additional context about when this skill should be used")


class SkillOutcomeRequest(BaseModel):
    skill_id: str
    executed_by: str
    success: bool
    notes: str | None = None
    duration_seconds: float | None = None
    failed_at_step: int | None = None


class SkillApproveRequest(BaseModel):
    approved_by: str
    notes: str | None = None
