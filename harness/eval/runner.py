"""Select explicit offline cases, real-model conversation cases, or live stages."""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def load_checks():
    checks = {}
    for module in (
        "personal_cases", "conversation_cases", "latency_checks",
        "production_store_cases", "production_archive_cases", "production_pipeline_cases",
        "production_role_cases", "production_cutover_cases", "production_transport_cases",
        "release_cases", "runtime_cases", "context_cases", "receipt_cases", "receipt_runtime_cases", "model_transport_cases", "rpc_deadline_cases", "delivery_binding_cases", "finalization_cases", "completion_budget_cases", "action_note_cases", "task_evidence_cases", "task_domain_cases", "task_budget_cases", "menu_quality_cases", "preflight_cos_cases", "meal_completion_cases",
        "memory_cases", "memory_background_cases", "stage_a_cases",
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
    parser.add_argument("--output", type=Path, help="new real-model transcript path; existing files are refused")
    parser.add_argument("--production-live-stage", choices=["business", "archive", "cleanup", "report"])
    args = parser.parse_args(argv)
    if args.output is not None and not args.real_model:
        parser.error("--output requires --real-model")
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
    real_only = {key for key, check in checks.items() if getattr(check, "real_only", False)}
    if args.case is not None:
        requested = [part.strip() for part in args.case.split(",")]
        unknown = [key for key in requested if key not in checks]
        if unknown:
            parser.error("unknown case IDs: " + ", ".join(repr(key) for key in unknown))
        if not args.real_model and real_only.intersection(requested):
            parser.error("these cases require --real-model: " + ", ".join(sorted(real_only.intersection(requested))))
        selected = list(dict.fromkeys(requested))
    elif args.all:
        selected = [key for key in checks if key not in real_only]
    else:
        for key, check in checks.items():
            print(f"{key}: {check.__doc__ or check.__name__}")
        return 0
    from harness.eval import conversation_cases
    if args.real_model and any(key not in conversation_cases.CHECKS for key in selected):
        parser.error("--real-model only supports conversation acceptance cases (E series)")
    transcript = None
    if args.real_model:
        path = args.output if args.output is not None else ROOT / "reports" / (
            "conversation-real-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            transcript = path.open("x", encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot create transcript {path}: {exc}")
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
    finally:
        conversation_cases.REAL = previous_mode
        if transcript is not None:
            with transcript:
                json.dump(conversation_cases.TRANSCRIPT, transcript, ensure_ascii=False, indent=2)
            print(f"transcript: {path.resolve()}")
    print(f"\n{len(selected)-failed} passed, {failed} failed (total {len(selected)})")
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
