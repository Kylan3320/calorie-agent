"""Check the selected business ZIP and Layer ZIP in one isolated Linux 3.10 load."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def _extract(archive, target):
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("duplicate ZIP members")
    if any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
           or "\\" in name for name in names):
        raise ValueError("ZIP member escapes extraction root")
    archive.extractall(target)


def unpack_artifacts(package, layer, directory):
    sys.path.insert(0, str(ROOT))
    from deploy.production_release import package_identity
    body, layer_body = package.read_bytes(), layer.read_bytes()
    identity = package_identity(package, body)
    with zipfile.ZipFile(io.BytesIO(body)) as archive, zipfile.ZipFile(io.BytesIO(layer_body)) as dependencies:
        if any(name.startswith(("console/", "reports/", "harness/", "deploy/", "dsh/"))
               or "cos_probe" in name for name in archive.namelist()):
            raise ValueError("non-runtime files in business ZIP")
        if not archive.getinfo("scf_bootstrap").external_attr >> 16 & 0o111:
            raise ValueError("scf_bootstrap must be executable")
        index_version = dependencies.read("rag_index/INDEX_VERSION").decode().strip()
        if not identity["build_id"] or not index_version:
            raise ValueError("BUILD_ID and INDEX_VERSION must be nonempty")
        _extract(archive, directory / "package")
        _extract(dependencies, directory / "layer")
    return {**identity, "layer": str(layer.resolve()),
            "layer_sha256": hashlib.sha256(layer_body).hexdigest(), "index_version": index_version}


def runtime_check(package, layer):
    if sys.version_info[:2] != (3, 10) or sys.platform != "linux":
        raise RuntimeError("package load requires Linux Python 3.10")
    paths = [str(package / "deps"), str(package), str(layer)]
    sys.path[:0] = paths
    os.environ.update(RAG_INDEX_DIR=str(layer / "rag_index"), KNOWLEDGE_RETRIEVER="lightrag",
                      TIKTOKEN_CACHE_DIR=str(layer / "tiktoken_cache"))
    from langchain.agents import create_agent
    from agent.model import DeepSeekModel
    from agent.context import tokens
    from agent.tools.registry import TOOL_REGISTRY
    from agent.storage.knowledge_retriever import _build_lightrag
    from app import reminder_handler, ingress_handler, message_handler, maintenance_handler, compute_handler
    from agent.storage.cos_store import CosStore
    from agent.storage.cos_transport import CosObjects
    from agent.storage.cos_archive import archive_pending
    from qcloud_cos import CosS3Client
    create_agent(model=DeepSeekModel(), tools=list(TOOL_REGISTRY.values()))
    if tokens("中文、reasoning 与完整工具请求") <= 0:
        raise RuntimeError("DeepSeek encoding failed to load")
    rag = _build_lightrag()
    if rag is None or not all(callable(obj) for obj in (reminder_handler, ingress_handler, message_handler,
            maintenance_handler, compute_handler, CosStore, CosObjects, archive_pending, CosS3Client)):
        raise RuntimeError("required runtime entry failed to load")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths), "PORT": str(port)}
    process = subprocess.Popen([sys.executable, "-S", str(package / "app.py")], cwd=package, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        health = None
        for _ in range(40):
            if process.poll() is not None:
                raise RuntimeError(process.stderr.read().decode(errors="replace"))
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                    health = json.load(response)
                break
            except OSError:
                time.sleep(.25)
        if not health or health["build_id"] != (package / "BUILD_ID").read_text().strip():
            raise RuntimeError("health BUILD_ID does not match selected ZIP")
        if health["retriever"] != "lightrag":
            raise RuntimeError("health retriever is not lightrag")
        return {"python": sys.version.split()[0], "platform": sys.platform,
                "health": {key: health[key] for key in ("build_id", "model", "retriever", "status")},
                "native_agent_loaded": True, "lightrag_loaded": True, "cos_store_loaded": True,
                "cos_transport_loaded": True, "archive_entry_loaded": True,
                "reminder_entry": "app.reminder_handler", "compute_entry": "app.compute_handler"}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True, help="exact function ZIP to deploy")
    parser.add_argument("--layer", type=Path, required=True, help="exact Layer ZIP to deploy")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/package-check.json")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        identity = unpack_artifacts(args.package, args.layer, directory)
        result = subprocess.run([sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--runtime",
                                 str(directory / "package"), str(directory / "layer")],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError("package runtime check failed:\n" + (result.stderr or result.stdout))
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        if evidence["health"]["build_id"] != identity["build_id"]:
            raise RuntimeError("runtime evidence does not match selected ZIP")
        evidence.update(identity)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--runtime":
        print(json.dumps(runtime_check(Path(sys.argv[2]), Path(sys.argv[3]))))
    else:
        raise SystemExit(main())
