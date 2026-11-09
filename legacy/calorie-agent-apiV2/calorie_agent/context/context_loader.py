from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import os
from pathlib import Path
import re
from typing import Any, Callable

from calorie_agent.context.context_budget import estimate_chars, trim_context
from calorie_agent.context.memory_retriever import empty_memory_summary, load_user_memory_summary
from calorie_agent.context.short_term_state import load_short_term_state
from calorie_agent.memory import ProfileOptions, load_planner_profile, render_profile_for_planner
from calorie_agent.skills.skill_registry import Skill, SkillMatch, match_skills


DEFAULT_MAX_SKILLS = 5
DEFAULT_CONTEXT_BUDGET_CHARS = 8000
DEFAULT_TIMEZONE_OFFSET_HOURS = "8"


@dataclass(frozen=True)
class SkillContext:
    name: str
    description: str
    score: float
    reasons: list[str]
    allowed_tools: list[str]
    required_context: list[str]
    strategy: str
    must_ask_when: list[str]
    forbidden: list[str]
    examples: list[str]


@dataclass(frozen=True)
class ContextOptions:
    max_skills: int = field(default_factory=lambda: _int_env("AGENT_RUNTIME_V2_MAX_SKILLS", DEFAULT_MAX_SKILLS))
    context_budget_chars: int = field(
        default_factory=lambda: _int_env("AGENT_RUNTIME_V2_CONTEXT_BUDGET_CHARS", DEFAULT_CONTEXT_BUDGET_CHARS)
    )
    include_skills: bool = True
    include_short_term: bool = field(
        default_factory=lambda: _bool_env("AGENT_RUNTIME_V2_INCLUDE_SHORT_TERM", True)
    )
    include_memory: bool = field(default_factory=lambda: _bool_env("AGENT_RUNTIME_V2_INCLUDE_MEMORY", True))
    include_retrieved_facts: bool = field(
        default_factory=lambda: _bool_env("AGENT_RUNTIME_V2_INCLUDE_RETRIEVED_FACTS", True)
    )
    read_only: bool = True
    skill_dir: str | Path | None = None
    current_date: str | None = None
    timezone: str = field(default_factory=lambda: _default_timezone())
    memory_summary: dict[str, Any] | None = None
    recent_action_results: list[dict[str, Any]] | None = None
    active_pending_summary: dict[str, Any] | None = None
    last_message_summary: dict[str, Any] | None = None
    facts_provider: Callable[..., dict[str, Any]] | None = None
    planner_profile_sources: dict[str, Any] | None = None


@dataclass(frozen=True)
class DynamicContext:
    user_message: str
    actor_user_id: str
    target_user_id: str
    chat_scope: dict[str, Any]
    loaded_skills: list[SkillContext]
    short_term_context: dict[str, Any]
    structured_memory: dict[str, Any]
    retrieved_facts: dict[str, Any]
    available_tools: list[str]
    budget_report: dict[str, Any]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_dynamic_context(message: str | dict[str, Any], options: ContextOptions | None = None) -> DynamicContext:
    opts = options or ContextOptions()
    user_message = _message_text(message)
    actor_user_id = _actor_user_id(message)
    target_user_id = _target_user_id(message, actor_user_id)
    chat_scope = _chat_scope(message)
    warnings: list[str] = []

    skill_matches = _load_skill_matches(user_message, opts, warnings)
    loaded_skills = [_skill_context(match) for match in skill_matches] if opts.include_skills else []
    available_tools = sorted({tool for skill in loaded_skills for tool in skill.allowed_tools})

    if opts.include_short_term:
        short_term_context, short_term_warnings = load_short_term_state(
            user_message,
            recent_action_results=opts.recent_action_results,
            active_pending_summary=opts.active_pending_summary,
            last_message_summary=opts.last_message_summary,
            current_date=opts.current_date,
            timezone_name=opts.timezone,
        )
        warnings.extend(short_term_warnings)
    else:
        short_term_context = {}

    if opts.include_memory:
        structured_memory = load_user_memory_summary(actor_user_id, user_message, opts.memory_summary)
        structured_memory = _attach_planner_profile(actor_user_id, structured_memory, opts, warnings)
    else:
        structured_memory = empty_memory_summary()

    retrieved_facts = {}
    if opts.include_retrieved_facts:
        retrieved_facts = _default_retrieved_facts(loaded_skills, structured_memory)
        provided_facts = _load_provider_facts(opts, message, loaded_skills, warnings)
        retrieved_facts.update(provided_facts)

    dynamic_context = DynamicContext(
        user_message=user_message,
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        chat_scope=chat_scope,
        loaded_skills=loaded_skills,
        short_term_context=_sanitize_context_value(short_term_context),
        structured_memory=_sanitize_context_value(structured_memory),
        retrieved_facts=_sanitize_context_value(retrieved_facts),
        available_tools=available_tools,
        budget_report={
            "read_only": bool(opts.read_only),
            "budget_chars": int(opts.context_budget_chars),
        },
        warnings=warnings,
    )

    dynamic_context = trim_context(dynamic_context, opts.context_budget_chars)
    if estimate_chars(dynamic_context) > opts.context_budget_chars:
        warnings = list(dynamic_context.warnings)
        if "context_over_budget_after_trim" not in warnings:
            warnings.append("context_over_budget_after_trim")
        dynamic_context = replace(dynamic_context, warnings=warnings)
    return dynamic_context


def _load_skill_matches(user_message: str, opts: ContextOptions, warnings: list[str]) -> list[SkillMatch]:
    if not opts.include_skills:
        return []
    try:
        return match_skills(
            user_message,
            max_skills=max(0, int(opts.max_skills)),
            skill_dir=_resolve_skill_dir(opts.skill_dir),
        )
    except Exception as exc:
        warnings.append(f"skill_load_failed:{type(exc).__name__}")
        return []


def _skill_context(match: SkillMatch) -> SkillContext:
    skill = match.skill
    return SkillContext(
        name=skill.name,
        description=skill.description,
        score=match.score,
        reasons=list(match.reasons),
        allowed_tools=list(skill.allowed_tools),
        required_context=list(skill.required_context),
        strategy=_extract_section(skill, "Planner Strategy"),
        must_ask_when=_section_bullets(skill, "Must Ask When"),
        forbidden=_section_bullets(skill, "Forbidden"),
        examples=_section_blocks(skill, "Examples"),
    )


def _default_retrieved_facts(
    loaded_skills: list[SkillContext],
    structured_memory: dict[str, Any],
) -> dict[str, Any]:
    names = _fact_relevant_skill_names(loaded_skills)
    facts: dict[str, Any] = {}

    if "record_food" in names:
        facts["food_database_summary"] = {"source": "not_loaded", "reason": "tool_executor_owns_food_lookup"}

    if "query_food_nutrition" in names:
        facts["today_intake_summary"] = {"source": "not_configured", "items": [], "totals": {}}

    if "semantic_undo" in names:
        facts["today_intake_groups_summary"] = {"source": "not_configured", "groups": []}
        facts["permission_summary"] = structured_memory.get(
            "permissions_summary",
            {"source": "not_configured", "rules": []},
        )

    if "daily_report" in names:
        facts["period_summary"] = {"source": "not_configured", "period": "", "facts": []}
        facts["standard_summary"] = {"source": "not_configured", "standard": {}}

    if "reminder_management" in names:
        facts["reminder_rules_summary"] = {"source": "not_configured", "rules": []}

    if "family_member" in names:
        facts["member_relations_summary"] = structured_memory.get(
            "member_relations_summary",
            {"source": "not_configured", "members": []},
        )
        facts["permission_summary"] = structured_memory.get(
            "permissions_summary",
            {"source": "not_configured", "rules": []},
        )

    return facts


def _fact_relevant_skill_names(loaded_skills: list[SkillContext]) -> set[str]:
    if not loaded_skills:
        return set()

    top = loaded_skills[0]
    names = {top.name}
    top_score = max(1.0, float(top.score))

    for skill in loaded_skills[1:]:
        if skill.score >= 100.0 or skill.score >= top_score * 0.6:
            names.add(skill.name)

    if top.name == "family_member":
        for skill in loaded_skills[1:]:
            if skill.name == "record_food" and skill.score >= 40.0:
                names.add("record_food")

    return names


def _load_provider_facts(
    opts: ContextOptions,
    message: str | dict[str, Any],
    loaded_skills: list[SkillContext],
    warnings: list[str],
) -> dict[str, Any]:
    if opts.facts_provider is None:
        return {}
    try:
        skill_names = [skill.name for skill in loaded_skills]
        return opts.facts_provider(message=message, skill_names=skill_names, read_only=opts.read_only) or {}
    except TypeError:
        try:
            return opts.facts_provider(message, loaded_skills) or {}
        except Exception as exc:
            warnings.append(f"facts_load_failed:{type(exc).__name__}")
            return {}
    except Exception as exc:
        warnings.append(f"facts_load_failed:{type(exc).__name__}")
        return {}


def _attach_planner_profile(
    user_id: str,
    structured_memory: dict[str, Any],
    opts: ContextOptions,
    warnings: list[str],
) -> dict[str, Any]:
    result = dict(structured_memory)
    if not _bool_env("PLANNER_PROFILE_ENABLED", True):
        result["planner_profile"] = {}
        return result

    source = opts.planner_profile_sources
    if source is None and isinstance(opts.memory_summary, dict):
        source = opts.memory_summary

    try:
        profile = load_planner_profile(
            user_id,
            ProfileOptions(
                include_trace_stats=_bool_env("PLANNER_PROFILE_INCLUDE_TRACE_STATS", True),
                max_aliases=_int_env("PLANNER_PROFILE_MAX_ALIASES", 20),
                max_patterns=_int_env("PLANNER_PROFILE_MAX_PATTERNS", 10),
                min_confidence=_float_env("PLANNER_PROFILE_MIN_CONFIDENCE", 0.7),
                enabled=True,
                profile_sources=source,
            ),
        )
    except Exception as exc:
        warnings.append(f"planner_profile_load_failed:{type(exc).__name__}")
        result["planner_profile"] = {}
        return result

    result["planner_profile"] = render_profile_for_planner(profile)
    return result


def _message_text(message: str | dict[str, Any]) -> str:
    if isinstance(message, str):
        return message.strip()
    for key in ("text", "content", "raw_text", "message"):
        value = message.get(key)
        if value:
            return str(value).strip()
    return ""


def _actor_user_id(message: str | dict[str, Any]) -> str:
    if not isinstance(message, dict):
        return ""
    for key in ("actor_user_id", "user_id", "open_id", "sender_id"):
        value = message.get(key)
        if value:
            return str(value)
    return ""


def _target_user_id(message: str | dict[str, Any], actor_user_id: str) -> str:
    if not isinstance(message, dict):
        return actor_user_id
    for key in ("target_user_id", "member_user_id"):
        value = message.get(key)
        if value:
            return str(value)
    return actor_user_id


def _chat_scope(message: str | dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {"chat_id": "", "chat_type": "private", "is_group": False}
    chat_type = str(message.get("chat_type") or message.get("conversation_type") or "private")
    return {
        "chat_id": str(message.get("chat_id") or ""),
        "chat_type": chat_type,
        "is_group": chat_type.lower() in {"group", "chat", "group_chat"},
    }


def _extract_section(skill: Skill, heading: str) -> str:
    lines = _section_lines(skill.body, heading)
    return "\n".join(lines).strip()


def _section_bullets(skill: Skill, heading: str) -> list[str]:
    values: list[str] = []
    for line in _section_lines(skill.body, heading):
        stripped = line.strip()
        if stripped.startswith("- "):
            values.append(stripped[2:].strip())
    return values


def _section_blocks(skill: Skill, heading: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in _section_lines(skill.body, heading):
        if line.startswith("User:") and current:
            blocks.append("\n".join(current).strip())
            current = [line]
            continue
        if line.strip():
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return blocks[:4]


def _section_lines(body: str, heading: str) -> list[str]:
    pattern = re.compile(r"^#\s+" + re.escape(heading) + r"\s*$", re.IGNORECASE)
    next_heading = re.compile(r"^#\s+")
    lines = body.splitlines()
    start = None
    for index, line in enumerate(lines):
        if pattern.match(line.strip()):
            start = index + 1
            break
    if start is None:
        return []
    end = len(lines)
    for index in range(start, len(lines)):
        if next_heading.match(lines[index].strip()):
            end = index
            break
    return lines[start:end]


def _resolve_skill_dir(skill_dir: str | Path | None) -> str | Path | None:
    configured = skill_dir or os.getenv("AGENT_RUNTIME_V2_SKILL_DIR", "")
    if not configured:
        return None
    path = Path(configured)
    if path.is_absolute():
        return path
    scf_root = Path(__file__).resolve().parents[2]
    candidate = scf_root / path
    if candidate.exists():
        return candidate
    return path


def _sanitize_context_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                continue
            result[key] = _sanitize_context_value(item)
        return result
    if isinstance(value, list):
        return [_sanitize_context_value(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    sensitive_fragments = (
        "api_key",
        "app_secret",
        "access_token",
        "tenant_access_token",
        "authorization",
        "password",
        "secret",
    )
    return any(fragment in normalized for fragment in sensitive_fragments)


def _default_timezone() -> str:
    offset = os.getenv("APP_TIMEZONE_OFFSET_HOURS", DEFAULT_TIMEZONE_OFFSET_HOURS)
    return f"UTC+{offset}" if not str(offset).startswith("-") else f"UTC{offset}"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
