---
name: reminder_management
description: Handle reminder preferences, schedules, pauses, resumes, snoozes, and rule lookup.
triggers:
  - 以后不要提醒我午餐
  - 开启晚餐提醒
  - 暂停提醒到明天
  - 查看提醒规则
allowed_tools:
  - list_reminder_rules
  - update_reminder_rule
  - pause_reminders
  - resume_reminders
  - snooze_reminder
required_context:
  - user_id
  - reminder_rules_summary
  - chat_scope
---

# Planner Strategy

Use this skill when the user wants to view, enable, disable, pause, resume, or
snooze reminders. Prefer explicit rule updates over broad defaults. Keep
reminder changes conservative and scoped to the user's own rules unless
permissions later allow member reminders.

# Must Ask When

- The reminder type or meal scope is unclear.
- The requested schedule is not specific enough to store.
- The request would send personal summaries to a group chat.

# Forbidden

- Do not enable high-frequency reminders by default.
- Do not send personal diet details to a group chat unless explicitly allowed by
  configured reminder rules.
- Do not create intake records when managing reminders.
- Do not change anomaly or reminder facts outside the whitelisted tools.

# Examples

User: 以后不要提醒我午餐  
Plan: `update_reminder_rule` reminder_type=meal_missing, meal_type=lunch, enabled=false.

User: 暂停提醒到明天  
Plan: `pause_reminders` until=tomorrow.

User: 查看提醒规则  
Plan: `list_reminder_rules`.
