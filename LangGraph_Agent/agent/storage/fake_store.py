"""In-memory FakeStore: same contract as BitableStore (contracts/tool_protocol.md).

Used by local eval and the shadow_fakewrite preset. Never touches the network.
"""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

from .bitable_adapter import (
    DEFAULT_DAILY_STANDARD, VALID_FOOD_STATUS, _is_valid_intake_status,
    intake_client_token, message_key, normalize_food_key, stable_uuid,
)
from .food_match import normalize_food_name
from .knowledge_seed import KNOWLEDGE_SEED
from .knowledge_retriever import normalize_text
from .timeutil import extract_text, now_millis, round_number

DEFAULT_FOODS = [
    {
        "record_id": "rec_egg",
        "food_id": "food_egg",
        "name": "鸡蛋",
        "aliases": ["egg", "煮鸡蛋"],
        "carbs_per_100g": 1.1,
        "protein_per_100g": 12.6,
        "fat_per_100g": 9.5,
    },
    {
        "record_id": "rec_rice",
        "food_id": "food_rice",
        "name": "米饭",
        "aliases": ["rice"],
        "carbs_per_100g": 25.9,
        "protein_per_100g": 2.6,
        "fat_per_100g": 0.3,
    },
    {
        "record_id": "rec_chicken_breast",
        "food_id": "food_chicken_breast",
        "name": "鸡胸肉",
        "aliases": ["chicken breast"],
        "carbs_per_100g": 2.5,
        "protein_per_100g": 24.6,
        "fat_per_100g": 1.9,
    },
    {
        "record_id": "rec_rice_noodle",
        "food_id": "food_rice_noodle",
        "name": "米粉",
        "aliases": [],
        "carbs_per_100g": 24.0,
        "protein_per_100g": 1.8,
        "fat_per_100g": 0.2,
    },
]


class FakeStore:
    """In-memory business store with a fixed intake date and simulated persistence."""

    def __init__(
        self,
        foods: list[dict[str, Any]] | None = None,
        today: str = "2026-09-05",
        intake_seed: list[dict[str, Any]] | None = None,
    ) -> None:
        self.foods = [dict(food) for food in (foods if foods is not None else DEFAULT_FOODS)]
        self.knowledge = [{**entry, "entry_id": stable_uuid(f"calorie-agent:knowledge:{normalize_text(entry['question'])}")}
                          for entry in KNOWLEDGE_SEED]
        self.today = today
        self.intake: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.usage_events: list[dict[str, Any]] = []
        self.seen_client_tokens: set[str] = set()
        self._clock = 1000
        for index, row in enumerate(intake_seed or []):
            self.intake.append(
                {
                    "record_id": f"rec_seed_{index + 1}",
                    "date": str(row.get("date") or today),
                    "user_id": str(row.get("user_id") or ""),
                    "message_id": str(row.get("message_id") or f"seed_{index + 1}"),
                    "raw_text": str(row.get("raw_text") or ""),
                    "meal_type": str(row.get("meal_type") or "unknown"),
                    "food_id": str(row.get("food_id") or ""),
                    "food_name": str(row.get("food_name") or ""),
                    "grams": float(row.get("grams") or 0),
                    "nutrition": {
                        "carbs": float(row.get("carbs") or 0),
                        "protein": float(row.get("protein") or 0),
                        "fat": float(row.get("fat") or 0),
                        "kcal": float(row.get("kcal") or 0),
                    },
                    "status": str(row.get("status") or "valid"),
                    "created_at": self._next_created_at(),
                }
            )

    def kind(self) -> str:
        return "fake"

    def today_string(self) -> str:
        """Store-canonical "today" (contracts/tool_protocol.md)."""
        return self.today

    def load_user_aliases(self) -> dict[str, str]:
        from .bitable_adapter import parse_alias_env

        return parse_alias_env()

    def _next_created_at(self) -> int:
        self._clock += 1000
        return self._clock

    # --- foods --------------------------------------------------------------

    def load_foods(self) -> list[dict[str, Any]]:
        return [deepcopy(food) for food in self.foods
                if str(food.get("name") or "").strip()
                and extract_text(food.get("status")).lower() in {"", *VALID_FOOD_STATUS}
                and all(food.get(key) is not None for key in ("carbs_per_100g", "protein_per_100g", "fat_per_100g"))]

    def load_knowledge(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.knowledge if entry.get("status", "active") in {"", "active"}]

    def sync_knowledge(self, entries: list[dict[str, Any]]) -> dict[str, int]:
        existing = {row.get("entry_id"): row for row in self.knowledge if row.get("entry_id")}
        created = updated = 0
        for entry in entries:
            fields = {key: str(entry.get(key) or "") for key in
                      ("entry_id", "question", "answer", "aliases", "tags", "source")}
            fields["status"] = "active"
            old = existing.get(fields["entry_id"])
            if old is None:
                self.knowledge.append(fields)
                existing[fields["entry_id"]] = fields
                created += 1
            elif any(old.get(key) != value for key, value in fields.items()):
                old.update(fields)
                updated += 1
        return {"created_count": created, "updated_count": updated}

    def write_food(self, food: dict[str, Any]) -> dict[str, Any]:
        """Add one food-library row (Boohee M1); same contract as BitableStore."""
        name = str(food.get("name") or "").strip()
        try:
            carbs = float(food.get("carbs_per_100g"))
            protein = float(food.get("protein_per_100g"))
            fat = float(food.get("fat_per_100g"))
        except (TypeError, ValueError):
            return {"status": "invalid", "food_id": "", "name": name}
        if not name:
            return {"status": "invalid", "food_id": "", "name": name}
        normalized = normalize_food_key(name)
        food_id = stable_uuid(f"calorie-agent:food:{normalized}")
        if any(row["food_id"] == food_id for row in self.load_foods()):
            return {"status": "duplicate", "food_id": food_id, "name": name}
        token = stable_uuid(f"calorie-agent:food-create:{normalized}")
        if token in self.seen_client_tokens:
            return {"status": "duplicate", "food_id": food_id, "name": name}
        self.seen_client_tokens.add(token)
        self.foods.append(
            {
                "record_id": f"rec_food_{len(self.foods) + 1}",
                "food_id": food_id,
                "name": name,
                "aliases": list(food.get("aliases") or []),
                "carbs_per_100g": carbs,
                "protein_per_100g": protein,
                "fat_per_100g": fat,
                "source": str(food.get("source") or ""),
                "boohee_id": str(food.get("boohee_id") or ""),
            }
        )
        return {"status": "created", "food_id": food_id, "name": name}

    # --- intake ---------------------------------------------------------------

    def today_summary(self, user_id: str) -> dict[str, Any]:
        items = []
        totals = {"carbs": 0.0, "protein": 0.0, "fat": 0.0, "kcal": 0.0}
        for row in sorted(self.intake, key=lambda item: item["created_at"]):
            if not _is_valid_intake_status(row["status"]):
                continue
            if user_id and row["user_id"] and row["user_id"] != user_id:
                continue
            if row["date"] != self.today:
                continue
            item = {
                "record_id": row["record_id"],
                "message_id": row["message_id"],
                "meal_type": row["meal_type"],
                "food_name": row["food_name"],
                "grams": row["grams"],
                "created_at": row["created_at"],
                "nutrition": dict(row["nutrition"]),
            }
            items.append(item)
            for key in totals:
                totals[key] = round_number(totals[key] + float(row["nutrition"][key]))
        return {"date": self.today, "count": len(items), "items": items, "totals": totals, "source": "fake"}

    def write_intake(
        self,
        message: dict[str, str],
        items: list[dict[str, Any]],
        meal_type: str = "unknown",
    ) -> dict[str, Any]:
        message_id = message_key(message)
        token = intake_client_token(message)
        if message_id and any(row["message_id"] == message_id for row in self.intake):
            return {"status": "duplicate_skipped", "created_count": 0, "records": []}
        if token in self.seen_client_tokens:
            return {"status": "duplicate_skipped", "created_count": 0, "records": []}
        if not items:
            return {"status": "nothing_to_write", "created_count": 0, "records": []}
        self.seen_client_tokens.add(token)
        created = []
        for item in items:
            row = {
                "record_id": f"rec_{len(self.intake) + 1}",
                "date": str(item.get("date") or self.today),
                "user_id": message.get("user_id", ""),
                "message_id": message_id,
                "raw_text": message.get("text", ""),
                "meal_type": item.get("meal_type") or meal_type or "unknown",
                "food_id": item.get("food_id", ""),
                "food_name": item.get("matched_name", ""),
                "grams": item.get("grams", 0),
                "nutrition": dict(item["nutrition"]),
                "status": "valid",
                "created_at": self._next_created_at(),
            }
            self.intake.append(row)
            created.append({"record_id": row["record_id"]})
        return {"status": "created", "created_count": len(created), "records": created}

    def last_intake(self, user_id: str) -> dict[str, Any] | None:
        valid = [row for row in self.intake if _is_valid_intake_status(row["status"])]
        if user_id:
            valid = [row for row in valid if row["user_id"] == user_id]
        if not valid:
            return None
        row = max(valid, key=lambda item: item["created_at"])
        return {key: deepcopy(row[key]) for key in
                ("record_id", "message_id", "meal_type", "food_name", "grams", "created_at", "nutrition")}

    def undo_last(self, user_id: str, record_id: str = "") -> dict[str, Any]:
        if not record_id:
            item = self.last_intake(user_id)
            if item is None:
                return {"status": "nothing_to_undo", "item": None}
            record_id = item["record_id"]
        row = next((row for row in self.intake if row["record_id"] == record_id
                    and (not user_id or row["user_id"] == user_id)), None)
        if row is None or (row["status"] != "deleted" and not _is_valid_intake_status(row["status"])):
            return {"status": "nothing_to_undo", "item": None}
        row["status"] = "deleted"
        item = {key: deepcopy(row[key]) for key in
                ("record_id", "message_id", "meal_type", "food_name", "grams", "created_at", "nutrition")}
        return {"status": "deleted", "item": item}

    # --- standard ---------------------------------------------------------------

    def get_standard(self, user_id: str) -> dict[str, Any]:
        return {**DEFAULT_DAILY_STANDARD, "source": "fake"}

    def summarize_history(self, user_id: str, start_date: str, end_date: str, food_query: str = "") -> dict[str, Any]:
        normalized_food = normalize_food_name(food_query)
        days: dict[str, dict[str, Any]] = {}
        total = {"count": 0, "grams": 0.0, "kcal": 0.0, "carbs": 0.0, "protein": 0.0, "fat": 0.0}
        for row in self.intake:
            if not _is_valid_intake_status(row["status"]):
                continue
            if user_id and row["user_id"] and row["user_id"] != user_id:
                continue
            if not (start_date <= row["date"] < end_date):
                continue
            if normalized_food:
                name = normalize_food_name(row["food_name"])
                if not name or (name != normalized_food and name not in normalized_food and normalized_food not in name):
                    continue
            day = days.setdefault(row["date"], {"date": row["date"], "count": 0, "grams": 0.0, "kcal": 0.0, "carbs": 0.0, "protein": 0.0, "fat": 0.0})
            day["count"] += 1
            total["count"] += 1
            day["grams"] = round_number(day["grams"] + float(row["grams"]))
            total["grams"] = round_number(total["grams"] + float(row["grams"]))
            for key in ("kcal", "carbs", "protein", "fat"):
                day[key] = round_number(day[key] + float(row["nutrition"][key]))
                total[key] = round_number(total[key] + float(row["nutrition"][key]))
        day_list = [days[key] for key in sorted(days)]
        over_budget_days: list[str] = []
        if not normalized_food:
            standard = self.get_standard(user_id)
            budget = float(standard.get("kcal") or 0)
            if budget > 0:
                over_budget_days = [day["date"] for day in day_list if float(day["kcal"]) > budget]
        return {
            "start_date": start_date,
            "end_date": end_date,
            "food_query": str(food_query or ""),
            "total": total,
            "days": day_list,
            "over_budget_days": over_budget_days,
            "source": "fake",
        }

    # --- pending ------------------------------------------------------------------

    def create_pending(
        self,
        message: dict[str, str],
        intent: str,
        food_name: str,
        grams: float | None,
        meal_type: str = "unknown",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        msg_key = message_key(message)
        pending_id = stable_uuid(f"calorie-agent:pending:{msg_key}:{intent}:{food_name}")
        existing = next((task for task in self.pending if task["pending_id"] == pending_id), None)
        if existing is not None:
            if (intent == "missing_food" and existing["status"] == "pending"
                    and not existing["payload"].get("candidates") and (payload or {}).get("candidates")):
                existing["payload"] = deepcopy(payload)
            return deepcopy(existing)
        token = stable_uuid(
            f"calorie-agent:pending-create:{msg_key}:{intent}:{food_name}"
        )
        self.seen_client_tokens.add(token)
        try:
            ttl_hours = max(float(os.getenv("PENDING_TASK_TTL_HOURS", "24")), 1)
        except ValueError:
            ttl_hours = 24.0
        task = {
            "record_id": f"pending_{len(self.pending) + 1}",
            "pending_id": pending_id,
            "user_id": message.get("user_id", ""),
            "chat_id": message.get("chat_id", ""),
            "source_message_id": msg_key,
            "intent": intent,
            "food_name": food_name,
            "grams": grams,
            "meal_type": meal_type or "unknown",
            "raw_text": message.get("text", ""),
            "payload": deepcopy(payload or {}),
            "status": "pending",
            "created_at": self._next_created_at(),
            "expires_at": 0 if intent in {"message_execution", "message_received"} else now_millis() + int(ttl_hours * 3600 * 1000),
        }
        self.pending.append(task)
        return deepcopy(task)

    def load_active_pending(self, user_id: str, chat_id: str) -> dict[str, Any] | None:
        active = [
            task
            for task in self.pending
            if task["status"] == "pending"
            and task["intent"] not in {"message_execution", "message_received"}
            and (not task["expires_at"] or task["expires_at"] > now_millis())
            and (not user_id or not task["user_id"] or task["user_id"] == user_id)
            and (not chat_id or not task["chat_id"] or task["chat_id"] == chat_id)
        ]
        if not active:
            return None
        return deepcopy(max(active, key=lambda task: task["created_at"]))

    def mark_pending_status(self, task: dict[str, Any], status: str) -> dict[str, Any]:
        for row in self.pending:
            if row["record_id"] == task.get("record_id"):
                row["status"] = status
                return {"code": 0}
        raise ValueError("pending task does not exist")

    def get_pending(self, pending_id: str) -> dict[str, Any] | None:
        return deepcopy(next((task for task in self.pending if task["pending_id"] == pending_id), None))

    def load_execution(self, message: dict[str, str]) -> dict[str, Any] | None:
        pending_id = stable_uuid(f"calorie-agent:pending:{message_key(message)}:message_execution:")
        return deepcopy(next((task for task in self.pending if task["pending_id"] == pending_id), None))

    def begin_execution(self, message: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        return self.create_pending(message, "message_execution", "", None, payload=payload)

    def save_execution(self, task: dict[str, Any], payload: dict[str, Any], *, completed: bool = False) -> None:
        row = next((row for row in self.pending if row["record_id"] == task["record_id"]), None)
        if row is None:
            raise ValueError("execution task does not exist")
        row.update(payload=deepcopy(payload), status="completed" if completed else "pending", expires_at=0)

    def load_unfinished_executions(self) -> list[dict[str, Any]]:
        tasks: dict[str, dict[str, Any]] = {}
        for task in self.pending:
            if task["intent"] not in {"message_execution", "message_received"} or task["status"] != "pending":
                continue
            key = task["source_message_id"]
            if key not in tasks or task["intent"] == "message_received":
                tasks[key] = task
        return deepcopy(sorted(tasks.values(), key=lambda task: task["created_at"]))

    # --- usage ------------------------------------------------------------------

    def append_usage_event(self, event: dict[str, Any]) -> None:
        self.usage_events.append(dict(event))
