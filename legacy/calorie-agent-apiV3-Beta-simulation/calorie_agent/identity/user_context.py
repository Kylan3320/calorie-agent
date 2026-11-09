from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _clean(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class UserContext:
    tenant_id: str
    user_id: str
    feishu_app_id: str
    feishu_open_id: str
    chat_id: str
    message_id: str
    raw_text: str
    event_time: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UserContext":
        return cls(
            tenant_id=_clean(data.get("tenant_id")),
            user_id=_clean(data.get("user_id")),
            feishu_app_id=_clean(data.get("feishu_app_id")),
            feishu_open_id=_clean(data.get("feishu_open_id")),
            chat_id=_clean(data.get("chat_id")),
            message_id=_clean(data.get("message_id")),
            raw_text=_clean(data.get("raw_text") if data.get("raw_text") is not None else data.get("text")),
            event_time=_clean(data.get("event_time")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "feishu_app_id": self.feishu_app_id,
            "feishu_open_id": self.feishu_open_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "raw_text": self.raw_text,
            "event_time": self.event_time,
        }

    def to_message_dict(self, base_message: Mapping[str, Any] | None = None) -> dict[str, Any]:
        message = dict(base_message or {})
        message.update(self.to_dict())
        message["text"] = self.raw_text
        return message
