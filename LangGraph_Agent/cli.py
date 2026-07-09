from __future__ import annotations

import json
import argparse
from typing import Any

from .graph import run_langgraph_agent


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LangGraph Agent V0 CLI")
    parser.add_argument("--planner", choices=["deterministic", "llm"], default="deterministic")
    parser.add_argument("text", nargs="*")
    args = parser.parse_args(list(argv or []))

    if args.text:
        texts = [" ".join(args.text)]
    else:
        texts = []
        print("LangGraph Agent V0 CLI. 输入空行退出。")
        while True:
            try:
                text = input("> ").strip()
            except EOFError:
                break
            if not text:
                break
            texts.append(text)

    for text in texts:
        result = run_langgraph_agent({"text": text}, settings={"planner_backend": args.planner})
        state = result.get("trace") if isinstance(result.get("trace"), dict) else {}
        _print_state(state)
    return 0


def _print_state(state: dict[str, Any]) -> None:
    print("trace_id:", state.get("trace_id", ""))
    print("normalized_text:", state.get("normalized_text", ""))
    print("plan:")
    print(json.dumps(state.get("plan", {}), ensure_ascii=False, indent=2))
    print("policy:")
    print(json.dumps(state.get("policy_decision", {}), ensure_ascii=False, indent=2))
    print("tool_results:")
    print(json.dumps(state.get("tool_results", []), ensure_ascii=False, indent=2))
    print("reply:", state.get("final_reply", ""))
    print("guard:")
    print(json.dumps(state.get("guard_result", {}), ensure_ascii=False, indent=2))
    if state.get("errors"):
        print("errors:")
        print(json.dumps(state.get("errors", []), ensure_ascii=False, indent=2))
    print("trace_written:", state.get("trace_written", False))
