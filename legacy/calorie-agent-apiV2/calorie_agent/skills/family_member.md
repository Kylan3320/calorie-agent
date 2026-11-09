---
name: family_member
description: Handle family member references, member binding, and permission-sensitive member actions.
triggers:
  - 给妈妈记录米饭100g
  - 查爸爸今天吃了什么
  - 添加妈妈为家庭成员
  - 修改成员权限
allowed_tools:
  - resolve_member
  - record_food
  - query_today
  - update_member_relation
required_context:
  - actor_user_id
  - member_relations
  - permissions_summary
  - chat_scope
---

# Planner Strategy

Use this skill when the user mentions a family member or asks to manage member
relationships. Resolve the member first, then plan the underlying action only
when the relation and permissions are clear. Member actions should remain
explicit; default food records still target the current user.

# Must Ask When

- The mentioned member is not bound.
- The member name matches more than one relation.
- The user does not have clear read or write permission for the target member.
- The message is in a group chat and the target member is not explicit.

# Forbidden

- Do not guess unbound members.
- Do not query or write another user's data without permission.
- Do not expose personal member details in group chat by default.
- Do not let a family-member skill bypass Tool Executor permission checks.

# Examples

User: 给妈妈记录米饭100g  
Plan: `resolve_member` member_name=妈妈, then `record_food` rice 100g for the resolved target if permitted.

User: 查爸爸今天吃了什么  
Plan: `resolve_member` member_name=爸爸, then `query_today` for the resolved target if permitted.

User: 添加妈妈为家庭成员  
Plan: `update_member_relation` action=create, member_name=妈妈.
