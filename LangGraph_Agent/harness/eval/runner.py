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
    parser.add_argument("--production-live-plan", type=Path)
    parser.add_argument("--production-live-stage", choices=["business", "archive", "cleanup", "report"])
    args = parser.parse_args(argv)
    if args.production_live_plan:
        if not args.production_live_stage:
            parser.error("--production-live-plan requires --production-live-stage")
        from harness.eval.production_live import main as live_main
        return live_main(["--plan", str(args.production_live_plan), "--stage", args.production_live_stage])
    if args.production_live_stage:
        parser.error("--production-live-stage requires --production-live-plan")
    from harness.eval import conversation_cases
    conversation_cases.REAL = args.real_model
    CHECKS.update(conversation_cases.CHECKS)
    from harness.eval.cos_probe_cases import CHECKS as COS_PROBE_CHECKS
    CHECKS.pop("C02", None)
    CHECKS.update({key: check for key, check in COS_PROBE_CHECKS.items()
                   if key != "C02" or key in args.case.split(",")})
    from harness.eval.cos_pipeline_cases import CHECKS as COS_PIPELINE_CHECKS
    CHECKS.update(COS_PIPELINE_CHECKS)
    from harness.eval.cos_pipeline_live_cases import CHECKS as COS_PIPELINE_LIVE_CHECKS
    CHECKS.update(COS_PIPELINE_LIVE_CHECKS)

    from harness.eval.production_store_cases import CHECKS as STORE_CHECKS
    from harness.eval.production_archive_cases import CHECKS as ARCHIVE_CHECKS
    from harness.eval.production_pipeline_cases import CHECKS as PIPELINE_CHECKS
    from harness.eval.production_role_cases import CHECKS as ROLE_CHECKS
    from harness.eval.production_cutover_cases import CHECKS as CUTOVER_CHECKS
    CHECKS.update(STORE_CHECKS)
    CHECKS.update(ARCHIVE_CHECKS)
    CHECKS.update(PIPELINE_CHECKS)
    CHECKS.update(ROLE_CHECKS)
    CHECKS.update(CUTOVER_CHECKS)
    # The specified acceptance groups include the new production boundaries.
    # Each extra check runs once; explicit PS/QA/P IDs remain available for rechecks.
    additions = {"E01": list(STORE_CHECKS.values()), "E05": list(ARCHIVE_CHECKS.values())}
    if not args.real_model:
        for key, extra in additions.items():
            if key not in args.case.split(","):
                continue
            original = CHECKS[key]
            def grouped(original=original, extra=extra):
                original()
                for check in extra:
                    check()
            grouped.__doc__ = original.__doc__
            CHECKS[key] = grouped

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
