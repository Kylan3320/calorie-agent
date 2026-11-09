"""Small release controls. No resource creation, implicit upload or credential output."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from deploy.api_deploy import tc3_request


def package_identity(path, body):
    """Identify the exact bytes consumed by a package check or release freeze."""
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        build_id = archive.read("BUILD_ID").decode().strip()
    return {"build_id": build_id, "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body), "package": str(path.resolve())}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["inspect", "freeze", "maintenance-on", "maintenance-off"])
    parser.add_argument("--function")
    args = parser.parse_args(argv)
    if args.operation == "freeze":
        package = ROOT / "deploy/calorie-agent-v4.zip"
        print(json.dumps(package_identity(package, package.read_bytes()), ensure_ascii=False))
        return
    if not args.function:
        parser.error("--function is required")
    identity = {"FunctionName": args.function, "Namespace": "default"}
    config = tc3_request("GetFunction", identity)
    if args.operation == "inspect":
        triggers = tc3_request("ListTriggers", identity)
        reserved = tc3_request("GetReservedConcurrencyConfig", identity)
        print(json.dumps({"function": args.function, "configuration": {key: config.get(key) for key in
            ("Status", "Runtime", "Handler", "Type", "MemorySize", "Timeout", "Role", "InstanceConcurrencyConfig")},
            "environment_keys": sorted(item["Key"] for item in config.get("Environment", {}).get("Variables", [])),
            "triggers": triggers.get("Triggers", []), "reserved": reserved}, ensure_ascii=False))
        return
    variables = {item["Key"]: item["Value"] for item in config.get("Environment", {}).get("Variables", [])}
    variables["INGRESS_MAINTENANCE"] = "1" if args.operation == "maintenance-on" else "0"
    result = tc3_request("UpdateFunctionConfiguration", {**identity,
        "Environment": {"Variables": [{"Key": key, "Value": value} for key, value in variables.items()]}})
    print(json.dumps({"function": args.function, "operation": args.operation, "request_id": result.get("RequestId")}))


if __name__ == "__main__":
    main()
