"""Release boundaries checked with local artifacts and injected SCF transport."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
from unittest.mock import patch
import zipfile

from deploy import api_deploy, check_package, deploy_rag_layer, ingest_knowledge, production_release


def RL01():
    """Explicit targets, preserved credentials/configuration and frozen artifact binding."""
    for module in (api_deploy, deploy_rag_layer):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                module.main([])
            except SystemExit as exc:
                assert exc.code == 2
            else:
                raise AssertionError("release without a function target was accepted")
    with tempfile.TemporaryDirectory() as temporary:
        package = Path(temporary) / "package.zip"
        package.write_bytes(b"frozen-package")
        calls = []
        def request(action, payload):
            calls.append((action, payload))
            return {"Status": "Active"}
        with patch.object(api_deploy, "tc3_request", request):
            api_deploy.main(["--function", "message-worker", str(package)])
        assert [action for action, _ in calls] == ["UpdateFunctionCode", "GetFunction"]
        assert all(payload["FunctionName"] == "message-worker" for _, payload in calls)
        assert base64.b64decode(calls[0][1]["ZipFile"]) == b"frozen-package"
    calls = []
    def request(action, payload):
        calls.append((action, payload))
        if action == "GetFunction":
            return {"Environment": {"Variables": [{"Key": "KEEP", "Value": "existing"}]},
                    "Layers": [{"LayerName": "parser", "LayerVersion": 1}, {"LayerName": "calorie-rag", "LayerVersion": 7}]}
        return {}
    with patch.object(deploy_rag_layer, "tc3_request", request):
        deploy_rag_layer.main(["--function", "message-worker", "--attach-only", "8"])
    assert [action for action, _ in calls] == ["GetFunction", "UpdateFunctionConfiguration"]
    updated = calls[-1][1]
    assert updated["FunctionName"] == "message-worker"
    assert updated["Layers"] == [{"LayerName": "parser", "LayerVersion": 1}, {"LayerName": "calorie-rag", "LayerVersion": 8}]
    assert {v["Key"]: v["Value"] for v in updated["Environment"]["Variables"]} == {
        "KEEP": "existing", "RAG_INDEX_DIR": "/opt/rag_index", "KNOWLEDGE_RETRIEVER": "lightrag", "TIKTOKEN_CACHE_DIR": "/opt/tiktoken_cache"}
    captured = []
    class Response(io.BytesIO):
        pass
    def urlopen(request, **kwargs):
        captured.append(request)
        return Response(b'{"Response":{"RequestId":"stub"}}')
    with patch.dict(os.environ, {"TENCENTCLOUD_SECRET_ID": "test-id", "TENCENTCLOUD_SECRET_KEY": "test-key", "TENCENTCLOUD_SESSION_TOKEN": "temporary-token"}), patch.object(api_deploy.urllib.request, "urlopen", urlopen):
        assert api_deploy.tc3_request("GetFunction", {"FunctionName": "message-worker"}) == {"RequestId": "stub"}
    assert dict((k.lower(), v) for k, v in captured[0].header_items())["x-tc-token"] == "temporary-token"

    # The checker must consume frozen ZIP bytes, even if a mutable build has the same ID.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        package, layer, output = root / "package.zip", root / "layer.zip", root / "check.json"
        build = root / "deploy/build"
        build.mkdir(parents=True)
        (build / "BUILD_ID").write_text("fixture-build")
        (build / "app.py").write_text("stale mutable app")
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("BUILD_ID", "fixture-build")
            archive.writestr("app.py", "frozen app")
            bootstrap = zipfile.ZipInfo("scf_bootstrap")
            bootstrap.create_system = 3
            bootstrap.external_attr = 0o100755 << 16
            archive.writestr(bootstrap, "#!/bin/sh\n")
        with zipfile.ZipFile(layer, "w") as archive:
            archive.writestr("rag_index/INDEX_VERSION", "fixture-index")
            archive.writestr("dependency.py", "frozen dependency")
        body, layer_body = package.read_bytes(), layer.read_bytes()
        runtime = {"health": {"build_id": "fixture-build"}, "python": "3.10", "platform": "linux"}
        def run(command, **kwargs):
            assert command[1:3] == ["-I", "-S"] and command[4] == "--runtime"
            extracted, dependencies = Path(command[5]), Path(command[6])
            assert (extracted / "app.py").read_text() == "frozen app"
            assert (dependencies / "dependency.py").read_text() == "frozen dependency"
            return check_package.subprocess.CompletedProcess(command, 0, json.dumps(runtime), "")
        arguments = ["--package", str(package), "--layer", str(layer), "--output", str(output)]
        with patch.object(check_package, "ROOT", root), patch.object(check_package.subprocess, "run", run):
            assert check_package.main(arguments) == 0
            evidence = json.loads(output.read_text())
            assert evidence["sha256"] == hashlib.sha256(body).hexdigest()
            assert evidence["layer_sha256"] == hashlib.sha256(layer_body).hexdigest()
            assert evidence["index_version"] == "fixture-index"
            assert all(evidence[key] == value for key, value in production_release.package_identity(package, body).items())
            runtime["health"]["build_id"] = "wrong-build"
            output.unlink()
            try:
                check_package.main(arguments)
            except RuntimeError:
                pass
            else:
                raise AssertionError("mismatched runtime evidence was accepted")
            assert not output.exists()


def _index(path):
    path.mkdir()
    values = {"kv_store_full_docs.json": {"doc": {"content": "independent source"}},
              "kv_store_text_chunks.json": {"chunk": {"full_doc_id": "doc"}},
              "kv_store_doc_status.json": {"doc": {"status": "processed"}}}
    for name in ("vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"):
        row = {"__id__": "chunk"} if name == "vdb_chunks.json" else {"__id__": "entity", "source_id": "chunk"}
        values[name] = {"embedding_dim": 2, "data": [row], "matrix": base64.b64encode(struct.pack("<ff", 1, 2)).decode()}
    for name, value in values.items():
        (path / name).write_text(json.dumps(value), encoding="utf-8")
    (path / "graph_chunk_entity_relation.graphml").write_text('<graphml xmlns="http://graphml.graphdrawing.org/xmlns"><key id="s" attr.name="source_id"/><graph><node id="e"><data key="s">chunk</data></node></graph></graphml>', encoding="utf-8")
    (path / "INDEX_VERSION").write_text("fixture-index", encoding="utf-8")


def RL02():
    """Layer packing preserves frozen dependencies and rejects corrupt indexes/overwrites."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        index = root / "index"
        _index(index)
        base, target = root / "base.zip", root / "new.zip"
        with zipfile.ZipFile(base, "w") as archive:
            archive.writestr("lightrag/parser.py", b"frozen-parser")
            archive.writestr("tiktoken_cache/tokenizer", b"frozen-tokenizer")
            archive.writestr("rag_index/obsolete.json", b"old-index")
            archive.writestr("__pycache__/stale.pyc", b"cache")
        expected = hashlib.sha256(base.read_bytes()).hexdigest()
        result = ingest_knowledge.package_layer(index, base, expected, target)
        assert result["document_count"] == 1 and result["index_version"] == "fixture-index"
        with zipfile.ZipFile(target) as archive:
            assert archive.read("lightrag/parser.py") == b"frozen-parser"
            assert archive.read("tiktoken_cache/tokenizer") == b"frozen-tokenizer"
            assert archive.read("rag_index/INDEX_VERSION") == b"fixture-index"
            assert "rag_index/obsolete.json" not in archive.namelist()
            assert not any("__pycache__" in name for name in archive.namelist())
        before = target.read_bytes()
        for digest, destination, error in ((expected, target, FileExistsError), ("wrong-hash", root / "bad.zip", ValueError)):
            try:
                ingest_knowledge.package_layer(index, base, digest, destination)
            except error:
                pass
            else:
                raise AssertionError("invalid packaging request was accepted")
        assert target.read_bytes() == before and not (root / "bad.zip").exists()
        vectors = index / "vdb_chunks.json"
        value = json.loads(vectors.read_text())
        value["matrix"] = base64.b64encode(struct.pack("<ff", 0, 0)).decode()
        vectors.write_text(json.dumps(value))
        try:
            ingest_knowledge.verify_index(index)
        except ValueError:
            pass
        else:
            raise AssertionError("zero vectors were accepted")
        _index(root / "other")
        graph = root / "other/graph_chunk_entity_relation.graphml"
        graph.write_text(graph.read_text().replace(">chunk<", ">missing<"))
        try:
            ingest_knowledge.verify_index(root / "other")
        except ValueError:
            pass
        else:
            raise AssertionError("unresolved graph provenance was accepted")


CHECKS = {"RL01": RL01, "RL02": RL02}
