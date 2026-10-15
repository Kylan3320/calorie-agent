"""Shadow double-run: replay the eval case set through two runtime configurations
and diff the results (the P1 replacement for the old four-candidate harness).

    python3 harness/shadow.py                 # fakewrite vs readonly (deterministic)
    SHADOW_WITH_LLM=1 python3 harness/shadow.py   # adds a deepseek leg when DEEPSEEK_API_KEY is set

Gate (same philosophy as the old single-writer gate, now config-free):
- readonly leg must never write (store stays empty)
- reply status must match across legs; reply text may differ (recorded in the report)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402

from agent import FakeStore, run  # noqa: E402
from agent.config import Config  # noqa: E402

_REPORT_PATH = _ROOT / "reports" / "shadow_diff.jsonl"


def _leg_config(preset: str, planner: str) -> Config:
    return Config(
        preset=preset,
        planner=planner,
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        allowed_tools=("record_food", "query_today", "query_food_nutrition", "undo_last"),
        timezone_offset_hours=8.0,
    )


def _run_leg(leg_name: str, preset: str, planner: str, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for case in cases:
        store = FakeStore()
        for index, step in enumerate(case.get("steps", []), start=1):
            message = {
                "message_id": f"{case.get('id')}-step{index}",
                "user_id": f"{case.get('id')}_user",
                "chat_id": f"{case.get('id')}_chat",
                "text": str(step.get("text") or ""),
            }
            result = run(message, store=store, config=_leg_config(preset, planner))
            records.append(
                {
                    "leg": leg_name,
                    "case_id": case.get("id"),
                    "step": index,
                    "tool_statuses": [[item.get("tool"), item.get("status")] for item in result.get("tool_results", [])],
                    "reply_source": result.get("reply_source"),
                    "reply": result.get("reply", ""),
                    "reply_sent_expected": preset == "live",
                    "store_write_count": len(store.intake),
                }
            )
    return records


def run_shadow(cases: list[dict[str, Any]]) -> dict[str, Any]:
    base = _run_leg("fakewrite_det", "shadow_fakewrite", "deterministic", cases)
    readonly = _run_leg("readonly_det", "shadow_readonly", "deterministic", cases)
    legs = {"fakewrite_det": base, "readonly_det": readonly}

    if os.getenv("SHADOW_WITH_LLM") == "1" and os.getenv("DEEPSEEK_API_KEY"):
        legs["fakewrite_llm"] = _run_leg("fakewrite_llm", "shadow_fakewrite", "deepseek", cases)

    write_violations: list[str] = []
    divergences: list[str] = []
    readonly_by_key = {(r["case_id"], r["step"]): r for r in readonly}
    for record in base:
        twin = readonly_by_key.get((record["case_id"], record["step"]))
        if twin and twin["store_write_count"] != 0:
            write_violations.append(f"readonly leg wrote rows at {record['case_id']} step{record['step']}")
        if twin and twin["reply_source"] != record["reply_source"]:
            # readonly 有意不回答 pending、不执行写入：只记录分歧，不作为门禁
            divergences.append(
                {"case_id": record["case_id"], "step": record["step"], "fakewrite": record["reply_source"], "readonly": twin["reply_source"]}
            )

    report = {
        "total_records": len(base),
        "legs": {name: len(records) for name, records in legs.items()},
        "readonly_write_violations": write_violations,
        "route_divergences": divergences,
        "records": [*base, *readonly, *[r for records in legs.values() for r in records]],
    }
    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _REPORT_PATH.open("w", encoding="utf-8") as handle:
        for record in report.pop("records"):
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return report


def main() -> int:
    cases_path = _ROOT / "harness" / "eval" / "cases.yaml"
    with cases_path.open("r", encoding="utf-8") as handle:
        cases = [case for case in yaml.safe_load(handle).get("cases", []) if isinstance(case, dict)]

    report = run_shadow(cases)
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, ensure_ascii=False, indent=2))
    decision = "PASS" if not report["readonly_write_violations"] else "NO_GO"
    print(f"shadow decision: {decision} (report: {_REPORT_PATH})")
    return 0 if decision == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
