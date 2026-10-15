"""Eval runner: run cases.yaml against FakeStore + deterministic planner.

    python3 harness/eval/runner.py            # full suite
    python3 harness/eval/runner.py --smoke    # smoke subset
    python3 harness/eval/runner.py --case T05 # single case

Assertions lock invariants (tool status sequences, key reply fragments, write
counts, totals self-consistency), never concrete nutrition numbers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402

from agent import FakeStore, run  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.storage import boohee_client  # noqa: E402
from agent.storage.timeutil import round_number  # noqa: E402

EVAL_CONFIG = Config(
    preset="shadow_fakewrite",
    planner="deterministic",
    model="",
    allowed_tools=("record_food", "query_today", "query_food_nutrition", "undo_last", "query_knowledge", "query_history"),
    timezone_offset_hours=8.0,
)

_MOCK_BOOHEE_RESPONSE = {
    "code": 0,
    "data": {
        "foods": [
            {"code": "mock_quinoa_rice", "name": "藜麦饭", "calories": 120, "protein": 4.4, "fat": 2.0, "carbohydrate": 20.5},
            {"code": "mock_quinoa", "name": "藜麦（生）", "calories": 357, "protein": 14.0, "fat": 6.0, "carbohydrate": 57.8},
        ]
    },
}


def _set_mock_boohee(enabled: bool) -> None:
    boohee_client.set_transport((lambda url, headers: _MOCK_BOOHEE_RESPONSE) if enabled else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calorie Agent eval runner")
    parser.add_argument("--smoke", action="store_true", help="only smoke-flagged cases")
    parser.add_argument("--case", default="", help="run one case by id")
    args = parser.parse_args(argv)

    cases = _load_cases()
    if args.case:
        cases = [case for case in cases if case.get("id") == args.case]
    elif args.smoke:
        cases = [case for case in cases if case.get("smoke")]

    passed = 0
    failed = 0
    for case in cases:
        ok, failures = _run_case(case)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {case.get('id')} {case.get('name')}")
        for failure in failures:
            print(f"       - {failure}")
        if ok:
            passed += 1
        else:
            failed += 1
    print(f"\n{passed} passed, {failed} failed (total {len(cases)})")
    return 1 if failed else 0


def _load_cases() -> list[dict[str, Any]]:
    path = Path(__file__).resolve().parent / "cases.yaml"
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return [case for case in data.get("cases", []) if isinstance(case, dict)]


def _run_case(case: dict[str, Any]) -> tuple[bool, list[str]]:
    seed = []
    for row in case.get("intake_seed") or []:
        seed.append(
            {
                "date": str(row.get("date") or ""),
                "food_name": str(row.get("food_name") or ""),
                "grams": float(row.get("grams") or 0),
                "carbs": float(row.get("carbs") or 0),
                "protein": float(row.get("protein") or 0),
                "fat": float(row.get("fat") or 0),
                "kcal": float(row.get("kcal") or 0),
            }
        )
    store = FakeStore(intake_seed=seed or None)
    failures: list[str] = []
    user_id = case.get("id", "eval") + "_user"
    _set_mock_boohee(bool(case.get("mock_boohee")))
    try:
        for index, step in enumerate(case.get("steps", []), start=1):
            text = str(step.get("text") or "")
            expect = step.get("expect") or {}
            message = {
                "message_id": str(step.get("msg_id") or f"{case.get('id')}-step{index}"),
                "user_id": user_id,
                "chat_id": case.get("id", "eval") + "_chat",
                "text": text,
            }
            result = run(message, store=store, config=EVAL_CONFIG)
            _check(expect, result, store, failures, step_label=f"{case.get('id')} step{index}", user_id=user_id)
    finally:
        _set_mock_boohee(False)
    return not failures, failures


def _check(
    expect: dict[str, Any],
    result: dict[str, Any],
    store: FakeStore,
    failures: list[str],
    step_label: str,
    user_id: str,
) -> None:
    def fail(message: str) -> None:
        failures.append(f"{step_label}: {message}")

    if expect.get("status") and result.get("status") != expect["status"]:
        fail(f"status={result.get('status')} expected {expect['status']} errors={result.get('errors')}")

    actual_statuses = [[item.get("tool"), item.get("status")] for item in result.get("tool_results", [])]
    expected_statuses = expect.get("tool_statuses")
    if expected_statuses is not None and actual_statuses != [list(pair) for pair in expected_statuses]:
        fail(f"tool_statuses={actual_statuses} expected {expected_statuses}")

    reply = str(result.get("reply") or "")
    for fragment in expect.get("reply_contains", []):
        if fragment not in reply:
            fail(f"reply missing fragment {fragment!r}; reply={reply!r}")
    for fragment in expect.get("reply_not_contains", []):
        if fragment in reply:
            fail(f"reply should not contain {fragment!r}; reply={reply!r}")

    if result.get("reply_source") == "safe_fallback" and not expect.get("allow_safe_fallback"):
        guard = result.get("guard_result", {})
        if not guard.get("passed"):
            fail(f"reply_guard blocked the reply: violations={guard.get('violations')} reply={reply!r}")

    write_count = _count_writes(result)
    if expect.get("write_count") is not None and write_count != expect["write_count"]:
        fail(f"write_count={write_count} expected {expect['write_count']}")

    if expect.get("store_rows") is not None:
        valid_rows = len([row for row in store.intake if row["status"] == "valid"])
        all_rows = len(store.intake)
        # store_rows asserts the number of rows ever written (soft-deleted rows included)
        if all_rows != expect["store_rows"]:
            fail(f"store_rows={all_rows} expected {expect['store_rows']} (valid now: {valid_rows})")

    if expect.get("totals_consistent"):
        _check_totals_consistent(result, store, fail, user_id)


def _count_writes(result: dict[str, Any]) -> int:
    count = 0
    for item in result.get("tool_results", []):
        data = item.get("data") if isinstance(item, dict) and isinstance(item.get("data"), dict) else {}
        write = data.get("intake_write_result") if isinstance(data.get("intake_write_result"), dict) else {}
        count += int(write.get("created_count") or 0)
    return count


def _check_totals_consistent(result: dict[str, Any], store: FakeStore, fail: Any, user_id: str) -> None:
    """Self-consistency: today totals must equal the sum of recorded facts."""
    fact_totals = {"kcal": 0.0, "carbs": 0.0, "protein": 0.0, "fat": 0.0}
    for item in result.get("tool_results", []):
        data = item.get("data") if isinstance(item, dict) and isinstance(item.get("data"), dict) else {}
        nutrition = data.get("nutrition") if isinstance(data.get("nutrition"), dict) else {}
        for key in fact_totals:
            fact_totals[key] = round_number(fact_totals[key] + float(nutrition.get(key) or 0))
    summary = store.today_summary(user_id)
    for key, value in fact_totals.items():
        if abs(float(summary["totals"].get(key) or 0) - value) > 0.05:
            fail(f"totals inconsistent for {key}: store={summary['totals'].get(key)} facts_sum={value}")


if __name__ == "__main__":
    sys.exit(main())
