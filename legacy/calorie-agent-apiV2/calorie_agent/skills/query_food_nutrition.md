---
name: query_food_nutrition
description: Handle questions about nutrition values for a recorded intake item.
triggers:
  - 今天西瓜那条多少热量
  - 今天西瓜碳水蛋白脂肪是多少
  - 午餐米饭那条碳水多少
  - 刚才那个水果蛋白质多少
allowed_tools:
  - query_intake_food_nutrition
  - query_today
required_context:
  - user_id
  - date_scope
  - intake_records_summary
---

# Planner Strategy

Use this skill when the user asks about kcal, carbs, protein, fat, or nutrition
for a food that was already recorded. Prefer `query_intake_food_nutrition` with
the user's date scope and food query. Use meal type or record group only when
the user explicitly provides them.

If the question is about today's full total rather than one recorded food, use
`query_today` instead.

# Must Ask When

- Multiple records match and the user did not specify which one.
- No recorded intake item matches the food query.
- The user asks about a food that has not been recorded and is not asking to
  create a new food.

# Forbidden

- Do not recalculate historical intake from the current food database.
- Do not let the LLM estimate kcal, carbs, protein, or fat.
- Do not assume which record the user meant when several records match.
- Do not write or delete records for a nutrition query.

# Examples

User: 今天西瓜那条多少热量  
Plan: `query_intake_food_nutrition` date_scope=today, food_query=西瓜.

User: 午餐米饭那条碳水多少  
Plan: `query_intake_food_nutrition` date_scope=today, food_query=米饭, meal_type=lunch.

User: 今天西瓜碳水蛋白脂肪是多少  
Plan: `query_intake_food_nutrition` date_scope=today, food_query=西瓜.
