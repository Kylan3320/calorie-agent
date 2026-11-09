---
name: semantic_undo
description: Handle semantic undo requests for previous or food-specific intake records.
triggers:
  - 撤销上一条
  - 删除上上条
  - 撤销之前带西瓜的那条
  - 撤销午餐那个水果
allowed_tools:
  - undo_intake
  - query_today
required_context:
  - user_id
  - today_intake_groups
  - last_action_result
---

# Planner Strategy

Use this skill when the user asks to revoke, undo, delete, or cancel an intake
record. Convert explicit positions such as previous, previous-previous, 上一条,
and 上上条 into semantic undo targets. Convert food-specific requests such as
带西瓜的那条 into contains-food targets.

Use `query_today` only when the planner needs read-only context to resolve a
target before asking the user to confirm.

# Must Ask When

- The target could refer to multiple records or message groups.
- The user uses vague wording and confidence is low.
- The request appears to affect another user's data.

# Forbidden

- Do not delete on low confidence.
- Do not bypass permission or ownership checks.
- Do not hard-delete records from chat requests.
- Do not treat a nutrition query as an undo request just because it mentions a
  food from a previous record.

# Examples

User: 撤销上一条  
Plan: `undo_intake` target mode latest.

User: 撤销之前带西瓜的那条  
Plan: `undo_intake` target mode contains_food, food_query=西瓜.

User: 删除上上条  
Plan: `undo_intake` target mode ordinal_group, ordinal=-2.
