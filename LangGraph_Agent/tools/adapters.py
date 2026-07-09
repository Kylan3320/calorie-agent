from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from calorie_agent.agent.schemas import AgentPlan as RuntimeAgentPlan
from calorie_agent.agent.tools import execute_agent_plan

from ..schemas import AgentPlan as LangGraphPlan


DEFAULT_V43_SINGLE_WRITER_TRACE_PATH = (
    Path(__file__).resolve().parents[4] / "reports" / "langgraph_agent_v43_single_writer_dry_run.jsonl"
)

V43_WRITE_EXECUTOR_TOOLS = {"record_food", "query_today"}
V43_WRITE_TOOLS = {"record_food"}


def build_single_writer_executor_contract() -> dict[str, Any]:
    return {
        "version": "v4.3",
        "purpose": "prepare LangGraph primary write by routing through the existing safe agent tool executor",
        "entrypoint": "calorie_agent.agent.tools.execute_agent_plan",
        "allowed_tools": sorted(V43_WRITE_EXECUTOR_TOOLS),
        "write_tools": sorted(V43_WRITE_TOOLS),
        "forbidden_tools": [
            "undo_intake",
            "clear_daily_intake",
            "change_standard",
            "delete_memory",
            "family_member_write",
        ],
        "idempotency": {
            "source_message_id": "message.message_id",
            "executor_action_message_id": "<message_id>:agent:<action_index>",
            "action_id": "LangGraph action_id is preserved when converted to runtime AgentAction",
            "duplicate_policy": "same source message_id + action index + food + grams must not create a second row",
        },
        "single_writer": {
            "langgraph_primary_may_execute_write": "only when current_runtime_executed is false",
            "if_current_runtime_already_executed": "block LangGraph write and fallback to current result",
            "same_request_double_write_allowed": False,
        },
        "pending_and_confirmation": {
            "pending_statuses": ["pending_created", "default_grams_suggested", "missing_grams", "not_found"],
            "confirmation_statuses": ["needs_confirmation"],
            "reply_rule": "planner or candidate may not claim a pending/confirmation write as recorded",
        },
        "stage_boundary": {
            "online_primary_enabled": False,
            "dry_run_only": True,
            "direct_db_write_allowed": False,
            "destructive_or_family_write_allowed": False,
        },
    }


def run_single_writer_adapter_dry_run(
    text: str,
    *,
    message: dict[str, str],
    plan: dict[str, Any],
    legacy: "SingleWriterDryRunLegacy | None" = None,
    current_runtime_executed: bool = False,
) -> dict[str, Any]:
    legacy = legacy or SingleWriterDryRunLegacy()
    langgraph_plan = LangGraphPlan.from_dict(plan)
    write_requested = any(action.tool in V43_WRITE_TOOLS for action in langgraph_plan.actions)
    blocked_reason = _blocked_reason(langgraph_plan)
    if current_runtime_executed and write_requested:
        blocked_reason = "single_writer_current_runtime_already_executed"
    if blocked_reason:
        return _blocked_result(text, message, langgraph_plan, blocked_reason, legacy)

    runtime_plan = _runtime_plan_from_langgraph(langgraph_plan)
    results = [item.to_dict() for item in execute_agent_plan(message, runtime_plan, legacy=legacy)]
    return _result_record(
        text=text,
        message=message,
        plan=langgraph_plan,
        results=results,
        legacy=legacy,
        current_runtime_executed=current_runtime_executed,
        blocked_reason="",
    )


def run_single_writer_dry_run_harness(
    *,
    trace_path: str | Path | None = DEFAULT_V43_SINGLE_WRITER_TRACE_PATH,
    top_n: int = 8,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for case in _cases():
        legacy = SingleWriterDryRunLegacy()
        runs = int(case.get("runs") or 1)
        for run_index in range(runs):
            record = run_single_writer_adapter_dry_run(
                str(case["text"]),
                message={
                    "message_id": str(case.get("message_id") or case["case_id"]),
                    "user_id": "v43_user",
                    "chat_id": f"{case['case_id']}_chat",
                    "text": str(case["text"]),
                },
                plan=case["plan"],
                legacy=legacy,
                current_runtime_executed=bool(case.get("current_runtime_executed")),
            )
            record["case_id"] = str(case["case_id"])
            record["name"] = str(case["name"])
            record["run_index"] = run_index + 1
            records.append(record)

    report = summarize_single_writer_records(records, top_n=top_n)
    if trace_path:
        _write_jsonl(records, Path(trace_path))
    return report


def summarize_single_writer_records(records: list[dict[str, Any]], *, top_n: int = 8) -> dict[str, Any]:
    duplicate_write_count = 0
    pending_claimed_recorded_count = 0
    single_writer_violation_count = 0
    langgraph_direct_db_write_count = 0
    review_cases: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}

    for record in records:
        status = str(record.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        duplicate_write = bool(record.get("duplicate_write"))
        pending_claimed = bool(record.get("pending_claimed_recorded"))
        single_writer_violation = bool(record.get("single_writer_violation"))
        direct_db_write = int(record.get("langgraph_direct_db_write_count") or 0)

        duplicate_write_count += 1 if duplicate_write else 0
        pending_claimed_recorded_count += 1 if pending_claimed else 0
        single_writer_violation_count += 1 if single_writer_violation else 0
        langgraph_direct_db_write_count += direct_db_write
        if duplicate_write or pending_claimed or single_writer_violation or direct_db_write or status.startswith("blocked_"):
            review_cases.append(_review_case(record))

    return {
        "version": "v4.3",
        "total_runs": len(records),
        "executor_contract": build_single_writer_executor_contract(),
        "existing_tool_executor_call_count": sum(int(record.get("existing_tool_executor_call_count") or 0) for record in records),
        "langgraph_write_count": sum(int(record.get("langgraph_write_count") or 0) for record in records),
        "duplicate_skip_count": sum(int(record.get("duplicate_skip_count") or 0) for record in records),
        "duplicate_write_count": duplicate_write_count,
        "pending_count": sum(int(record.get("pending_count") or 0) for record in records),
        "confirmation_count": sum(int(record.get("confirmation_count") or 0) for record in records),
        "pending_claimed_recorded_count": pending_claimed_recorded_count,
        "langgraph_direct_db_write_count": langgraph_direct_db_write_count,
        "single_writer_violation_count": single_writer_violation_count,
        "current_already_executed_block_count": len(
            [record for record in records if record.get("blocked_reason") == "single_writer_current_runtime_already_executed"]
        ),
        "unsupported_scope_block_count": len([record for record in records if str(record.get("status") or "") == "blocked_unsupported_scope"]),
        "status_counts": status_counts,
        "top_review_cases": review_cases[:top_n],
        "records": records,
    }


def evaluate_single_writer_report(report: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    notes: list[str] = []
    if int(report.get("total_runs") or 0) <= 0:
        failures.append("total_runs must be > 0")
    if int(report.get("existing_tool_executor_call_count") or 0) <= 0:
        failures.append("existing_tool_executor_call_count must be > 0")
    if int(report.get("duplicate_write_count") or 0) != 0:
        failures.append("duplicate_write_count must be 0")
    if int(report.get("pending_claimed_recorded_count") or 0) != 0:
        failures.append("pending_claimed_recorded_count must be 0")
    if int(report.get("langgraph_direct_db_write_count") or 0) != 0:
        failures.append("langgraph_direct_db_write_count must be 0")
    if int(report.get("single_writer_violation_count") or 0) != 0:
        failures.append("single_writer_violation_count must be 0")
    if int(report.get("current_already_executed_block_count") or 0) <= 0:
        failures.append("current_already_executed_block_count must be > 0")
    if int(report.get("duplicate_skip_count") or 0) <= 0:
        failures.append("duplicate_skip_count must be > 0")
    if int(report.get("pending_count") or 0) <= 0:
        failures.append("pending_count must be > 0")
    if int(report.get("confirmation_count") or 0) <= 0:
        failures.append("confirmation_count must be > 0")
    notes.append("V4.3 is dry-run only and does not enable online LangGraph primary write")
    return {"passed": not failures, "decision": "ready_for_v4_4_shadow_parity" if not failures else "no_go", "failures": failures, "notes": notes}


def _runtime_plan_from_langgraph(plan: LangGraphPlan) -> RuntimeAgentPlan:
    actions = []
    for action in plan.actions:
        actions.append(
            {
                "tool": action.tool,
                "params": dict(action.parameters),
                "target": {},
                "action_id": action.action_id,
                "reason": action.reason,
                "confidence": plan.confidence,
            }
        )
    return RuntimeAgentPlan.from_dict(
        {
            "goal": plan.goal,
            "actions": actions,
            "needs_confirmation": plan.needs_confirmation,
            "confidence": plan.confidence,
        }
    )


def _blocked_reason(plan: LangGraphPlan) -> str:
    for action in plan.actions:
        if action.tool not in V43_WRITE_EXECUTOR_TOOLS:
            return f"unsupported_tool:{action.tool}"
        if action.safety in {"destructive", "permission_sensitive"}:
            return "unsupported_risk_scope"
        if _has_forbidden_write_flags(action.parameters):
            return "forbidden_write_flags"
    return ""


def _blocked_result(
    text: str,
    message: dict[str, str],
    plan: LangGraphPlan,
    blocked_reason: str,
    legacy: "SingleWriterDryRunLegacy",
) -> dict[str, Any]:
    return _result_record(
        text=text,
        message=message,
        plan=plan,
        results=[],
        legacy=legacy,
        current_runtime_executed=blocked_reason == "single_writer_current_runtime_already_executed",
        blocked_reason=blocked_reason,
    )


def _result_record(
    *,
    text: str,
    message: dict[str, str],
    plan: LangGraphPlan,
    results: list[dict[str, Any]],
    legacy: "SingleWriterDryRunLegacy",
    current_runtime_executed: bool,
    blocked_reason: str,
) -> dict[str, Any]:
    write_count = _write_count(results)
    duplicate_skip_count = _status_count(results, {"duplicate_skipped"})
    pending_count = _status_count(results, {"pending_created", "default_grams_suggested", "missing_grams", "not_found"})
    confirmation_count = _status_count(results, {"needs_confirmation"})
    duplicate_write = duplicate_skip_count > 0 and write_count > 0
    return {
        "schema_version": "langgraph_single_writer_dry_run.v4.3",
        "message_id": str(message.get("message_id") or ""),
        "text": text,
        "plan": plan.to_dict(),
        "status": _status(results, blocked_reason),
        "blocked_reason": blocked_reason,
        "current_runtime_executed": current_runtime_executed,
        "langgraph_write_attempted": any(action.tool in V43_WRITE_TOOLS for action in plan.actions),
        "existing_tool_executor_call_count": 0 if blocked_reason else 1,
        "tool_results": results,
        "tool_statuses": [str(item.get("status") or "") for item in results],
        "langgraph_write_count": write_count,
        "duplicate_skip_count": duplicate_skip_count,
        "duplicate_write": duplicate_write,
        "pending_count": pending_count,
        "confirmation_count": confirmation_count,
        "pending_claimed_recorded": False,
        "langgraph_direct_db_write_count": int(getattr(legacy, "direct_db_write_count", 0) or 0),
        "single_writer_violation": current_runtime_executed and write_count > 0,
        "idempotency_keys": list(getattr(legacy, "idempotency_keys", [])),
        "executor_message_ids": list(getattr(legacy, "executor_message_ids", [])),
    }


def _status(results: list[dict[str, Any]], blocked_reason: str) -> str:
    if blocked_reason:
        if blocked_reason == "single_writer_current_runtime_already_executed":
            return "blocked_single_writer"
        return "blocked_unsupported_scope"
    statuses = {str(item.get("status") or "") for item in results}
    if not statuses:
        return "ok_noop"
    if "error" in statuses:
        return "fallback_tool_error"
    if "needs_confirmation" in statuses:
        return "needs_confirmation"
    if statuses & {"pending_created", "default_grams_suggested", "missing_grams", "not_found"}:
        return "pending"
    if all(status in {"ok", "duplicate_skipped"} for status in statuses):
        return "ok"
    return "review"


def _cases() -> list[dict[str, Any]]:
    return [
        {
            "case_id": "LGV43-SW-01",
            "name": "primary dry-run record",
            "text": "record egg 100g",
            "plan": _plan("record_intake", [_record_action("a1", "egg", 100)]),
        },
        {
            "case_id": "LGV43-SW-02",
            "name": "primary dry-run record then query",
            "text": "record egg 100g then query today",
            "plan": _plan("record_and_query", [_record_action("a1", "egg", 100), _query_today("a2")]),
        },
        {
            "case_id": "LGV43-SW-03",
            "name": "duplicate retry",
            "text": "record egg 100g",
            "message_id": "LGV43-SW-03-retry",
            "runs": 2,
            "plan": _plan("record_intake", [_record_action("a1", "egg", 100)]),
        },
        {
            "case_id": "LGV43-SW-04",
            "name": "current already executed blocks langgraph write",
            "text": "record egg 100g",
            "current_runtime_executed": True,
            "plan": _plan("record_intake", [_record_action("a1", "egg", 100)]),
        },
        {
            "case_id": "LGV43-SW-05",
            "name": "missing grams becomes pending",
            "text": "record egg",
            "plan": _plan("record_intake", [_record_action("a1", "egg", None)]),
        },
        {
            "case_id": "LGV43-SW-06",
            "name": "ambiguous food needs confirmation",
            "text": "record ri 100g",
            "plan": _plan("record_intake", [_record_action("a1", "ri", 100)]),
        },
        {
            "case_id": "LGV43-SW-07",
            "name": "unsupported destructive remains blocked",
            "text": "undo last",
            "plan": _plan(
                "semantic_undo",
                [{"action_id": "a1", "tool": "undo_intake", "parameters": {"mode": "previous"}, "safety": "destructive"}],
            ),
        },
    ]


def _plan(goal: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"goal": goal, "actions": actions, "used_context": ["v4.3_single_writer"], "confidence": 0.99}


def _record_action(action_id: str, food_query: str, grams: float | None) -> dict[str, Any]:
    params: dict[str, Any] = {"food_query": food_query, "meal_type": "unknown"}
    if grams is not None:
        params["grams"] = grams
    return {"action_id": action_id, "tool": "record_food", "parameters": params, "reason": "v4.3 dry-run", "safety": "write"}


def _query_today(action_id: str) -> dict[str, Any]:
    return {"action_id": action_id, "tool": "query_today", "parameters": {}, "reason": "v4.3 dry-run", "safety": "read_only"}


def _write_count(results: list[dict[str, Any]]) -> int:
    count = 0
    for item in results:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        source = data.get("result") if isinstance(data.get("result"), dict) else data
        write = source.get("intake_write_result") if isinstance(source.get("intake_write_result"), dict) else {}
        count += int(write.get("created_count") or 0)
    return count


def _status_count(results: list[dict[str, Any]], statuses: set[str]) -> int:
    return len([item for item in results if str(item.get("status") or "") in statuses])


def _has_forbidden_write_flags(params: dict[str, Any]) -> bool:
    forbidden = {"delete", "undo", "hard_delete", "update", "upsert", "profile", "member_id", "family_member", "external_source"}
    return any(bool(params.get(key)) for key in forbidden)


def _review_case(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": str(record.get("case_id") or ""),
        "name": str(record.get("name") or ""),
        "status": str(record.get("status") or ""),
        "blocked_reason": str(record.get("blocked_reason") or ""),
        "langgraph_write_count": int(record.get("langgraph_write_count") or 0),
        "duplicate_skip_count": int(record.get("duplicate_skip_count") or 0),
        "single_writer_violation": bool(record.get("single_writer_violation")),
        "langgraph_direct_db_write_count": int(record.get("langgraph_direct_db_write_count") or 0),
    }


def _write_jsonl(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


class SingleWriterDryRunLegacy:
    def __init__(self) -> None:
        self.foods = [
            {"food_id": "food_egg", "record_id": "food_rec_egg", "name": "egg", "aliases": ["鸡蛋"], "carbs_per_100g": 1.1, "protein_per_100g": 12.6, "fat_per_100g": 9.5},
            {"food_id": "food_rice", "record_id": "food_rec_rice", "name": "rice", "aliases": ["米饭"], "carbs_per_100g": 25.9, "protein_per_100g": 2.6, "fat_per_100g": 0.3},
            {"food_id": "food_rice_cake", "record_id": "food_rec_rice_cake", "name": "ricecake", "aliases": [], "carbs_per_100g": 24.0, "protein_per_100g": 1.8, "fat_per_100g": 0.2},
        ]
        self.records: list[dict[str, Any]] = []
        self.seen_write_keys: set[str] = set()
        self.idempotency_keys: list[str] = []
        self.executor_message_ids: list[str] = []
        self.direct_db_write_count = 0

    def _settings(self) -> dict[str, str]:
        values = {
            "bitable_app_token": "local",
            "bitable_intake_table_id": "intake",
            "bitable_standard_table_id": "",
            "bitable_memory_table_id": "",
            "personal_memory_enabled": "false",
            "auto_alias_learning_enabled": "false",
        }
        for key in ("date", "user_id", "message_id", "meal_type", "food_name", "grams", "carbs", "protein", "fat", "kcal", "status", "created_at"):
            values[f"bitable_field_intake_{key}"] = key
        return values

    def _load_food_database(self, _message: dict[str, str] | None = None) -> list[dict[str, Any]]:
        return list(self.foods)

    def _load_today_intake_summary(self, _message: dict[str, str]) -> dict[str, Any]:
        totals = {"kcal": 0.0, "carbs": 0.0, "protein": 0.0, "fat": 0.0}
        for item in self.records:
            for key in totals:
                totals[key] = round(totals[key] + float(item.get(key) or 0), 1)
        return {"count": len(self.records), "items": list(self.records), "totals": totals}

    def _load_daily_standard(self, _message: dict[str, str]) -> dict[str, float]:
        return {"kcal": 1800.0, "carbs": 200.0, "protein": 100.0, "fat": 60.0}

    def _default_daily_standard(self) -> dict[str, float]:
        return {"kcal": 1800.0, "carbs": 200.0, "protein": 100.0, "fat": 60.0}

    def _format_today_query_reply(self, summary: dict[str, Any], _standard: dict[str, Any]) -> str:
        totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
        return f"dry-run today count {summary.get('count', 0)}, kcal {totals.get('kcal', 0):g}."

    def _today_string(self) -> str:
        return "2026-07-04"

    def _load_active_pending_task(self, _message: dict[str, str]) -> dict[str, Any]:
        return {}

    def _now_millis(self) -> int:
        return 1783171269000

    def _stable_uuid4_from_text(self, text: str) -> str:
        return str(uuid5(NAMESPACE_URL, text))

    def _intake_field(self, settings: dict[str, str], key: str) -> str:
        return settings[f"bitable_field_intake_{key}"]

    def _unique_non_empty(self, values: list[str]) -> list[str]:
        return [value for index, value in enumerate(values) if value and value not in values[:index]]

    def _list_bitable_records(self, _app_token: str, _table_id: str, *, field_names: list[str] | None = None) -> list[dict[str, Any]]:
        return []

    def _extract_text(self, value: Any) -> str:
        return "" if value is None else str(value).strip()

    def _extract_number(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _is_valid_intake_status(self, value: Any) -> bool:
        return str(value or "").lower() in {"valid", "active", "normal"}

    def _create_pending_task_for_missing(
        self,
        message: dict[str, str],
        _parse_result: dict[str, Any],
        missing: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "status": "created",
            "message_id": message.get("message_id", ""),
            "missing": missing,
            "food_name": missing.get("name", ""),
            "grams": missing.get("grams"),
            "dry_run": True,
            "real_write": False,
        }

    def _handle_add_food_command(self, message: dict[str, str], parse_result: dict[str, Any]) -> dict[str, Any]:
        self.executor_message_ids.append(str(message.get("message_id") or ""))
        foods = parse_result.get("foods") if isinstance(parse_result.get("foods"), list) else []
        created_count = 0
        items: list[dict[str, Any]] = []
        for index, item in enumerate(foods):
            if not isinstance(item, dict):
                continue
            food_name = str(item.get("name") or "")
            grams = self._extract_number(item.get("grams")) or 0.0
            key = f"{message.get('message_id', '')}:{index}:{food_name}:{grams:g}"
            self.idempotency_keys.append(key)
            if key in self.seen_write_keys:
                continue
            self.seen_write_keys.add(key)
            nutrition = _nutrition_for(food_name, grams)
            record = {"food_name": food_name, "grams": grams, **nutrition}
            self.records.append(record)
            items.append(record)
            created_count += 1
        status = "duplicate_skipped" if foods and created_count == 0 else "ok"
        return {
            "status": status,
            "lookup_result": {"items": items, "missing": [], "totals": {"kcal": sum(float(item.get("kcal") or 0) for item in items)}},
            "intake_write_result": {"status": "created" if created_count else "duplicate_skipped", "created_count": created_count},
            "reply": "dry-run recorded" if created_count else "dry-run duplicate skipped",
            "dry_run": True,
            "real_write": False,
        }


def _nutrition_for(food_name: str, grams: float) -> dict[str, float]:
    per_100g = {
        "egg": {"carbs": 1.1, "protein": 12.6, "fat": 9.5},
        "rice": {"carbs": 25.9, "protein": 2.6, "fat": 0.3},
        "ricecake": {"carbs": 24.0, "protein": 1.8, "fat": 0.2},
    }.get(food_name, {"carbs": 0.0, "protein": 0.0, "fat": 0.0})
    multiplier = grams / 100.0
    carbs = round(float(per_100g["carbs"]) * multiplier, 1)
    protein = round(float(per_100g["protein"]) * multiplier, 1)
    fat = round(float(per_100g["fat"]) * multiplier, 1)
    kcal = round(carbs * 4 + protein * 4 + fat * 9, 1)
    return {"kcal": kcal, "carbs": carbs, "protein": protein, "fat": fat}
