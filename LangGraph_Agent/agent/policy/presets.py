"""All preset decisions, including write/cache/session permissions."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Preset:
    name: str
    allowed_tools: frozenset[str]
    store_kind: str
    send_reply: bool
    allow_write: bool

def resolve_preset(name, config_allowed_tools):
    from ..tools.registry import TOOL_REGISTRY
    all_tools = set(TOOL_REGISTRY)
    if name == "live":
        return Preset(name, frozenset(all_tools & set(config_allowed_tools)), "bitable", True, True)
    if name == "shadow_fakewrite":
        return Preset(name, frozenset(all_tools), "fake", False, True)
    if name == "shadow_readonly":
        return Preset(name, frozenset(n for n,t in TOOL_REGISTRY.items() if t.metadata["risk"] == "read_only"), "bitable", False, False)
    raise ValueError(f"unknown preset: {name}")
