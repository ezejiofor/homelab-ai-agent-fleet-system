"""
SkillValidator — security gate for all submitted skills.

Checks every string value in the skill (name, description, step params)
against a blocklist of dangerous patterns. Computes a security score.
Skills with score < 0.7 are rejected; 0.7-0.9 flagged; 0.9+ clean.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from skill_schema import Skill
try:
    from skill_vetter import run_second_pass  # 2nd-pass audit
except Exception:
    run_second_pass = None  # type: ignore


CONFIG_PATH = Path("/app/config/blocked_patterns.json")
_DEFAULT_BLOCKED = [
    "rm -rf", "rmdir", "shred", "dd if=",
    "kubectl delete namespace", "kubectl delete node",
    "DROP TABLE", "DROP DATABASE", "TRUNCATE",
    "format ", "mkfs", "fdisk",
    "eval(", "exec(", "__import__", "subprocess",
    "os.system", "os.popen", "popen(",
    "curl.*|.*bash", "wget.*|.*sh",
    "base64 -d", "base64 --decode",
    "chmod 777", "chmod -R 777",
    "iptables -F", "iptables --flush",
    "systemctl stop", "systemctl disable",
    "vault token revoke -self",
    "kubectl drain --force",
    "shutdown", "reboot", "halt", "poweroff",
    ":(){ :|:& };:", "/dev/sda", "/dev/nvme", "/dev/vda",
]


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {"patterns": _DEFAULT_BLOCKED, "max_steps": 15}


def _extract_all_strings(obj, depth: int = 0) -> list[str]:
    """Recursively extract all string values from a dict/list structure."""
    if depth > 6:
        return []
    results = []
    if isinstance(obj, str):
        results.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            results.extend(_extract_all_strings(v, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_extract_all_strings(item, depth + 1))
    return results


class ValidationResult:
    def __init__(self):
        self.flags: list[str] = []
        self.score: float = 1.0
        self.passed: bool = True

    def flag(self, msg: str, severity: float = 0.2):
        self.flags.append(msg)
        self.score = max(0.0, self.score - severity)
        if self.score < 0.7:
            self.passed = False


def validate_skill(skill: Skill) -> ValidationResult:
    cfg = _load_config()
    patterns = cfg.get("patterns", _DEFAULT_BLOCKED)
    max_steps = cfg.get("max_steps", 15)

    result = ValidationResult()

    # 1. Step count
    if len(skill.steps) > max_steps:
        result.flag(f"Too many steps: {len(skill.steps)} > {max_steps}", severity=0.3)

    # 2. Scan all string content for blocked patterns
    all_strings = _extract_all_strings(skill.model_dump())
    for text in all_strings:
        text_lower = text.lower()
        for pattern in patterns:
            # Support simple regex patterns (those containing .* or |)
            if ".*" in pattern or "|" in pattern:
                try:
                    if re.search(pattern, text_lower, re.IGNORECASE):
                        result.flag(f"Blocked pattern matched: '{pattern}'", severity=0.4)
                except re.error:
                    if pattern.lower() in text_lower:
                        result.flag(f"Blocked pattern found: '{pattern}'", severity=0.4)
            else:
                if pattern.lower() in text_lower:
                    result.flag(f"Blocked pattern found: '{pattern}'", severity=0.4)

    # 3. Validate step tool allowlist (already enforced by Pydantic AllowedTool enum,
    #    but double-check params don't embed shell commands)
    for step in skill.steps:
        params_str = json.dumps(step.params).lower()
        for pattern in patterns:
            if pattern.lower() in params_str:
                result.flag(f"Step {step.id} params contain blocked pattern: '{pattern}'", severity=0.5)

    # 4. Name length
    if len(skill.name) > 60:
        result.flag("Skill name exceeds 60 characters", severity=0.05)

    # 5. Empty steps
    for step in skill.steps:
        if not step.description.strip():
            result.flag(f"Step {step.id} has empty description", severity=0.1)

    # --- 2nd-pass audit (Skill Vetter): can only LOWER trust, never raise it. ---
    if run_second_pass is not None:
        try:
            verdict = run_second_pass(skill, first_pass_score=result.score)
            for f in verdict.flags:
                result.flag(f"[vetter] {f}", severity=0.0)  # informational, score already accounted for
            result.score = min(result.score, verdict.composite_score)
            result.passed = result.passed and verdict.passed
            # attach for callers that want detail
            setattr(result, "vetter", verdict.as_dict())
        except Exception as exc:
            result.flag(f"[vetter] second-pass failed: {exc}", severity=0.05)
    return result
