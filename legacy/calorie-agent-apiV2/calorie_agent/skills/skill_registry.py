from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata
from typing import Any


DEFAULT_SKILL_DIR = Path(__file__).resolve().parent
DEFAULT_RENDER_BUDGET_CHARS = 6000

_CHINESE_HINTS = [
    "记录",
    "吃了",
    "早餐",
    "午餐",
    "晚餐",
    "克",
    "热量",
    "碳水",
    "蛋白",
    "蛋白质",
    "脂肪",
    "营养",
    "那条",
    "多少",
    "撤销",
    "删除",
    "上一条",
    "上上条",
    "之前",
    "总结",
    "周报",
    "月报",
    "趋势",
    "最近",
    "本周",
    "这周",
    "上周",
    "提醒",
    "暂停",
    "恢复",
    "开启",
    "关闭",
    "妈妈",
    "爸爸",
    "家庭",
    "成员",
    "权限",
]

_SKILL_HINTS = {
    "record_food": ["记录", "吃了", "早餐", "午餐", "晚餐", "克", "g", "usual", "combo"],
    "query_food_nutrition": ["热量", "碳水", "蛋白", "蛋白质", "脂肪", "营养", "那条", "多少"],
    "semantic_undo": ["撤销", "删除", "上一条", "上上条", "之前", "带"],
    "daily_report": ["总结", "日报", "周报", "月报", "趋势", "最近", "本周", "这周", "上周", "7天", "7 天"],
    "reminder_management": ["提醒", "不要提醒", "暂停", "恢复", "开启", "关闭", "午餐提醒", "晚餐提醒"],
    "family_member": ["妈妈", "爸爸", "家庭", "成员", "权限", "给妈妈", "查爸爸"],
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    triggers: list[str]
    allowed_tools: list[str]
    required_context: list[str]
    body: str
    path: str


@dataclass(frozen=True)
class SkillMatch:
    skill: Skill
    score: float
    reasons: list[str]


def load_skills(skill_dir: str | Path | None = None) -> list[Skill]:
    root = Path(skill_dir) if skill_dir is not None else DEFAULT_SKILL_DIR
    if not root.exists():
        return []

    skills: list[Skill] = []
    for path in sorted(root.glob("*.md")):
        parsed = _parse_skill_file(path)
        if parsed is not None:
            skills.append(parsed)
    return skills


def list_skill_names(skill_dir: str | Path | None = None) -> list[str]:
    return [skill.name for skill in load_skills(skill_dir)]


def match_skills(
    message: str,
    max_skills: int = 5,
    skills: list[Skill] | None = None,
    skill_dir: str | Path | None = None,
) -> list[SkillMatch]:
    if max_skills <= 0:
        return []

    available = skills if skills is not None else load_skills(skill_dir)
    matches = [_score_skill(message, skill) for skill in available]
    matches = [match for match in matches if match.score > 0]
    matches.sort(key=lambda match: (-match.score, match.skill.name))
    return matches[:max_skills]


def render_skills_for_prompt(
    matches: list[SkillMatch],
    max_chars: int = DEFAULT_RENDER_BUDGET_CHARS,
) -> str:
    rendered: list[str] = []
    remaining = max(0, max_chars)
    for match in matches:
        section = _render_skill(match)
        if len(section) > remaining:
            if remaining <= 0:
                break
            rendered.append(section[:remaining].rstrip())
            break
        rendered.append(section)
        remaining -= len(section)
    return "\n\n".join(part for part in rendered if part)


def validate_skill(skill: Skill) -> list[str]:
    errors: list[str] = []
    if not skill.name:
        errors.append("missing name")
    if not skill.description:
        errors.append(f"{skill.name or skill.path}: missing description")
    if not skill.allowed_tools:
        errors.append(f"{skill.name or skill.path}: missing allowed_tools")
    if "Planner Strategy" not in skill.body:
        errors.append(f"{skill.name or skill.path}: missing Planner Strategy section")
    if "Forbidden" not in skill.body:
        errors.append(f"{skill.name or skill.path}: missing Forbidden section")
    return errors


def _parse_skill_file(path: Path) -> Skill | None:
    text = path.read_text(encoding="utf-8")
    metadata, body = _parse_front_matter(text)
    if not metadata:
        return None

    return Skill(
        name=str(metadata.get("name", "")).strip(),
        description=str(metadata.get("description", "")).strip(),
        triggers=_as_list(metadata.get("triggers")),
        allowed_tools=_as_list(metadata.get("allowed_tools")),
        required_context=_as_list(metadata.get("required_context")),
        body=body.strip(),
        path=str(path),
    )


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, text

    metadata: dict[str, Any] = {}
    current_key = ""
    for line in lines[1:end_index]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") and current_key:
            metadata.setdefault(current_key, []).append(stripped[2:].strip())
            continue
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        current_key = key.strip()
        value = raw_value.strip()
        metadata[current_key] = value if value else []

    return metadata, "\n".join(lines[end_index + 1 :])


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _score_skill(message: str, skill: Skill) -> SkillMatch:
    message_norm = _normalize(message)
    message_terms = _terms(message)
    score = 0.0
    reasons: list[str] = []

    for trigger in skill.triggers:
        trigger_norm = _normalize(trigger)
        if not trigger_norm:
            continue
        if trigger_norm in message_norm or message_norm in trigger_norm:
            score += 100.0
            reasons.append(f"trigger:{trigger}")
            continue

        trigger_terms = _terms(trigger)
        common = sorted(message_terms & trigger_terms)
        if common:
            score += min(45.0, 15.0 + len(common) * 7.0)
            reasons.append("trigger_terms:" + ",".join(common[:4]))

    for hint in _SKILL_HINTS.get(skill.name, []):
        if _normalize(hint) in message_norm:
            score += 12.0
            reasons.append(f"hint:{hint}")

    for name_part in skill.name.split("_"):
        if name_part and name_part in message_norm:
            score += 8.0
            reasons.append(f"name:{name_part}")

    description_terms = _terms(skill.description)
    common_description = sorted(message_terms & description_terms)
    if common_description:
        score += min(12.0, len(common_description) * 3.0)
        reasons.append("description_terms:" + ",".join(common_description[:3]))

    return SkillMatch(skill=skill, score=score, reasons=_dedupe(reasons))


def _render_skill(match: SkillMatch) -> str:
    skill = match.skill
    lines = [
        f"## Skill: {skill.name}",
        f"Description: {skill.description}",
        f"Allowed tools: {', '.join(skill.allowed_tools)}",
        f"Required context: {', '.join(skill.required_context)}",
        f"Match score: {match.score:.1f}",
        f"Match reasons: {', '.join(match.reasons)}",
        "",
        skill.body,
    ]
    return "\n".join(lines).strip()


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", "", normalized)


def _terms(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    terms = set(re.findall(r"[a-z0-9_]+", normalized))
    compact = _normalize(normalized)
    for hint in _CHINESE_HINTS:
        if _normalize(hint) in compact:
            terms.add(hint)
    return {term for term in terms if term}


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
