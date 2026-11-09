"""Explicit maintenance-window export/import with an independently saved audit.

Export is read-only. Import refuses a nonempty destination, preserves source
IDs, and verifies the destination through its public Store API. This script
does not switch webhooks or delete source rows.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.storage.bitable_adapter import BitableStore
from agent.storage.migration import baseline_manifest, ensure_schema, export_cos_baseline, import_cos_baseline


def write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["export", "import", "ensure-schema"])
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--prefix", help="Explicit independent production COS prefix")
    args = parser.parse_args(argv)
    if args.operation == "ensure-schema":
        print(json.dumps({"added": ensure_schema(BitableStore())}, ensure_ascii=False))
        return
    if not args.snapshot or not args.audit:
        parser.error("--snapshot and --audit are required")
    if args.operation == "export":
        data = export_cos_baseline(BitableStore())
        write_new(args.snapshot, data)
        write_new(args.audit, baseline_manifest(data))
    else:
        if not args.prefix:
            parser.error("import requires an explicit --prefix")
        from agent.storage.cos_store import CosStore
        data = json.loads(args.snapshot.read_text(encoding="utf-8"))
        if data.get("schema") != "calorie-agent.baseline.v1":
            raise ValueError("unknown baseline schema")
        result = import_cos_baseline(CosStore(prefix=args.prefix), data)
        write_new(args.audit, {"prefix": args.prefix, **result})
    print(json.dumps({"operation": args.operation, "snapshot": str(args.snapshot), "audit": str(args.audit)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
