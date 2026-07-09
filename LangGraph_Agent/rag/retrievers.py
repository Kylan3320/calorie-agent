from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any


FALLBACK_PROJECT_DOCS = [
    {
        "id": "project.agent.boundary",
        "source": "project_docs",
        "path": "fallback",
        "title": "agent boundary",
        "text": "模型只负责规划；工具产出事实；回复必须基于工具事实；写入和删除需要 policy gate。",
    },
    {
        "id": "project.v0.mode",
        "source": "project_docs",
        "path": "fallback",
        "title": "v0 mode",
        "text": "LangGraph Agent V0 是 CLI-only 和 fake-tool-only，不接 Feishu，不写真实数据。",
    },
]

USER_HISTORY = [
    {
        "id": "history.watermelon.today",
        "source": "user_history",
        "food_name": "西瓜",
        "grams": 300,
        "kcal": 84,
        "carbs": 18.0,
        "protein": 1.8,
        "fat": 0.6,
        "date_scope": "today",
    },
    {
        "id": "history.egg.default",
        "source": "user_history",
        "food_name": "鸡蛋",
        "default_grams": 100,
    },
]

FOOD_DB = [
    {"id": "food.egg", "source": "food_db", "name": "鸡蛋", "aliases": ["蛋"], "kcal_per_100g": 143},
    {"id": "food.rice", "source": "food_db", "name": "米饭", "aliases": ["白米饭"], "kcal_per_100g": 116},
    {"id": "food.watermelon", "source": "food_db", "name": "西瓜", "aliases": ["瓜"], "kcal_per_100g": 28},
]


def retrieve_all(text: str, *, include_web: bool = False) -> dict[str, list[dict[str, Any]]]:
    normalized = str(text or "").lower()
    return {
        "project_docs": retrieve_project_docs(normalized),
        "user_history": _filter_foodish(USER_HISTORY, normalized),
        "food_db": _filter_foodish(FOOD_DB, normalized),
        "web_nutrition": _web_nutrition(normalized) if include_web else [],
    }


def retrieve_project_docs(text: str, *, max_results: int = 5) -> list[dict[str, Any]]:
    chunks = _project_doc_chunks()
    scored = [(_score_chunk(text, chunk), chunk) for chunk in chunks]
    matches = [dict(chunk, score=score) for score, chunk in scored if score > 0]
    matches.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("id") or "")))
    if matches:
        return matches[:max_results]
    return [dict(item, score=0) for item in FALLBACK_PROJECT_DOCS]


def _filter_foodish(items: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in items:
        haystack = " ".join(
            str(value)
            for value in [
                item.get("food_name"),
                item.get("name"),
                " ".join(item.get("aliases", []) if isinstance(item.get("aliases"), list) else []),
            ]
            if value
        )
        if not text or any(token in text for token in haystack.lower().split()):
            matches.append(dict(item))
            continue
        if any(str(value or "") and str(value) in text for value in (item.get("food_name"), item.get("name"))):
            matches.append(dict(item))
    return matches[:5]


def _web_nutrition(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    return [
        {
            "id": "web.mock.nutrition",
            "source": "web_nutrition",
            "text": "外部营养知识在 V0 只作为最低优先级参考，不作为写入事实。",
            "confidence": "mock",
        }
    ]


@lru_cache(maxsize=1)
def _project_doc_chunks() -> tuple[dict[str, Any], ...]:
    root = Path(__file__).resolve().parents[4]
    candidates = [
        root / "README.md",
        root / "README.en.md",
        root / "docs",
        root / "tencent_scf" / "calorie_agent" / "skills",
    ]
    chunks: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.is_file():
            chunks.extend(_chunks_from_file(candidate, root))
        elif candidate.is_dir():
            for path in sorted(candidate.glob("*.md")):
                chunks.extend(_chunks_from_file(path, root))
    return tuple(chunks or FALLBACK_PROJECT_DOCS)


def _chunks_from_file(path: Path, root: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    relative = path.relative_to(root).as_posix()
    parts = _split_markdown_sections(text)
    chunks: list[dict[str, Any]] = []
    for index, (title, body) in enumerate(parts[:20]):
        compact = _compact_text(body)
        if not compact:
            continue
        chunks.append(
            {
                "id": f"{relative}#{index}",
                "source": "project_docs",
                "path": relative,
                "title": title or path.stem,
                "text": compact[:1200],
            }
        )
    return chunks


def _split_markdown_sections(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("#"):
            if current_lines:
                sections.append((current_title, current_lines))
            current_title = line.strip("# ").strip()
            current_lines = [line]
            continue
        current_lines.append(line)
    if current_lines:
        sections.append((current_title, current_lines))
    return [(title, "\n".join(body)) for title, body in sections]


def _score_chunk(text: str, chunk: dict[str, Any]) -> int:
    if not text:
        return 1 if "langgraph" in _normalize(str(chunk.get("text") or "")) else 0
    haystack = _normalize(" ".join([str(chunk.get("title") or ""), str(chunk.get("path") or ""), str(chunk.get("text") or "")]))
    terms = _query_terms(text)
    score = 0
    for term in terms:
        if term and term in haystack:
            score += 3 if len(term) > 1 else 1
    path = str(chunk.get("path") or "")
    if any(word in text for word in ("撤销", "删除", "上一条")) and "semantic_undo" in path:
        score += 20
    if any(word in text for word in ("总结", "日报", "报告")) and "daily_report" in path:
        score += 20
    if any(word in text for word in ("鸡蛋", "米饭", "记录", "吃")) and "record_food" in path:
        score += 20
    if any(word in text for word in ("热量", "营养", "西瓜", "那条")) and "query_food_nutrition" in path:
        score += 20
    if "langgraph" in text and "langgraph-agent" in path:
        score += 15
    return score


def _query_terms(text: str) -> set[str]:
    terms = set(re.findall(r"[a-zA-Z0-9_]+", text.lower()))
    for token in ("撤销", "删除", "上一条", "鸡蛋", "米饭", "西瓜", "热量", "营养", "今天", "总结", "日报", "记录", "写入"):
        if token in text:
            terms.add(token)
    return terms


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
