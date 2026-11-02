#!/usr/bin/env bash
# Build with the actual SCF Python version on Linux; no host-platform wheels.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3.10}"
BUILD="$ROOT/deploy/build"
ZIP="$ROOT/deploy/calorie-agent-v4.zip"
"$PYTHON" -c 'import sys; assert sys.version_info[:2] == (3,10), sys.version; assert sys.platform == "linux"'
# Only this fixed build directory is replaced.
if [[ "$(realpath -m "$BUILD")" != "$ROOT/deploy/build" ]]; then exit 1; fi
if [[ "${1:-}" == "--reuse-deps" ]]; then
  "$PYTHON" - "$BUILD/deps" "$ROOT/requirements.txt" <<'PY'
import importlib.metadata as m, pathlib, sys
installed={d.metadata["Name"].lower().replace("_","-"):d.version for d in m.distributions(path=[sys.argv[1]])}
for line in pathlib.Path(sys.argv[2]).read_text().splitlines():
    name,version=line.split("==")
    assert installed.get(name.lower().replace("_","-"))==version, (name,version,installed.get(name.lower().replace("_","-")))
PY
  rm -rf "$BUILD/agent"
else
  rm -rf "$BUILD"
  mkdir -p "$BUILD/deps"
fi
cp -r "$ROOT/agent" "$BUILD/agent"
cp "$ROOT/app.py" "$ROOT/config.yaml" "$ROOT/scf_bootstrap" "$ROOT/requirements.txt" "$BUILD/"
find "$BUILD/agent" -name __pycache__ -type d -prune -exec rm -rf {} +
chmod +x "$BUILD/scf_bootstrap"
if [[ "${1:-}" != "--reuse-deps" ]]; then
  "$PYTHON" -m pip install --disable-pip-version-check -q -r "$ROOT/requirements.txt" --target "$BUILD/deps"
fi
"$PYTHON" - "$BUILD" "$ZIP" <<'PY'
import hashlib, pathlib, sys, zipfile
root, target = map(pathlib.Path, sys.argv[1:])
h = hashlib.sha256()
for p in sorted(root.rglob("*")):
    if p.is_file() and "__pycache__" not in p.parts and p.name != "BUILD_ID":
        h.update(str(p.relative_to(root)).encode()); h.update(p.read_bytes())
(root / "BUILD_ID").write_text("cos-production-" + h.hexdigest()[:16])
with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as z:
    for p in root.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            info=zipfile.ZipInfo.from_file(p,str(p.relative_to(root)))
            if p.name=="scf_bootstrap": info.external_attr=0o100755 << 16
            z.writestr(info,p.read_bytes(),compress_type=zipfile.ZIP_DEFLATED)
print((root / "BUILD_ID").read_text(), target.stat().st_size)
PY
