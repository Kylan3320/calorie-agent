"""Local CLI (FakeStore by default, DeepSeek credentials/network required):

    python3 cli.py 米饭200g
    python3 cli.py --planner deepseek 记录鸡蛋100g，查下今天还剩多少
    python3 cli.py            # interactive loop
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import uuid
from collections.abc import Iterator
from typing import Any

from agent import FakeStore, load_config, run
from agent.storage.live_client import LiveClient
from agent.policy import resolve_preset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calorie Agent V4 local CLI")
    parser.add_argument("--planner", choices=["deepseek"], default=None)
    parser.add_argument("--preset", choices=["live", "shadow_fakewrite", "shadow_readonly"], default=None)
    parser.add_argument("text", nargs="*")
    args = parser.parse_args(argv)

    config = load_config()
    if args.planner:
        config = dataclasses.replace(config, planner=args.planner)
    if args.preset:
        config = dataclasses.replace(config, preset=args.preset)
    # CLI 默认使用本地假存储；模型与业务运行时和其他入口一致。
    if config.preset == "live" and args.preset is None:
        config = dataclasses.replace(config, preset="shadow_fakewrite")

    preset = resolve_preset(config.preset, config.allowed_tools)
    from agent.storage import create_store
    store = FakeStore() if preset.store_kind == "fake" else (create_store(config, preset) if config.preset != "live" else None)
    client = LiveClient() if config.preset == "live" else None
    user_id = os.environ["CLI_USER_ID"] if client else "local_user"
    texts = [" ".join(args.text)] if args.text else _interactive()
    session_id = "cli:" + uuid.uuid4().hex
    for text in texts:
        message = {"message_id": f"cli-{uuid.uuid4().hex}", "user_id": user_id, "chat_id": session_id, "text": text}
        result = client.submit(message, source="cli") if client else run(message, store=store, config=config)
        _print_result(result)
        from agent.graph import mark_reply_presented
        if client:
            client.presented(result["delivery_reference"])
        elif preset.allow_write:
            mark_reply_presented(store, "local_user", session_id, result)
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
    print("native calls:", json.dumps(result.get("native_calls", []), ensure_ascii=False))
    print("reply:     ", result.get("reply", ""))
    print("reply_source:", result.get("reply_source", ""))
    print("guard:     ", json.dumps(result.get("guard_result", {}), ensure_ascii=False))
    if result.get("errors"):
        print("errors:    ", json.dumps(result["errors"], ensure_ascii=False))
    print()


if __name__ == "__main__":
    sys.exit(main())
