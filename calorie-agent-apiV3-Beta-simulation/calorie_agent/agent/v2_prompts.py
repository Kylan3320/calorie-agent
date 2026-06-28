from __future__ import annotations

import json
from typing import Any


V2_PLANNER_PROMPT_VERSION = "agent_planner.v2.skill_aware"

V2_PLANNER_SYSTEM_PROMPT = """
You are the skill-aware planner for a Feishu calorie tracking agent.
Return valid JSON only. Do not include markdown.

Core rules:
- You may choose only tools listed in available_tools.
- You only produce an action plan. You do not execute tools.
- You do not calculate kcal, carbs, protein, fat, or other nutrition facts.
- You do not invent food nutrition or intake facts.
- You do not write, delete, or update tables directly.
- Skills are strategy notes, not execution results.
- Memory is a planning hint, not a fact source.
- Planner profile is a preference hint; it never overrides tool facts or permissions.
- If one user message contains multiple tasks, split them into ordered actions.
- If a request is ambiguous or dangerous, set needs_confirmation=true.
- For nutrition questions about a recorded intake item, prefer
  query_intake_food_nutrition when it is available.

Return this JSON shape:
{
  "schema_version": "agent_plan.v2",
  "goal": "short_goal",
  "actions": [
    {
      "action_id": "a1",
      "tool": "tool_from_available_tools",
      "parameters": {},
      "reason": "short reason",
      "safety": "read_only|write|destructive|permission_sensitive",
      "depends_on": []
    }
  ],
  "used_skills": ["skill_name"],
  "confidence": 0.0,
  "needs_confirmation": false,
  "confirmation_reason": null,
  "planner_notes": null
}
""".strip()


def build_v2_planner_messages(dynamic_context: Any) -> list[dict[str, str]]:
    context = _context_payload(dynamic_context)
    return [
        {"role": "system", "content": V2_PLANNER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Dynamic context JSON:\n"
            + json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n\nUser message:\n"
            + str(context.get("user_message") or ""),
        },
    ]


def _context_payload(dynamic_context: Any) -> dict[str, Any]:
    payload = dynamic_context.to_dict() if hasattr(dynamic_context, "to_dict") else dict(dynamic_context or {})
    loaded_skills = []
    for skill in payload.get("loaded_skills", []) if isinstance(payload.get("loaded_skills"), list) else []:
        if not isinstance(skill, dict):
            continue
        loaded_skills.append(
            {
                "name": skill.get("name", ""),
                "description": skill.get("description", ""),
                "allowed_tools": skill.get("allowed_tools", []),
                "required_context": skill.get("required_context", []),
                "strategy": skill.get("strategy", ""),
                "must_ask_when": skill.get("must_ask_when", []),
                "forbidden": skill.get("forbidden", []),
            }
        )

    return {
        "user_message": payload.get("user_message", ""),
        "actor_user_id": payload.get("actor_user_id", ""),
        "target_user_id": payload.get("target_user_id", ""),
        "chat_scope": payload.get("chat_scope", {}),
        "available_tools": payload.get("available_tools", []),
        "loaded_skills": loaded_skills,
        "short_term_context": payload.get("short_term_context", {}),
        "structured_memory": payload.get("structured_memory", {}),
        "retrieved_facts": payload.get("retrieved_facts", {}),
        "warnings": payload.get("warnings", []),
    }
