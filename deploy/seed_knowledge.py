#!/usr/bin/env python3
"""Initialize/update seed entries in Bitable by stable entry_id."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.storage import BitableStore, stable_uuid
from agent.storage.knowledge_retriever import normalize_text
from agent.storage.knowledge_seed import KNOWLEDGE_SEED


def main() -> int:
    entries = [{**entry, "entry_id": stable_uuid(f"calorie-agent:knowledge:{normalize_text(entry['question'])}")}
               for entry in KNOWLEDGE_SEED]
    result = BitableStore().sync_knowledge(entries)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
