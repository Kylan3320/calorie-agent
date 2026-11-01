"""Refresh the explicitly isolated package, reusing its already verified Linux deps."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "deploy/cos-probe.zip"
with zipfile.ZipFile(TARGET) as previous:
    entries = {n: previous.read(n) for n in previous.namelist() if n.startswith("deps/") and not n.endswith("/")}
for base in ("agent", "config"):
    for path in (ROOT / base).rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix in {".py", ".yaml", ".json"}:
            entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
for name in ("app.py", "config.yaml", "harness/eval/cos_probe_live.py", "harness/eval/cos_pipeline_live.py", "harness/eval/personal_cases.py"):
    entries[name] = (ROOT / name).read_bytes()
bootstrap = 'import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parent/"deps"))\n'
entries["cos_probe_entry.py"] = (bootstrap + "from harness.eval.cos_probe_live import handler\n").encode()
entries["cos_pipeline_entry.py"] = (bootstrap + "from harness.eval.cos_pipeline_live import ingress_handler, message_handler, sync_handler\n").encode()
digest = hashlib.sha256()
for name, data in sorted(entries.items()):
    digest.update(name.encode())
    digest.update(data)
build = "cos-probe-" + digest.hexdigest()[:16]
entries["BUILD_ID"] = build.encode()
temporary = TARGET.with_suffix(".new.zip")
with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
    for name, data in entries.items():
        archive.writestr(name, data)
temporary.replace(TARGET)
metadata = {"build_id": build, "bytes": TARGET.stat().st_size, "sha256": hashlib.sha256(TARGET.read_bytes()).hexdigest(),
            "source": "isolated existing Linux dependency bundle; current source only", "production_changed": False}
(ROOT / "reports/cos-probe/pipeline-build.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
print(json.dumps(metadata))
