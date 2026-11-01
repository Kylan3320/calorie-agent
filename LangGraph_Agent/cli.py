"""Local CLI (FakeStore by default, no network):

    python3 cli.py 米饭200g
    python3 cli.py --planner deepseek 记录鸡蛋100g，查下今天还剩多少
    python3 cli.py            # interactive loop
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import uuid
from collections.abc import Iterator
from typing import Any

from agent import BitableStore, FakeStore, load_config, run
from agent.policy import resolve_preset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calorie Agent V4 local CLI")
    parser.add_argument("--planner", choices=["deterministic", "deepseek"], default=None)
    parser.add_argument("--preset", choices=["live", "shadow_fakewrite", "shadow_readonly"], default=None)
    parser.add_argument("text", nargs="*")
    args = parser.parse_args(argv)

    config = load_config()
    if args.planner:
        config = dataclasses.replace(config, planner=args.planner)
    if args.preset:
        config = dataclasses.replace(config, preset=args.preset)
    # CLI 默认走本地假存储 + 确定性规划，保证零配置可跑
    if config.preset == "live" and args.preset is None:
        config = dataclasses.replace(config, preset="shadow_fakewrite")
    if config.planner == "deepseek" and args.planner is None:
        config = dataclasses.replace(config, planner="deterministic")

    preset = resolve_preset(config.preset, config.allowed_tools)
    store = FakeStore() if preset.store_kind == "fake" else BitableStore(offset_hours=config.timezone_offset_hours)
    texts = [" ".join(args.text)] if args.text else _interactive()
    for text in texts:
        result = run({"message_id": f"cli-{uuid.uuid4().hex}", "user_id": "local_user", "chat_id": "cli", "text": text}, store=store, config=config)
        _print_result(result)
    return 0


def _interactive() -> Iterator[str]:
    print("Calorie Agent V4 CLI（空行退出）")
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            break
        if not text:
            break
        yield text


def _print_result(result: dict[str, Any]) -> None:
    print("plan:      ", json.dumps(result.get("plan", {}), ensure_ascii=False))
    print("policy:    ", json.dumps(result.get("policy_decision", {}), ensure_ascii=False))
    print("reply:     ", result.get("reply", ""))
    print("reply_source:", result.get("reply_source", ""))
    print("guard:     ", json.dumps(result.get("guard_result", {}), ensure_ascii=False))
    if result.get("errors"):
        print("errors:    ", json.dumps(result["errors"], ensure_ascii=False))
    print()


if __name__ == "__main__":
    sys.exit(main())
