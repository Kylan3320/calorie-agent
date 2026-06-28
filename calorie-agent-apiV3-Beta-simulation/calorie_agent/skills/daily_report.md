---
name: daily_report
description: Handle daily, weekly, monthly, trend, comparison, and target-gap report questions.
triggers:
  - 今天总结
  - 本周周报
  - 最近 7 天蛋白质有没有变少
  - 这周比上周热量高了吗
allowed_tools:
  - generate_daily_report
  - generate_weekly_report
  - generate_monthly_report
  - query_trend
  - compare_period
required_context:
  - user_id
  - date_scope
  - report_period
  - standard_summary
---

# Planner Strategy

Use this skill when the user asks for a daily summary, weekly report, monthly
report, trend, comparison, or target-gap analysis. Choose the narrowest report
tool that answers the question. Use `query_trend` for trend questions and
`compare_period` for period-to-period comparison.

Report tools must return backend facts. The reply should summarize those facts
without adding unsupported numbers.

# Must Ask When

- The requested period is ambiguous and cannot be mapped to today, this week,
  last 7 days, this month, or a clear comparison.
- The user asks for a custom range that the current tools cannot represent.

# Forbidden

- Do not invent trend conclusions.
- Do not treat no-record days as zero intake.
- Do not write intake records while answering report questions.
- Do not use current food database values to rewrite historical reports.

# Examples

User: 今天总结  
Plan: `generate_daily_report` period=today.

User: 最近 7 天蛋白质有没有变少  
Plan: `query_trend` metric=protein, period=last_7_days.

User: 这周比上周热量高了吗  
Plan: `compare_period` period=this_week, compare_to=last_week, metric=kcal.
