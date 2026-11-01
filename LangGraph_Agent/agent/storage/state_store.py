"""Shared durable document semantics using existing pending IDs and tokens."""
from copy import deepcopy

class StateStore:
    def load_document(self, kind, user_id, scope=""):
        from .bitable_adapter import stable_uuid
        key = f"{kind}:{user_id}:{scope}"
        task = self.get_pending(stable_uuid(f"calorie-agent:pending:{key}:{kind}:"))
        return deepcopy(task["payload"]) if task else {}

    def save_document(self, kind, user_id, scope, payload):
        message = dict(message_id=f"{kind}:{user_id}:{scope}", user_id=user_id, chat_id=scope, text="")
        task = self.create_pending(message, kind, "", None, payload=payload)
        if task.get("status") == "not_configured":
            raise RuntimeError("pending table required for persistent state")
        self.save_execution(task, deepcopy(payload))

    def get_standard(self, user_id):
        settings = self.load_document("user_settings", user_id)
        return {**self.base_standard(user_id), **settings.get("targets", {}),
                **settings.get("daily_targets", {}).get(self.today_string(), {})}

    def load_session(self, user_id, session_id):
        return self.load_document("agent_session", user_id, session_id)

    def save_session(self, user_id, session_id, session):
        self.save_document("agent_session", user_id, session_id, session)
