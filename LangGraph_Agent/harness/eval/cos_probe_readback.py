"""Independent read-only Feishu table audit for explicit isolated probe scopes."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--scope", choices=("pipeline-validation", "immediate-validation", "meal-validation", "idle-pipeline-validation"), required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    values = json.loads(Path(args.env_file).read_text(encoding="utf-8-sig"))
    for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "BITABLE_APP_TOKEN"):
        os.environ[key] = values[key]
    from agent.storage.bitable_adapter import BitableStore
    table = json.loads((Path(__file__).resolve().parents[2] / "reports/cos-probe/table.json").read_text(encoding="utf-8-sig"))
    store = BitableStore()
    store.intake_table = table["table_id"]
    prefix = "cos-probe-projection:" + args.scope + ":"
    started = time.time()
    rows = [r for r in store.intake_items("cos-probe-user", include_deleted=True) if r["message_id"].startswith(prefix)]
    valid = [r for r in rows if r["status"] != "deleted"]
    result = {"scope": args.scope, "read_at": time.time(), "read_ms": round((time.time()-started)*1000),
              "count": len(valid), "kcal": sum(r["nutrition"]["kcal"] for r in valid),
              "rows": [{k: r[k] for k in ("record_id", "message_id", "food_name", "grams", "nutrition", "date", "status")} for r in rows]}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
