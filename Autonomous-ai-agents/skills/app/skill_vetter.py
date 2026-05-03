"""
SkillVetter -- 2nd-pass security audit on top of skill_validator.SecurityValidator.

Inspired by @spclaudehome/Skill Vetter (ClawHub). Rebuilt from scratch so it
plugs into our existing SecurityValidator + Pydantic Skill schema and never
auto-promotes trust_level.

Run as part of validate_skill() (called at the end) or standalone via
run_second_pass(skill, first_pass_result).
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from skill_schema import Skill

ALLOWED_HOSTS_PATH = Path("/app/config/allowed_hosts.json")
PATTERN_FAMILIES_PATH = Path("/app/config/pattern_families.json")

_DEFAULT_ALLOWED_HOSTS = [
    "skillshub.autonomous-ai-agents.svc",
    "reme.autonomous-ai-agents.svc",
    "hiclaw.autonomous-ai-agents.svc",
    "ollama.autonomous-ai-agents.svc",
    "growthops.autonomous-ai-agents.svc",
    "localhost",
]

# Pattern families catch shapes the static blocklist misses.
_DEFAULT_FAMILIES = {
    "exfiltration_post": {
        "regex": r"(http_post|webhook).*(pastebin|requestbin|ngrok|webhook\.site|burp)",
        "severity": 0.6,
        "description": "POST to known exfiltration sinks",
    },
    "credential_path_read": {
        "regex": r"(/var/run/secrets|/root/\.ssh|\.aws/credentials|kubeconfig|/etc/shadow)",
        "severity": 0.6,
        "description": "Reads credential / secret paths",
    },
    "prompt_injection_sink": {
        # ollama_generate consuming raw external content with no sanitization keyword
        "regex": r"ollama_generate.*\b(file_read|http_get).*\b(?!sanitiz|escape|strip)",
        "severity": 0.3,
        "description": "LLM consumes external content with no sanitization step",
    },
    "wildcard_filesystem": {
        "regex": r"(file_(read|write)).*(\.\./|/\*|\*\*/\*\*)",
        "severity": 0.4,
        "description": "Wildcard or path-traversal in filesystem step",
    },
}

# Read-only category should not contain mutating tools.
_MUTATING_TOOLS = {"file_write", "http_post", "delegate_to_agent"}
_READ_ONLY_CATEGORIES = {"monitoring", "analysis", "intelligence"}


@dataclass
class VetterVerdict:
    composite_score: float = 1.0
    permission_score: float = 1.0
    intent_score: float = 1.0
    family_score: float = 1.0
    domain_score: float = 1.0
    flags: list[str] = field(default_factory=list)
    family_hits: list[str] = field(default_factory=list)
    excess_tools: list[str] = field(default_factory=list)
    bad_hosts: list[str] = field(default_factory=list)
    recommended_trust: str = "draft"
    passed: bool = True

    def as_dict(self) -> dict:
        return {
            "composite_score": round(self.composite_score, 3),
            "permission_score": round(self.permission_score, 3),
            "intent_score": round(self.intent_score, 3),
            "family_score": round(self.family_score, 3),
            "domain_score": round(self.domain_score, 3),
            "flags": self.flags,
            "family_hits": self.family_hits,
            "excess_tools": self.excess_tools,
            "bad_hosts": self.bad_hosts,
            "recommended_trust": self.recommended_trust,
            "passed": self.passed,
        }


def _load_allowed_hosts() -> list[str]:
    try:
        return json.loads(ALLOWED_HOSTS_PATH.read_text()).get("hosts", _DEFAULT_ALLOWED_HOSTS)
    except Exception:
        return _DEFAULT_ALLOWED_HOSTS


def _load_families() -> dict:
    try:
        return json.loads(PATTERN_FAMILIES_PATH.read_text())
    except Exception:
        return _DEFAULT_FAMILIES


def _check_permissions(skill: Skill, verdict: VetterVerdict) -> None:
    tools_used = {step.tool for step in skill.steps}
    if skill.category in _READ_ONLY_CATEGORIES:
        excess = tools_used & _MUTATING_TOOLS
        if excess:
            verdict.excess_tools = sorted(excess)
            verdict.flags.append(
                f"Read-only category '{skill.category}' uses mutating tools: {sorted(excess)}"
            )
            verdict.permission_score = max(0.0, 1.0 - 0.3 * len(excess))


def _check_intent(skill: Skill, verdict: VetterVerdict) -> None:
    desc = skill.description.lower()
    behavior = " ".join(s.description.lower() for s in skill.steps)
    # Cheap heuristic: read-only words in description but mutating tools present
    read_only_words = ("read", "fetch", "list", "scan", "monitor", "detect", "analyze", "audit")
    mutating_tools_present = any(
        s.tool in _MUTATING_TOOLS for s in skill.steps
    )
    if any(w in desc for w in read_only_words) and "write" not in desc and "post" not in desc:
        if mutating_tools_present:
            verdict.flags.append("Description implies read-only but steps mutate.")
            verdict.intent_score = 0.6


def _check_families(skill: Skill, verdict: VetterVerdict) -> None:
    families = _load_families()
    blob = json.dumps(skill.model_dump()).lower()
    severity_sum = 0.0
    for name, fam in families.items():
        try:
            if re.search(fam["regex"], blob, re.IGNORECASE):
                verdict.family_hits.append(name)
                verdict.flags.append(f"Pattern family hit: {name} ({fam.get('description', '')})")
                severity_sum += float(fam.get("severity", 0.3))
        except re.error:
            continue
    verdict.family_score = max(0.0, 1.0 - severity_sum)


def _check_hosts(skill: Skill, verdict: VetterVerdict) -> None:
    allowed = _load_allowed_hosts()
    bad = []
    for step in skill.steps:
        if step.tool not in {"http_get", "http_post"}:
            continue
        url = step.params.get("url") or step.params.get("endpoint") or ""
        if not url:
            continue
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            host = ""
        if host and not any(host == a or host.endswith("." + a) for a in allowed):
            bad.append(host)
    if bad:
        verdict.bad_hosts = bad
        verdict.flags.append(f"Non-allowlisted hosts referenced: {bad}")
        verdict.domain_score = max(0.0, 1.0 - 0.2 * len(bad))


def run_second_pass(skill: Skill, first_pass_score: float = 1.0) -> VetterVerdict:
    """Apply 2nd-pass audit on top of an already-validated skill.

    Composite formula:
        0.50 * first_pass_score
      + 0.20 * permission_score
      + 0.15 * intent_score
      + 0.10 * family_score
      + 0.05 * domain_score
    """
    verdict = VetterVerdict()
    _check_permissions(skill, verdict)
    _check_intent(skill, verdict)
    _check_families(skill, verdict)
    _check_hosts(skill, verdict)

    composite = (
        0.50 * first_pass_score
        + 0.20 * verdict.permission_score
        + 0.15 * verdict.intent_score
        + 0.10 * verdict.family_score
        + 0.05 * verdict.domain_score
    )
    verdict.composite_score = composite

    # Trust recommendation -- can only LOWER trust, never raise it.
    if composite >= 0.95:
        verdict.recommended_trust = "production"
    elif composite >= 0.85:
        verdict.recommended_trust = "staging"
    elif composite >= 0.60:
        verdict.recommended_trust = "draft"
        verdict.passed = False
    else:
        verdict.recommended_trust = "quarantined"
        verdict.passed = False

    return verdict
