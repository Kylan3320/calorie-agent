"""Repository interfaces for V3 storage.

The MySQL implementation is intentionally not wired into the online runtime in
this thread. Tests use InMemoryRepository so local verification does not depend
on a running MySQL server.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from typing import Any
import uuid

from . import mysql_client


DEFAULT_DAILY_STANDARD = {
    "kcal": 2230.0,
    "carbs": 250.0,
    "protein": 150.0,
    "fat": 70.0,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stable_id(prefix: str, *parts: Any) -> str:
    text = ":".join(str(part) for part in parts)
    return f"{prefix}_{uuid.uuid5(uuid.NAMESPACE_URL, text).hex}"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _payload_date(value: Any | None = None) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if value:
        return str(value)[:10]
    return datetime.now(timezone.utc).date().isoformat()


def _mysql_timestamp(value: Any | None = None) -> str:
    text = _clean_text(value)
    if not text:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return text.replace("T", " ").replace("Z", "")


def _matches_date_scope(row_date: str, date_scope: Any) -> bool:
    if date_scope is None:
        return True
    if isinstance(date_scope, str):
        return row_date == date_scope[:10]
    if isinstance(date_scope, date):
        return row_date == date_scope.isoformat()
    if isinstance(date_scope, dict):
        exact = date_scope.get("date")
        if exact:
            return row_date == _payload_date(exact)
        start = date_scope.get("start_date") or date_scope.get("start")
        end = date_scope.get("end_date") or date_scope.get("end")
        if start and row_date < _payload_date(start):
            return False
        if end and row_date > _payload_date(end):
            return False
        return True
    return True


class BaseMySQLRepository:
    def __init__(self, client: Any = mysql_client) -> None:
        self.client = client


class TenantRepository(BaseMySQLRepository):
    def resolve_or_create_user(self, feishu_app_id: str, feishu_open_id: str, chat_id: str) -> dict[str, Any]:
        feishu_app_id = _clean_text(feishu_app_id)
        feishu_open_id = _clean_text(feishu_open_id)
        chat_id = _clean_text(chat_id)
        if not feishu_app_id or not feishu_open_id:
            raise ValueError("feishu_app_id and feishu_open_id are required")

        tenant = self.client.fetch_one(
            "SELECT * FROM tenants WHERE feishu_app_id=%s",
            (feishu_app_id,),
        )
        if tenant is None:
            tenant_id = _stable_id("tenant", feishu_app_id)
            self.client.execute(
                """
                INSERT INTO tenants (tenant_id, feishu_app_id, status)
                VALUES (%s, %s, 'active')
                ON DUPLICATE KEY UPDATE updated_at=CURRENT_TIMESTAMP
                """,
                (tenant_id, feishu_app_id),
            )
        else:
            tenant_id = tenant["tenant_id"]

        user = self.client.fetch_one(
            "SELECT * FROM users WHERE tenant_id=%s AND feishu_open_id=%s",
            (tenant_id, feishu_open_id),
        )
        if user is None:
            user_id = _stable_id("user", tenant_id, feishu_open_id)
            self.client.execute(
                """
                INSERT INTO users (user_id, tenant_id, feishu_open_id, chat_id, status)
                VALUES (%s, %s, %s, %s, 'active')
                ON DUPLICATE KEY UPDATE chat_id=VALUES(chat_id), updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, tenant_id, feishu_open_id, chat_id),
            )
        else:
            user_id = user["user_id"]
            if chat_id and chat_id != user.get("chat_id"):
                self.client.execute(
                    "UPDATE users SET chat_id=%s, updated_at=CURRENT_TIMESTAMP WHERE user_id=%s",
                    (chat_id, user_id),
                )

        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "feishu_open_id": feishu_open_id,
            "chat_id": chat_id,
            "message_id": "",
            "raw_text": "",
            "event_time": "",
        }


class UserRepository(BaseMySQLRepository):
    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return self.client.fetch_one("SELECT * FROM users WHERE user_id=%s", (_clean_text(user_id),))

    def set_user_status(self, user_id: str, status: str) -> dict[str, Any]:
        result = self.client.execute(
            "UPDATE users SET status=%s, updated_at=CURRENT_TIMESTAMP WHERE user_id=%s",
            (_clean_text(status), _clean_text(user_id)),
        )
        return {"updated": result["rowcount"] > 0, "rowcount": result["rowcount"]}


class FoodRepository(BaseMySQLRepository):
    def search_food(self, user_id: str, query: str) -> list[dict[str, Any]]:
        user = self.get_user(user_id)
        if user is None:
            return []
        like = f"%{_clean_text(query).lower()}%"
        return self.client.fetch_all(
            """
            SELECT DISTINCT f.*
            FROM foods f
            LEFT JOIN food_aliases a ON a.food_id=f.food_id AND a.status='active'
            WHERE f.status <> 'deleted'
              AND (f.scope='global' OR (f.tenant_id=%s AND f.user_id=%s))
              AND (LOWER(f.name) LIKE %s OR LOWER(a.alias) LIKE %s)
            ORDER BY f.scope DESC, f.updated_at DESC
            LIMIT 20
            """,
            (user["tenant_id"], user_id, like, like),
        )

    def get_food(self, food_id: str, user_id: str) -> dict[str, Any] | None:
        user = self.get_user(user_id)
        if user is None:
            return None
        return self.client.fetch_one(
            """
            SELECT *
            FROM foods
            WHERE food_id=%s
              AND status <> 'deleted'
              AND (scope='global' OR (tenant_id=%s AND user_id=%s))
            """,
            (_clean_text(food_id), user["tenant_id"], user_id),
        )

    def create_user_food(self, user_id: str, food_payload: dict[str, Any]) -> dict[str, Any]:
        user = self.get_user(user_id)
        if user is None:
            raise ValueError("user_id does not exist")
        name = _clean_text(food_payload.get("name") or food_payload.get("food_name"))
        if not name:
            raise ValueError("food name is required")
        food_id = food_payload.get("food_id") or _new_id("food")
        carbs = food_payload.get("carbs_per_100g")
        protein = food_payload.get("protein_per_100g")
        fat = food_payload.get("fat_per_100g")
        status = _clean_text(food_payload.get("status")) or "active"
        if carbs is None or protein is None or fat is None:
            status = "pending"
        self.client.execute(
            """
            INSERT INTO foods (
                food_id, tenant_id, user_id, scope, name, carbs_per_100g,
                protein_per_100g, fat_per_100g, kcal_per_100g, status
            )
            VALUES (%s, %s, %s, 'user', %s, %s, %s, %s, %s, %s)
            """,
            (
                food_id,
                user["tenant_id"],
                user_id,
                name,
                carbs,
                protein,
                fat,
                food_payload.get("kcal_per_100g"),
                status,
            ),
        )
        return self.get_food(food_id, user_id) or {"food_id": food_id, "status": status}

    def upsert_food_alias(self, user_id: str, food_id: str, alias: str) -> dict[str, Any]:
        user = self.get_user(user_id)
        food = self.get_food(food_id, user_id)
        if user is None or food is None:
            raise ValueError("user_id or food_id does not exist")
        alias_id = _stable_id("alias", user["tenant_id"], user_id, alias)
        self.client.execute(
            """
            INSERT INTO food_aliases (alias_id, tenant_id, user_id, food_id, alias, status)
            VALUES (%s, %s, %s, %s, %s, 'active')
            ON DUPLICATE KEY UPDATE food_id=VALUES(food_id), status='active', updated_at=CURRENT_TIMESTAMP
            """,
            (alias_id, user["tenant_id"], user_id, food_id, _clean_text(alias)),
        )
        return {"alias_id": alias_id, "food_id": food_id, "alias": _clean_text(alias), "updated": True}


class IntakeRepository(BaseMySQLRepository):
    def create_intake_record(
        self,
        user_id: str,
        record_payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        user = self.get_user(user_id)
        if user is None:
            raise ValueError("user_id does not exist")
        idempotency_key = _clean_text(idempotency_key)
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        existing = self.client.fetch_one(
            "SELECT * FROM intake_records WHERE tenant_id=%s AND user_id=%s AND idempotency_key=%s",
            (user["tenant_id"], user_id, idempotency_key),
        )
        if existing:
            return {"created": False, "idempotent": True, "record": existing}

        record_id = record_payload.get("record_id") or _new_id("intake")
        record_date = _payload_date(record_payload.get("date"))
        self.client.execute(
            """
            INSERT INTO intake_records (
                record_id, tenant_id, user_id, message_id, idempotency_key, food_id,
                food_name_snapshot, grams, kcal, carbs, protein, fat, meal_type,
                raw_text, record_date, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
            """,
            (
                record_id,
                user["tenant_id"],
                user_id,
                _clean_text(record_payload.get("message_id")),
                idempotency_key,
                record_payload.get("food_id"),
                _clean_text(record_payload.get("food_name_snapshot") or record_payload.get("food_name")),
                _number(record_payload.get("grams")),
                _number(record_payload.get("kcal")),
                _number(record_payload.get("carbs")),
                _number(record_payload.get("protein")),
                _number(record_payload.get("fat")),
                _clean_text(record_payload.get("meal_type")) or "unknown",
                _clean_text(record_payload.get("raw_text")),
                record_date,
            ),
        )
        return {
            "created": True,
            "idempotent": False,
            "record": self.client.fetch_one("SELECT * FROM intake_records WHERE record_id=%s", (record_id,)),
        }

    def list_intake_records(
        self,
        user_id: str,
        date_scope: Any = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        user = self.get_user(user_id)
        if user is None:
            return []
        filters = filters or {}
        params: list[Any] = [user["tenant_id"], user_id]
        where = ["tenant_id=%s", "user_id=%s"]
        if filters.get("status"):
            where.append("status=%s")
            params.append(filters["status"])
        else:
            where.append("status <> 'deleted'")
        if isinstance(date_scope, (str, date)):
            where.append("record_date=%s")
            params.append(_payload_date(date_scope))
        elif isinstance(date_scope, dict):
            if date_scope.get("date"):
                where.append("record_date=%s")
                params.append(_payload_date(date_scope["date"]))
            else:
                if date_scope.get("start_date") or date_scope.get("start"):
                    where.append("record_date >= %s")
                    params.append(_payload_date(date_scope.get("start_date") or date_scope.get("start")))
                if date_scope.get("end_date") or date_scope.get("end"):
                    where.append("record_date <= %s")
                    params.append(_payload_date(date_scope.get("end_date") or date_scope.get("end")))
        return self.client.fetch_all(
            "SELECT * FROM intake_records WHERE " + " AND ".join(where) + " ORDER BY created_at ASC",
            tuple(params),
        )

    def soft_delete_intake_records(self, user_id: str, selector: dict[str, Any], reason: str) -> dict[str, Any]:
        records = self.list_intake_records(user_id, None, {})
        if selector.get("record_id"):
            selected = [row for row in records if row.get("record_id") == selector["record_id"]]
        elif selector.get("message_id"):
            selected = [row for row in records if row.get("message_id") == selector["message_id"]]
        elif selector.get("last"):
            selected = records[-1:]
        else:
            selected = []
        record_ids = [row["record_id"] for row in selected]
        for record_id in record_ids:
            self.client.execute(
                """
                UPDATE intake_records
                SET status='deleted', delete_reason=%s, updated_at=CURRENT_TIMESTAMP
                WHERE record_id=%s
                """,
                (_clean_text(reason), record_id),
            )
        return {"deleted_count": len(record_ids), "record_ids": record_ids}

    def summarize_intake(self, user_id: str, date_scope: Any = None) -> dict[str, Any]:
        records = self.list_intake_records(user_id, date_scope, {})
        totals = {"kcal": 0.0, "carbs": 0.0, "protein": 0.0, "fat": 0.0}
        for record in records:
            for key in totals:
                totals[key] += _number(record.get(key))
        return {"user_id": user_id, "date_scope": date_scope, "totals": totals, "record_count": len(records)}


class StandardRepository(BaseMySQLRepository):
    def get_daily_standard(self, user_id: str, standard_date: Any) -> dict[str, Any]:
        user = self.get_user(user_id)
        if user is None:
            raise ValueError("user_id does not exist")
        day = _payload_date(standard_date)
        row = self.client.fetch_one(
            "SELECT * FROM standards WHERE tenant_id=%s AND user_id=%s AND standard_date=%s",
            (user["tenant_id"], user_id, day),
        )
        if row:
            return row
        return {"tenant_id": user["tenant_id"], "user_id": user_id, "standard_date": day, **DEFAULT_DAILY_STANDARD}

    def upsert_daily_standard(self, user_id: str, standard_payload: dict[str, Any]) -> dict[str, Any]:
        user = self.get_user(user_id)
        if user is None:
            raise ValueError("user_id does not exist")
        day = _payload_date(standard_payload.get("date") or standard_payload.get("standard_date"))
        standard_id = _stable_id("standard", user["tenant_id"], user_id, day)
        values = {**DEFAULT_DAILY_STANDARD, **standard_payload}
        self.client.execute(
            """
            INSERT INTO standards (standard_id, tenant_id, user_id, standard_date, kcal, carbs, protein, fat)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                kcal=VALUES(kcal), carbs=VALUES(carbs), protein=VALUES(protein),
                fat=VALUES(fat), updated_at=CURRENT_TIMESTAMP
            """,
            (
                standard_id,
                user["tenant_id"],
                user_id,
                day,
                _number(values.get("kcal")),
                _number(values.get("carbs")),
                _number(values.get("protein")),
                _number(values.get("fat")),
            ),
        )
        return {"standard_id": standard_id, "updated": True}


class MemoryRepository(BaseMySQLRepository):
    def list_memories(self, user_id: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        user = self.get_user(user_id)
        if user is None:
            return []
        filters = filters or {}
        params: list[Any] = [user["tenant_id"], user_id]
        where = ["tenant_id=%s", "user_id=%s", "status <> 'deleted'"]
        if filters.get("memory_type"):
            where.append("memory_type=%s")
            params.append(filters["memory_type"])
        if filters.get("key"):
            where.append("memory_key=%s")
            params.append(filters["key"])
        return self.client.fetch_all(
            "SELECT * FROM memories WHERE " + " AND ".join(where) + " ORDER BY updated_at DESC",
            tuple(params),
        )

    def upsert_memory(self, user_id: str, memory_payload: dict[str, Any]) -> dict[str, Any]:
        user = self.get_user(user_id)
        if user is None:
            raise ValueError("user_id does not exist")
        memory_type = _clean_text(memory_payload.get("memory_type")) or "planner_hint"
        key = _clean_text(memory_payload.get("key") or memory_payload.get("memory_key"))
        if not key:
            raise ValueError("memory key is required")
        memory_id = memory_payload.get("memory_id") or _stable_id("memory", user["tenant_id"], user_id, memory_type, key)
        self.client.execute(
            """
            INSERT INTO memories (
                memory_id, tenant_id, user_id, memory_type, memory_key,
                value_json, confidence, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
            ON DUPLICATE KEY UPDATE
                value_json=VALUES(value_json), confidence=VALUES(confidence),
                status='active', updated_at=CURRENT_TIMESTAMP
            """,
            (
                memory_id,
                user["tenant_id"],
                user_id,
                memory_type,
                key,
                json.dumps(memory_payload.get("value", {}), ensure_ascii=False),
                _number(memory_payload.get("confidence"), 1.0),
            ),
        )
        return {"memory_id": memory_id, "updated": True}

    def delete_memory(self, user_id: str, selector: dict[str, Any]) -> dict[str, Any]:
        user = self.get_user(user_id)
        if user is None:
            return {"deleted_count": 0}
        params: list[Any] = [user["tenant_id"], user_id]
        where = ["tenant_id=%s", "user_id=%s"]
        if selector.get("memory_id"):
            where.append("memory_id=%s")
            params.append(selector["memory_id"])
        elif selector.get("key"):
            where.append("memory_key=%s")
            params.append(selector["key"])
        else:
            return {"deleted_count": 0}
        result = self.client.execute(
            "UPDATE memories SET status='deleted', updated_at=CURRENT_TIMESTAMP WHERE " + " AND ".join(where),
            tuple(params),
        )
        return {"deleted_count": result["rowcount"]}


class MetricsRepository(BaseMySQLRepository):
    def record_usage_event(self, event_payload: dict[str, Any]) -> dict[str, Any]:
        event_id = event_payload.get("event_id") or _new_id("event")
        self.client.execute(
            """
            INSERT INTO usage_events (
                event_id, tenant_id, user_id, event_type, success, latency_ms,
                error_type, message_id, metadata_json, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                _clean_text(event_payload.get("tenant_id")),
                _clean_text(event_payload.get("user_id")),
                _clean_text(event_payload.get("event_type")),
                1 if event_payload.get("success", True) else 0,
                int(event_payload.get("latency_ms") or 0),
                _clean_text(event_payload.get("error_type")),
                _clean_text(event_payload.get("message_id")),
                json.dumps(event_payload.get("metadata", event_payload.get("metadata_json", {})), ensure_ascii=False),
                _mysql_timestamp(event_payload.get("created_at")),
            ),
        )
        return {"event_id": event_id, "created": True}

    def record_feedback_sample(self, feedback_payload: dict[str, Any]) -> dict[str, Any]:
        feedback_id = feedback_payload.get("feedback_id") or _new_id("feedback")
        self.client.execute(
            """
            INSERT INTO feedback_samples (
                feedback_id, tenant_id, user_id, message_id, feedback_type,
                content, sentiment, status, metadata_json, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                feedback_id,
                _clean_text(feedback_payload.get("tenant_id")),
                _clean_text(feedback_payload.get("user_id")),
                _clean_text(feedback_payload.get("message_id")),
                _clean_text(feedback_payload.get("feedback_type")) or "general",
                _clean_text(feedback_payload.get("content")),
                _clean_text(feedback_payload.get("sentiment")) or "unknown",
                _clean_text(feedback_payload.get("status")) or "open",
                json.dumps(feedback_payload.get("metadata", feedback_payload.get("metadata_json", {})), ensure_ascii=False),
                _mysql_timestamp(feedback_payload.get("created_at")),
            ),
        )
        return {"feedback_id": feedback_id, "created": True}

    def list_retention_events(self, scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        scope = scope or {}
        params: list[Any] = []
        where = ["success=1"]
        if scope.get("tenant_id"):
            where.append("tenant_id=%s")
            params.append(scope["tenant_id"])
        if scope.get("user_id"):
            where.append("user_id=%s")
            params.append(scope["user_id"])
        if scope.get("start"):
            where.append("created_at >= %s")
            params.append(_mysql_timestamp(scope["start"]))
        if scope.get("end"):
            where.append("created_at <= %s")
            params.append(_mysql_timestamp(scope["end"]))
        return self.client.fetch_all(
            "SELECT * FROM usage_events WHERE " + " AND ".join(where) + " ORDER BY created_at ASC",
            tuple(params),
        )


class MySQLStorageRepository(
    TenantRepository,
    UserRepository,
    FoodRepository,
    IntakeRepository,
    StandardRepository,
    MemoryRepository,
    MetricsRepository,
):
    """Concrete repository that exposes the frozen V3 storage interface."""


class InMemoryRepository:
    """In-memory implementation used by local evals and downstream fake tests."""

    def __init__(self) -> None:
        self.tenants: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.foods: dict[str, dict[str, Any]] = {}
        self.food_aliases: dict[str, dict[str, Any]] = {}
        self.intake_records: dict[str, dict[str, Any]] = {}
        self.standards: dict[str, dict[str, Any]] = {}
        self.memories: dict[str, dict[str, Any]] = {}
        self.usage_events: dict[str, dict[str, Any]] = {}
        self.feedback_samples: dict[str, dict[str, Any]] = {}

    def resolve_or_create_user(self, feishu_app_id: str, feishu_open_id: str, chat_id: str) -> dict[str, Any]:
        feishu_app_id = _clean_text(feishu_app_id)
        feishu_open_id = _clean_text(feishu_open_id)
        chat_id = _clean_text(chat_id)
        if not feishu_app_id or not feishu_open_id:
            raise ValueError("feishu_app_id and feishu_open_id are required")
        tenant_id = _stable_id("tenant", feishu_app_id)
        self.tenants.setdefault(
            tenant_id,
            {
                "tenant_id": tenant_id,
                "feishu_app_id": feishu_app_id,
                "status": "active",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            },
        )
        user_id = _stable_id("user", tenant_id, feishu_open_id)
        user = self.users.setdefault(
            user_id,
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "feishu_open_id": feishu_open_id,
                "chat_id": chat_id,
                "status": "active",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            },
        )
        if chat_id:
            user["chat_id"] = chat_id
            user["updated_at"] = _now_iso()
        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "feishu_open_id": feishu_open_id,
            "chat_id": chat_id,
            "message_id": "",
            "raw_text": "",
            "event_time": "",
        }

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        user = self.users.get(_clean_text(user_id))
        return dict(user) if user else None

    def set_user_status(self, user_id: str, status: str) -> dict[str, Any]:
        user = self.users.get(_clean_text(user_id))
        if not user:
            return {"updated": False}
        user["status"] = _clean_text(status)
        user["updated_at"] = _now_iso()
        return {"updated": True}

    def search_food(self, user_id: str, query: str) -> list[dict[str, Any]]:
        user = self.users.get(_clean_text(user_id))
        if not user:
            return []
        needle = _clean_text(query).lower()
        candidates: list[dict[str, Any]] = []
        for food in self.foods.values():
            if food.get("status") == "deleted":
                continue
            visible = food.get("scope") == "global" or (
                food.get("tenant_id") == user["tenant_id"] and food.get("user_id") == user_id
            )
            if not visible:
                continue
            aliases = [
                alias["alias"].lower()
                for alias in self.food_aliases.values()
                if alias.get("food_id") == food["food_id"] and alias.get("status") == "active"
            ]
            if needle in food.get("name", "").lower() or any(needle in alias for alias in aliases):
                candidates.append(dict(food))
        return candidates[:20]

    def get_food(self, food_id: str, user_id: str) -> dict[str, Any] | None:
        user = self.users.get(_clean_text(user_id))
        food = self.foods.get(_clean_text(food_id))
        if not user or not food or food.get("status") == "deleted":
            return None
        visible = food.get("scope") == "global" or (
            food.get("tenant_id") == user["tenant_id"] and food.get("user_id") == user_id
        )
        return dict(food) if visible else None

    def create_user_food(self, user_id: str, food_payload: dict[str, Any]) -> dict[str, Any]:
        user = self.users.get(_clean_text(user_id))
        if not user:
            raise ValueError("user_id does not exist")
        name = _clean_text(food_payload.get("name") or food_payload.get("food_name"))
        if not name:
            raise ValueError("food name is required")
        food_id = food_payload.get("food_id") or _new_id("food")
        carbs = food_payload.get("carbs_per_100g")
        protein = food_payload.get("protein_per_100g")
        fat = food_payload.get("fat_per_100g")
        status = _clean_text(food_payload.get("status")) or "active"
        if carbs is None or protein is None or fat is None:
            status = "pending"
        food = {
            "food_id": food_id,
            "tenant_id": user["tenant_id"],
            "user_id": user_id,
            "scope": "user",
            "name": name,
            "carbs_per_100g": carbs,
            "protein_per_100g": protein,
            "fat_per_100g": fat,
            "kcal_per_100g": food_payload.get("kcal_per_100g"),
            "status": status,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        self.foods[food_id] = food
        return dict(food)

    def upsert_food_alias(self, user_id: str, food_id: str, alias: str) -> dict[str, Any]:
        user = self.users.get(_clean_text(user_id))
        food = self.get_food(food_id, user_id)
        if not user or not food:
            raise ValueError("user_id or food_id does not exist")
        alias = _clean_text(alias)
        alias_id = _stable_id("alias", user["tenant_id"], user_id, alias)
        self.food_aliases[alias_id] = {
            "alias_id": alias_id,
            "tenant_id": user["tenant_id"],
            "user_id": user_id,
            "food_id": food_id,
            "alias": alias,
            "status": "active",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        return {"alias_id": alias_id, "food_id": food_id, "alias": alias, "updated": True}

    def create_intake_record(
        self,
        user_id: str,
        record_payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        user = self.users.get(_clean_text(user_id))
        if not user:
            raise ValueError("user_id does not exist")
        idempotency_key = _clean_text(idempotency_key)
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        for record in self.intake_records.values():
            if (
                record["tenant_id"] == user["tenant_id"]
                and record["user_id"] == user_id
                and record["idempotency_key"] == idempotency_key
            ):
                return {"created": False, "idempotent": True, "record": dict(record)}

        record_id = record_payload.get("record_id") or _new_id("intake")
        record = {
            "record_id": record_id,
            "tenant_id": user["tenant_id"],
            "user_id": user_id,
            "message_id": _clean_text(record_payload.get("message_id")),
            "idempotency_key": idempotency_key,
            "food_id": record_payload.get("food_id"),
            "food_name_snapshot": _clean_text(
                record_payload.get("food_name_snapshot") or record_payload.get("food_name")
            ),
            "grams": _number(record_payload.get("grams")),
            "kcal": _number(record_payload.get("kcal")),
            "carbs": _number(record_payload.get("carbs")),
            "protein": _number(record_payload.get("protein")),
            "fat": _number(record_payload.get("fat")),
            "meal_type": _clean_text(record_payload.get("meal_type")) or "unknown",
            "raw_text": _clean_text(record_payload.get("raw_text")),
            "record_date": _payload_date(record_payload.get("date")),
            "status": "active",
            "delete_reason": "",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        self.intake_records[record_id] = record
        return {"created": True, "idempotent": False, "record": dict(record)}

    def list_intake_records(
        self,
        user_id: str,
        date_scope: Any = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        user = self.users.get(_clean_text(user_id))
        if not user:
            return []
        filters = filters or {}
        rows = []
        for record in self.intake_records.values():
            if record["tenant_id"] != user["tenant_id"] or record["user_id"] != user_id:
                continue
            if not _matches_date_scope(record["record_date"], date_scope):
                continue
            if filters.get("status"):
                if record.get("status") != filters["status"]:
                    continue
            elif record.get("status") == "deleted":
                continue
            rows.append(dict(record))
        return sorted(rows, key=lambda row: row["created_at"])

    def soft_delete_intake_records(self, user_id: str, selector: dict[str, Any], reason: str) -> dict[str, Any]:
        records = self.list_intake_records(user_id, None, {})
        if selector.get("record_id"):
            selected = [row for row in records if row["record_id"] == selector["record_id"]]
        elif selector.get("message_id"):
            selected = [row for row in records if row["message_id"] == selector["message_id"]]
        elif selector.get("last"):
            selected = records[-1:]
        else:
            selected = []
        record_ids = [row["record_id"] for row in selected]
        for record_id in record_ids:
            record = self.intake_records[record_id]
            record["status"] = "deleted"
            record["delete_reason"] = _clean_text(reason)
            record["updated_at"] = _now_iso()
        return {"deleted_count": len(record_ids), "record_ids": record_ids}

    def summarize_intake(self, user_id: str, date_scope: Any = None) -> dict[str, Any]:
        records = self.list_intake_records(user_id, date_scope, {})
        totals = {"kcal": 0.0, "carbs": 0.0, "protein": 0.0, "fat": 0.0}
        for record in records:
            for key in totals:
                totals[key] += _number(record.get(key))
        return {"user_id": user_id, "date_scope": date_scope, "totals": totals, "record_count": len(records)}

    def get_daily_standard(self, user_id: str, standard_date: Any) -> dict[str, Any]:
        user = self.users.get(_clean_text(user_id))
        if not user:
            raise ValueError("user_id does not exist")
        day = _payload_date(standard_date)
        standard = self.standards.get(_stable_id("standard", user["tenant_id"], user_id, day))
        if standard:
            return dict(standard)
        return {"tenant_id": user["tenant_id"], "user_id": user_id, "standard_date": day, **DEFAULT_DAILY_STANDARD}

    def upsert_daily_standard(self, user_id: str, standard_payload: dict[str, Any]) -> dict[str, Any]:
        user = self.users.get(_clean_text(user_id))
        if not user:
            raise ValueError("user_id does not exist")
        day = _payload_date(standard_payload.get("date") or standard_payload.get("standard_date"))
        standard_id = _stable_id("standard", user["tenant_id"], user_id, day)
        standard = {
            "standard_id": standard_id,
            "tenant_id": user["tenant_id"],
            "user_id": user_id,
            "standard_date": day,
            **DEFAULT_DAILY_STANDARD,
            **{key: _number(standard_payload.get(key)) for key in ("kcal", "carbs", "protein", "fat") if key in standard_payload},
            "updated_at": _now_iso(),
        }
        self.standards[standard_id] = standard
        return {"standard_id": standard_id, "updated": True}

    def list_memories(self, user_id: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        user = self.users.get(_clean_text(user_id))
        if not user:
            return []
        filters = filters or {}
        rows = []
        for memory in self.memories.values():
            if memory["tenant_id"] != user["tenant_id"] or memory["user_id"] != user_id:
                continue
            if memory.get("status") == "deleted":
                continue
            if filters.get("memory_type") and memory.get("memory_type") != filters["memory_type"]:
                continue
            if filters.get("key") and memory.get("memory_key") != filters["key"]:
                continue
            rows.append(dict(memory))
        return rows

    def upsert_memory(self, user_id: str, memory_payload: dict[str, Any]) -> dict[str, Any]:
        user = self.users.get(_clean_text(user_id))
        if not user:
            raise ValueError("user_id does not exist")
        memory_type = _clean_text(memory_payload.get("memory_type")) or "planner_hint"
        key = _clean_text(memory_payload.get("key") or memory_payload.get("memory_key"))
        if not key:
            raise ValueError("memory key is required")
        memory_id = memory_payload.get("memory_id") or _stable_id("memory", user["tenant_id"], user_id, memory_type, key)
        self.memories[memory_id] = {
            "memory_id": memory_id,
            "tenant_id": user["tenant_id"],
            "user_id": user_id,
            "memory_type": memory_type,
            "memory_key": key,
            "value": memory_payload.get("value", {}),
            "confidence": _number(memory_payload.get("confidence"), 1.0),
            "status": "active",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        return {"memory_id": memory_id, "updated": True}

    def delete_memory(self, user_id: str, selector: dict[str, Any]) -> dict[str, Any]:
        user = self.users.get(_clean_text(user_id))
        if not user:
            return {"deleted_count": 0}
        deleted = 0
        for memory in self.memories.values():
            if memory["tenant_id"] != user["tenant_id"] or memory["user_id"] != user_id:
                continue
            matched = selector.get("memory_id") == memory["memory_id"] or selector.get("key") == memory["memory_key"]
            if matched and memory.get("status") != "deleted":
                memory["status"] = "deleted"
                memory["updated_at"] = _now_iso()
                deleted += 1
        return {"deleted_count": deleted}

    def record_usage_event(self, event_payload: dict[str, Any]) -> dict[str, Any]:
        event_id = event_payload.get("event_id") or _new_id("event")
        self.usage_events[event_id] = {
            "event_id": event_id,
            "tenant_id": _clean_text(event_payload.get("tenant_id")),
            "user_id": _clean_text(event_payload.get("user_id")),
            "event_type": _clean_text(event_payload.get("event_type")),
            "success": bool(event_payload.get("success", True)),
            "latency_ms": int(event_payload.get("latency_ms") or 0),
            "error_type": _clean_text(event_payload.get("error_type")),
            "message_id": _clean_text(event_payload.get("message_id")),
            "metadata": event_payload.get("metadata", event_payload.get("metadata_json", {})),
            "created_at": _clean_text(event_payload.get("created_at")) or _now_iso(),
        }
        return {"event_id": event_id, "created": True}

    def record_feedback_sample(self, feedback_payload: dict[str, Any]) -> dict[str, Any]:
        feedback_id = feedback_payload.get("feedback_id") or _new_id("feedback")
        self.feedback_samples[feedback_id] = {
            "feedback_id": feedback_id,
            "tenant_id": _clean_text(feedback_payload.get("tenant_id")),
            "user_id": _clean_text(feedback_payload.get("user_id")),
            "message_id": _clean_text(feedback_payload.get("message_id")),
            "feedback_type": _clean_text(feedback_payload.get("feedback_type")) or "general",
            "content": _clean_text(feedback_payload.get("content")),
            "sentiment": _clean_text(feedback_payload.get("sentiment")) or "unknown",
            "status": _clean_text(feedback_payload.get("status")) or "open",
            "metadata": feedback_payload.get("metadata", feedback_payload.get("metadata_json", {})),
            "created_at": _clean_text(feedback_payload.get("created_at")) or _now_iso(),
        }
        return {"feedback_id": feedback_id, "created": True}

    def list_retention_events(self, scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        scope = scope or {}
        rows = []
        for event in self.usage_events.values():
            if not event.get("success"):
                continue
            if scope.get("tenant_id") and event.get("tenant_id") != scope["tenant_id"]:
                continue
            if scope.get("user_id") and event.get("user_id") != scope["user_id"]:
                continue
            if scope.get("start") and event.get("created_at", "") < scope["start"]:
                continue
            if scope.get("end") and event.get("created_at", "") > scope["end"]:
                continue
            rows.append(dict(event))
        return sorted(rows, key=lambda row: row["created_at"])
