"""Eval runner: run cases.yaml against FakeStore + deterministic planner.

    python3 harness/eval/runner.py            # full suite
    python3 harness/eval/runner.py --smoke    # smoke subset
    python3 harness/eval/runner.py --case T05 # single case

Dialogue cases and focused contract checks share this entry point.
"""

from __future__ import annotations

import argparse
import contextlib
import io
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
from harness.eval.personal_cases import CHECKS  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calorie Agent eval runner")
    parser.add_argument("--smoke", action="store_true", help="only smoke-flagged cases")
    parser.add_argument("--case", default="", help="case ID or comma-separated IDs")
    parser.add_argument("--real-model", action="store_true")
    args = parser.parse_args(argv)
    from harness.eval import conversation_cases
    conversation_cases.REAL = args.real_model
    CHECKS.update(conversation_cases.CHECKS)

    cases = _load_cases()
    if args.case:
        cases = [case for case in cases if case.get("id") in args.case.split(",")]
    elif args.smoke:
        cases = [case for case in cases if case.get("smoke")]
    if not cases:
        print("No cases selected; check the case ID or suite configuration.")
        return 2

    passed = 0
    failed = 0
    for case in cases:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ok, failures = _run_case(case)
        except Exception as exc:
            ok, failures = False, [f"{type(exc).__name__}: {exc}"]
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {case.get('id')} {case.get('name')}")
        for failure in failures:
            print(f"       - {failure}")
        if ok:
            passed += 1
        else:
            failed += 1
    if args.real_model:
        import json
        (_ROOT / "reports/conversation-real.json").write_text(json.dumps(conversation_cases.TRANSCRIPT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{passed} passed, {failed} failed (total {len(cases)})")
    return 1 if failed else 0


def _load_cases():
    return [{"id": key, "name": check.__doc__, "check": check} for key,check in CHECKS.items()]


def _run_case(case: dict[str, Any]) -> tuple[bool, list[str]]:
    if "check" in case:
        case["check"]()
        return True, []

if __name__ == "__main__":
    sys.exit(main())
