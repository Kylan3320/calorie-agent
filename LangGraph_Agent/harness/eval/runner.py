"""Select explicit offline cases, real-model conversation cases, or live stages."""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def load_checks():
    checks = {}
    for module in (
        "personal_cases", "conversation_cases", "latency_checks",
        "production_store_cases", "production_archive_cases", "production_pipeline_cases",
        "production_role_cases", "production_cutover_cases", "production_transport_cases",
        "release_cases", "runtime_cases",
    ):
        group = importlib.import_module("harness.eval." + module).CHECKS
        duplicates = checks.keys() & group.keys()
        if duplicates:
            raise ValueError(f"duplicate case IDs: {sorted(duplicates)}")
        checks.update(group)
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description="Calorie Agent eval runner")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--list", action="store_true", help="list available case IDs (default)")
    selection.add_argument("--case", help="comma-separated case IDs; each runs once")
    selection.add_argument("--all", action="store_true", help="run all offline cases")
    selection.add_argument("--production-live-plan", type=Path)
    parser.add_argument("--real-model", action="store_true", help="use DeepSeek for explicitly selected E cases")
    parser.add_argument("--production-live-stage", choices=["business", "archive", "cleanup", "report"])
    args = parser.parse_args(argv)
    if args.production_live_plan:
        if args.real_model or not args.production_live_stage:
            parser.error("--production-live-plan requires --production-live-stage and cannot use --real-model")
        from harness.eval.production_live import main as live_main
        return live_main(["--plan", str(args.production_live_plan), "--stage", args.production_live_stage])
    if args.production_live_stage:
        parser.error("--production-live-stage requires --production-live-plan")
    if args.real_model and not args.case:
        parser.error("--real-model requires explicit --case E IDs")

    checks = load_checks()
    if args.case is not None:
        requested = [part.strip() for part in args.case.split(",")]
        unknown = [key for key in requested if key not in checks]
        if unknown:
            parser.error("unknown case IDs: " + ", ".join(repr(key) for key in unknown))
        selected = list(dict.fromkeys(requested))
    elif args.all:
        selected = list(checks)
    else:
        for key, check in checks.items():
            print(f"{key}: {check.__doc__ or check.__name__}")
        return 0
    from harness.eval import conversation_cases
    if args.real_model and any(key not in conversation_cases.CHECKS for key in selected):
        parser.error("--real-model only supports E01-E06")
    previous_mode = conversation_cases.REAL
    conversation_cases.REAL = args.real_model
    conversation_cases.TRANSCRIPT.clear()
    failed = 0
    try:
        for key in selected:
            check = checks[key]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    check()
            except Exception as exc:
                failed += 1
                print(f"[FAIL] {key} {check.__doc__ or check.__name__}\n       - {type(exc).__name__}: {exc}")
            else:
                print(f"[PASS] {key} {check.__doc__ or check.__name__}")
        if args.real_model:
            path = ROOT / "reports" / "conversation-real.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(conversation_cases.TRANSCRIPT, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        conversation_cases.REAL = previous_mode
    print(f"\n{len(selected)-failed} passed, {failed} failed (total {len(selected)})")
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
