---
name: record_food
description: Handle food intake recording, including multi-food messages, meals, and saved combos.
triggers:
  - 米饭200g
  - 早餐鸡蛋100g，中午米饭200g
  - 记录鸡胸肉150g
  - record usual lunch
allowed_tools:
  - record_food
  - search_food
  - create_pending
  - query_today
required_context:
  - user_id
  - food_database_summary
  - active_pending
  - memory_aliases
  - saved_combos
---

# Planner Strategy

Use this skill when the user wants to record food intake. Split multi-food
messages into ordered `record_food` actions. Preserve explicit meal types such
as breakfast, lunch, dinner, post workout, 早餐, 午餐, 晚餐, and 练后. If the user
asks to record a saved combo, use the combo-related tool path already available
to the current runtime rather than inventing food items.

If a record request also asks for today's remaining target, plan record actions
first and then a read-only `query_today` action.

# Must Ask When

- Food is unknown and the user did not provide per-100g nutrition.
- Grams are missing and no confirmed default grams can be used.
- Fuzzy food matching returns ambiguous candidates.
- The user refers to a combo that is not found.

# Forbidden

- Do not calculate nutrition in the planner.
- Do not invent foods that are absent from the food table.
- Do not silently write an intake record when grams are missing.
- Do not bypass pending confirmation for missing food or ambiguous matches.

# Examples

User: 早餐鸡蛋100g，中午米饭200g  
Plan: `record_food` egg 100g breakfast, then `record_food` rice 200g lunch.

User: 米饭  
Plan: ask for grams or create the appropriate missing-grams pending task.

User: 早餐鸡蛋100g，查下今天还剩多少  
Plan: `record_food` egg 100g breakfast, then `query_today`.
