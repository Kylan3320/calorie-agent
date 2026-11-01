"""Configuration loading: config.yaml for engineering config, env for secrets/tables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# 检索器合法值由 agent/storage/knowledge_retriever.py 维护。

PRESETS = ("live", "shadow_fakewrite", "shadow_readonly")
PLANNERS = ("deterministic", "deepseek")
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEZONE_OFFSET_HOURS = 8.0
DEFAULT_ALLOWED_TOOLS = ("record_food", "query_today", "query_food_nutrition", "undo_last", "query_knowledge", "query_history")


@dataclass(frozen=True)
class Config:
    preset: str
    planner: str
    model: str
    allowed_tools: tuple[str, ...]
    timezone_offset_hours: float
    knowledge_retriever: str = "faq"

    def as_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "planner": self.planner,
            "model": self.model,
            "allowed_tools": list(self.allowed_tools),
            "timezone_offset_hours": self.timezone_offset_hours,
            "knowledge_retriever": self.knowledge_retriever,
        }


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - deploy installs pyyaml
        raise RuntimeError("PyYAML is required to read config.yaml") from exc
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=1)
def load_config() -> Config:
    raw = _load_yaml(ROOT / "config.yaml")
    # env 优先于 yaml：灰度切换预设（PRESET）无需重新打包部署
    preset = str(os.getenv("PRESET") or raw.get("preset") or "live").strip()
    if preset not in PRESETS:
        raise ValueError(f"unknown preset: {preset} (expected one of {PRESETS})")
    planner = str(raw.get("planner") or "deepseek").strip()
    if planner not in PLANNERS:
        raise ValueError(f"unknown planner: {planner} (expected one of {PLANNERS})")
    knowledge_retriever = str(os.getenv("KNOWLEDGE_RETRIEVER") or raw.get("knowledge_retriever") or "faq").strip()
    from .storage.knowledge_retriever import KNOWLEDGE_RETRIEVERS

    if knowledge_retriever not in KNOWLEDGE_RETRIEVERS:
        raise ValueError(f"unknown knowledge_retriever: {knowledge_retriever} (expected one of {KNOWLEDGE_RETRIEVERS})")
    allowed = raw.get("allowed_tools")
    if not isinstance(allowed, list) or not allowed:
        allowed = list(DEFAULT_ALLOWED_TOOLS)
    try:
        offset_value = raw.get("timezone_offset_hours")
        offset = float(offset_value if offset_value is not None else os.getenv("APP_TIMEZONE_OFFSET_HOURS") or DEFAULT_TIMEZONE_OFFSET_HOURS)
    except (TypeError, ValueError):
        offset = DEFAULT_TIMEZONE_OFFSET_HOURS
    return Config(
        preset=preset,
        planner=planner,
        model=str(raw.get("model") or os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL),
        allowed_tools=tuple(str(item) for item in allowed),
        timezone_offset_hours=offset,
        knowledge_retriever=knowledge_retriever,
    )


def reset_config_cache() -> None:
    load_config.cache_clear()


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default)
