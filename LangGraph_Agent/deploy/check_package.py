"""The specification's single Linux 3.10 package-start check."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import zipfile

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", type=Path, required=True)
    args = parser.parse_args()
    assert sys.version_info[:2] == (3, 10) and sys.platform == "linux"
    root = Path(__file__).resolve().parents[1]
    package = root / "deploy" / "build"
    with zipfile.ZipFile(root / "deploy" / "calorie-agent-v4.zip") as z:
        assert all(not n.startswith(("console/", "reports/", "harness/", "deploy/")) for n in z.namelist())
        assert not any("cos_probe" in n or n.startswith("dsh/") for n in z.namelist())
        assert z.read("BUILD_ID").decode() == (package / "BUILD_ID").read_text()
        assert z.getinfo("scf_bootstrap").external_attr >> 16 & 0o111
    sys.path[:0] = [str(package / "deps"), str(package), str(args.layer.resolve())]
    os.environ["RAG_INDEX_DIR"] = str(args.layer.resolve() / "rag_index")
    os.environ["KNOWLEDGE_RETRIEVER"] = "lightrag"
    os.environ["TIKTOKEN_CACHE_DIR"] = str(args.layer.resolve() / "tiktoken_cache")
    from langchain.agents import create_agent
    from agent.model import DeepSeekModel
    from agent.tools.registry import TOOL_REGISTRY
    from agent.storage.knowledge_retriever import _build_lightrag
    from app import reminder_handler, ingress_handler, message_handler, maintenance_handler
    from agent.storage.cos_store import CosStore
    from agent.storage.cos_transport import CosObjects
    from agent.storage.cos_archive import archive_pending
    from qcloud_cos import CosS3Client
    create_agent(model=DeepSeekModel(), tools=list(TOOL_REGISTRY.values()))
    rag = _build_lightrag()
    assert rag is not None and all(callable(obj) for obj in (reminder_handler, ingress_handler, message_handler, maintenance_handler, CosStore, CosObjects, archive_pending, CosS3Client))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path[:3]), "PORT": "9000"}
    process = subprocess.Popen([sys.executable, str(package / "app.py")], cwd=package, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        health = None
        for _ in range(40):
            if process.poll() is not None:
                raise RuntimeError(process.stderr.read().decode())
            try:
                health = json.load(urllib.request.urlopen("http://127.0.0.1:9000/health", timeout=1))
                break
            except OSError:
                time.sleep(.25)
        assert health and health["build_id"] == (package / "BUILD_ID").read_text()
        assert health["retriever"] == "lightrag"
        evidence = {"python": sys.version.split()[0], "platform": sys.platform, "health": {
            k: health[k] for k in ("build_id", "model", "retriever", "status")},
            "native_agent_loaded": True, "lightrag_loaded": True,
            "cos_store_loaded": True, "cos_transport_loaded": True, "archive_entry_loaded": True,
            "reminder_entry": "app.reminder_handler", "package_bytes": (root/"deploy/calorie-agent-v4.zip").stat().st_size}
        (root/"reports/package-check.json").write_text(json.dumps(evidence, indent=2))
        print(json.dumps(evidence))
    finally:
        process.terminate()
        process.wait(timeout=5)

if __name__ == "__main__":
    main()
