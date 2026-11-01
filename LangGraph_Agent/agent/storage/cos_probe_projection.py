"""Manually invoked projection for the isolated probe; never a message worker."""
from copy import deepcopy
import os

from .bitable_adapter import BitableStore, INTAKE_FIELD_DEFAULTS, _intake_field
from . import feishu_client

NAME = "COS隔离验证20261101"


def create_test_table():
    store = BitableStore()
    url = feishu_client.feishu_api_base()+f"/bitable/v1/apps/{store.app_token}/tables"
    numeric = {"date", "grams", "carbs", "protein", "fat", "kcal", "created_at"}
    fields = [{"field_name": _intake_field(k), "type": 2 if k in numeric else 1}
              for k in INTAKE_FIELD_DEFAULTS]
    result = feishu_client.http_json_post(url, {"table": {"name": NAME, "fields": fields}},
        {"Authorization": "Bearer "+feishu_client.get_tenant_access_token()})
    if result.get("code") != 0:
        raise RuntimeError("isolated table creation failed: "+str(result.get("code")))
    return result["data"]["table_id"]


def sync_snapshot(state, table_id, *, scope="meal-validation"):
    if scope not in {"meal-validation", "immediate-validation", "pipeline-validation", "idle-pipeline-validation"}:
        raise ValueError("unknown projection scope")
    store = BitableStore()
    if not table_id or table_id == store.intake_table:
        raise ValueError("an explicit separate test table is required")
    # Verify the target by its exact test-only name before any write.
    url = feishu_client.feishu_api_base()+f"/bitable/v1/apps/{store.app_token}/tables?page_size=100"
    result = feishu_client.http_json_get(url,
        {"Authorization": "Bearer "+feishu_client.get_tenant_access_token()})
    if result.get("code") != 0:
        raise RuntimeError("cannot verify isolated table")
    if not any(t["table_id"] == table_id and t["name"] == NAME for t in result["data"]["items"]):
        raise ValueError("target table is not the named probe table")
    store.intake_table = table_id
    before = store.intake_items("cos-probe-user", include_deleted=True)
    for row in state["intake"]:
        if row["user_id"] != "cos-probe-user":
            raise ValueError("non-probe identity refused")
        key = "cos-probe-projection:"+scope+":"+row["record_id"]
        matches = [r for r in before if r["message_id"] == key]
        if len(matches) > 1:
            raise RuntimeError("duplicate projected identity")
        item = {**deepcopy(row), "matched_name": row["food_name"]}
        if not matches:
            if row["status"] == "deleted":
                continue
            store.write_intake({"message_id": key, "user_id": "cos-probe-user",
                                "text": "COS隔离验证；禁止用于生产账本"}, [item], row["meal_type"])
        else:
            store.revise_intake("cos-probe-user", matches[0]["record_id"],
                                None if row["status"] == "deleted" else item)
    prefix = "cos-probe-projection:"+scope+":"
    return [r for r in store.intake_items("cos-probe-user", include_deleted=True)
            if r["message_id"].startswith(prefix)]
