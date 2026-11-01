"""Storage layer: the only place allowed to talk to Feishu/Bitable (see AGENTS.md)."""

from .bitable_adapter import BitableStore, intake_client_token, message_key, reply_uuid, stable_uuid, usage_event_token
from .fake_store import FakeStore
from .timeutil import now_millis, parse_grams_from_text, round_number, today_start_millis, today_string

__all__ = [
    "BitableStore",
    "FakeStore",
    "intake_client_token",
    "message_key",
    "now_millis",
    "parse_grams_from_text",
    "reply_uuid",
    "round_number",
    "stable_uuid",
    "today_start_millis",
    "today_string",
    "usage_event_token",
]


def create_store(config, preset=None):
    from ..policy import resolve_preset
    from .cos_store import CosStore
    preset = preset or resolve_preset(config.preset, config.allowed_tools)
    if preset.store_kind == "fake":
        return FakeStore()
    return CosStore(offset_hours=config.timezone_offset_hours, readonly=not preset.allow_write)
