"""本地网页配置台：配置编辑（分层溯源）/ Playground / 轨迹查看 / 评测按钮。

    .venv/bin/python3 -m console.server          # http://127.0.0.1:8765

思想来自 DeepSeek Harness（学思想不搬内核，REQUIREMENTS.md D7/D18）：
- 配置三层可见：schema 默认 < config.yaml < 环境变量覆盖，逐字段显示来源。
- Playground 直接调 agent.graph.run（不发飞书消息），默认 FakeStore，可切真实表格。
- 轨迹页读取 calorie_agent_trace.v1 JSONL（Playground 的每次运行也会写轨迹）。
- 评测按钮直接跑 harness/eval/runner.py（smoke/full/单用例）。
"""

from __future__ import annotations

import dataclasses
import html
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import FakeStore, load_config, run  # noqa: E402
from agent.config import PLANNERS, PRESETS, reset_config_cache  # noqa: E402
from agent.schemas import SUPPORTED_TOOLS  # noqa: E402

ENV_KEYS = [
    "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN", "DEEPSEEK_API_KEY",
    "BITABLE_APP_TOKEN", "BITABLE_FOOD_TABLE_ID", "BITABLE_INTAKE_TABLE_ID",
    "BITABLE_STANDARD_TABLE_ID", "BITABLE_PENDING_TABLE_ID", "BITABLE_USAGE_TABLE_ID",
    "PRESET", "DEEPSEEK_MODEL", "APP_TIMEZONE_OFFSET_HOURS", "TRACE_PATH",
]
ENV_FILE_CANDIDATES = [Path("/tmp/v4_env.json")]

# 工具展示与保存的稳定顺序（SUPPORTED_TOOLS 是 set，迭代序不定；新增工具自动排在尾部）
_TOOL_ORDER = ("record_food", "query_today", "query_food_nutrition", "undo_last")
ALL_TOOLS = _TOOL_ORDER + tuple(sorted(set(SUPPORTED_TOOLS) - set(_TOOL_ORDER)))

app = FastAPI(title="Calorie Agent Console", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")

_SESSIONS: dict[str, dict[str, Any]] = {}
_ENV_FILE_LOADED = False


def _load_env_file() -> None:
    """本地调试便利：把 /tmp/v4_env.json 的键值注入 os.environ（已有值的键不覆盖）。"""
    global _ENV_FILE_LOADED
    if _ENV_FILE_LOADED:
        return
    _ENV_FILE_LOADED = True
    for candidate in ENV_FILE_CANDIDATES:
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key, value in data.items():
            if isinstance(value, str) and key not in os.environ:
                os.environ[key] = value
        break


def _session(request: Request) -> tuple[str, dict[str, Any]]:
    _load_env_file()
    sid = request.cookies.get("cal_console_sid") or ""
    if sid not in _SESSIONS:
        sid = uuid.uuid4().hex
        _SESSIONS[sid] = {"store": FakeStore(), "history": []}
    return sid, _SESSIONS[sid]


# ---------------------------------------------------------------- 渲染小件

def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _json_block(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return f'<pre class="json">{_esc(text)}</pre>'


def _page(title: str, active: str, body: str) -> str:
    nav = "".join(
        f'<a href="{href}"{" class=active" if href == active else ""}>{label}</a>'
        for href, label in [("/", "总览"), ("/config", "配置"), ("/playground", "Playground"), ("/trace", "轨迹"), ("/eval", "评测")]
    )
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        '<title>Calorie Agent 控制台</title>'
        '<link rel="stylesheet" href="/static/style.css"></head><body>'
        f'<header><h1>🔥 Calorie Agent 控制台</h1><nav>{nav}</nav></header>'
        f'<main><h2>{_esc(title)}</h2>{body}</main></body></html>'
    )


def _kv_table(rows: list[tuple[str, str, str]]) -> str:
    cells = "".join(
        f"<tr><td><code>{_esc(k)}</code></td><td>{v}</td><td class='dim'>{_esc(src)}</td></tr>"
        for k, v, src in rows
    )
    return f'<table class="kv"><tr><th>键</th><th>值</th><th>来源</th></tr>{cells}</table>'


# ---------------------------------------------------------------- 路由：总览

@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    config = load_config()
    env_rows = [(key, "已设置" if os.getenv(key) else "（未设置）", "环境变量") for key in ENV_KEYS]
    body = (
        f'<div class="grid">'
        f'<div class="card"><h3>当前配置</h3>{_kv_table([
            ("preset", _esc(config.preset), "config.yaml / env"),
            ("planner", _esc(config.planner), "config.yaml"),
            ("model", _esc(config.model), "config.yaml / env"),
            ("allowed_tools", _esc(", ".join(config.allowed_tools)), "config.yaml"),
            ("timezone_offset_hours", _esc(config.timezone_offset_hours), "config.yaml / env"),
        ])}</div>'
        f'<div class="card"><h3>环境变量（只显示设置与否，不显示值）</h3>{_kv_table(env_rows)}</div>'
        f'<div class="card"><h3>快捷入口</h3>'
        f'<p><a href="/playground">Playground 调试</a>（默认假存储，不发飞书消息）</p>'
        f'<p><a href="/trace">轨迹查看</a>（本地 TRACE_PATH，默认 /tmp/traces.jsonl）</p>'
        f'<p><a href="/eval">评测</a>（harness/eval，FakeStore）</p>'
        f'<p class="dim">线上健康检查：curl 云函数 /health（控制台不直接访问线上）。</p></div>'
        f'</div>'
    )
    return _page("总览", "/", body)


# ---------------------------------------------------------------- 路由：配置

def _field_source(name: str, env_key: str) -> str:
    return f"环境变量 {env_key} 覆盖中" if os.getenv(env_key) else "config.yaml"


@app.get("/config", response_class=HTMLResponse)
def config_page() -> str:
    config = load_config()
    tool_boxes = "".join(
        f'<label class="chip"><input type="checkbox" name="allowed_tools" value="{_esc(t)}"'
        f'{" checked" if t in config.allowed_tools else ""}> {_esc(t)}</label>'
        for t in ALL_TOOLS
    )
    preset_opts = "".join(f'<option value="{p}"{" selected" if p == config.preset else ""}>{p}</option>' for p in PRESETS)
    planner_opts = "".join(f'<option value="{p}"{" selected" if p == config.planner else ""}>{p}</option>' for p in PLANNERS)
    body = (
        # 注意：全部表单用 multipart 编码。Starlette 对无 charset 的 urlencoded 中文按 latin-1 解码会乱码，multipart 无此问题。
        '<form method="post" action="/config/save" enctype="multipart/form-data">'
        '<div class="card"><h3>工程配置（config.yaml，保存后本机立即生效；线上需重新部署）</h3>'
        f'<p><label>preset</label> <select name="preset">{preset_opts}</select> <span class="dim">{_esc(_field_source("preset", "PRESET"))}</span></p>'
        f'<p><label>planner</label> <select name="planner">{planner_opts}</select></p>'
        f'<p><label>model</label> <input name="model" value="{_esc(config.model)}" size="30"> <span class="dim">{_esc(_field_source("model", "DEEPSEEK_MODEL"))}</span></p>'
        f'<p><label>timezone_offset_hours</label> <input name="timezone_offset_hours" value="{_esc(config.timezone_offset_hours)}" size="6"> <span class="dim">{_esc(_field_source("timezone_offset_hours", "APP_TIMEZONE_OFFSET_HOURS"))}</span></p>'
        f'<p><label>allowed_tools</label><br>{tool_boxes}</p>'
        '<p><button type="submit">保存 config.yaml</button> <span class="dim">保存时自动备份 config.yaml.bak</span></p>'
        '</div></form>'
        '<div class="card"><h3>当前文件内容</h3>' + f'<pre class="json">{_esc((ROOT / "config.yaml").read_text(encoding="utf-8"))}</pre>' + '</div>'
    )
    return _page("配置", "/config", body)


@app.post("/config/save")
def config_save(
    preset: str = Form(""),
    planner: str = Form(""),
    model: str = Form(""),
    timezone_offset_hours: str = Form(""),
    allowed_tools: list[str] = Form([]),
) -> Any:
    if preset not in PRESETS:
        return HTMLResponse(_page("配置错误", "/config", f'<p class="err">未知 preset: {_esc(preset)}</p>'), status_code=400)
    if planner not in PLANNERS:
        return HTMLResponse(_page("配置错误", "/config", f'<p class="err">未知 planner: {_esc(planner)}</p>'), status_code=400)
    tools = tuple(t for t in ALL_TOOLS if t in set(allowed_tools))
    if not tools:
        return HTMLResponse(_page("配置错误", "/config", '<p class="err">allowed_tools 不能为空</p>'), status_code=400)
    try:
        offset = float(timezone_offset_hours)
    except ValueError:
        return HTMLResponse(_page("配置错误", "/config", '<p class="err">timezone_offset_hours 必须是数字</p>'), status_code=400)

    path = ROOT / "config.yaml"
    if path.exists():
        backup = ROOT / "config.yaml.bak"
        if not backup.exists():
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    lines = [
        "# 工程配置（secrets 与表 ID 走环境变量，见 REQUIREMENTS.md §5）",
        "# 修改本文件后需重新部署云函数。",
        "",
        f"preset: {preset}",
        f"planner: {planner}",
        f"model: {model.strip() or 'deepseek-v4-flash'}",
        "",
        "# 工具白名单（live 预设生效；shadow 预设有各自的固定范围）",
        "allowed_tools:",
        *(f"  - {t}" for t in tools),
        "",
        "# 本地时区偏移（小时），用于“今天”的口径",
        f"timezone_offset_hours: {offset:g}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    reset_config_cache()
    return RedirectResponse("/config", status_code=303)


# ---------------------------------------------------------------- 路由：Playground

@app.get("/playground", response_class=HTMLResponse)
def playground(request: Request) -> Any:
    sid, session = _session(request)
    resp = HTMLResponse(_playground_page(session, request, ""))
    resp.set_cookie("cal_console_sid", sid)
    return resp


@app.post("/playground/reset")
def playground_reset(request: Request) -> Any:
    sid = request.cookies.get("cal_console_sid") or uuid.uuid4().hex
    _SESSIONS[sid] = {"store": FakeStore(), "history": []}
    resp = RedirectResponse("/playground", status_code=303)
    resp.set_cookie("cal_console_sid", sid)
    return resp


@app.post("/playground")
def playground_run(
    request: Request,
    text: str = Form(""),
    planner: str = Form("deterministic"),
    preset: str = Form("shadow_fakewrite"),
    store_kind: str = Form("fake"),
) -> Any:
    sid, session = _session(request)
    if preset not in PRESETS:
        preset = "shadow_fakewrite"
    if preset == "shadow_fakewrite":
        store_kind = "fake"  # 假写预设语义固定绑内存存储
    elif preset != "live":
        store_kind = "bitable"  # 只读影子看真实数据
    elif store_kind not in {"fake", "bitable"}:
        store_kind = "fake"

    config = dataclasses.replace(load_config(), preset=preset, planner=planner if planner in PLANNERS else "deterministic")
    if store_kind == "bitable":
        from agent.storage import BitableStore

        store = BitableStore(offset_hours=config.timezone_offset_hours)
    else:
        store = session["store"]

    result: dict[str, Any] = {}
    error = ""
    if text.strip():
        started = time.monotonic()
        try:
            result = run(
                {"message_id": f"console-{uuid.uuid4().hex[:12]}", "user_id": "console_user", "chat_id": "console", "text": text},
                store=store,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - 控制台要把异常显示出来而不是崩
            error = f"{type(exc).__name__}: {exc}"
        session["history"].append({
            "text": text, "store": store_kind, "preset": preset, "planner": planner,
            "reply": result.get("reply", "") if result else "",
            "status": result.get("status", "") if result else "exception",
            "seconds": round(time.monotonic() - started, 2),
        })
        session["history"] = session["history"][-20:]
    resp = HTMLResponse(_playground_page(session, request, error, text, planner, preset, store_kind, result))
    resp.set_cookie("cal_console_sid", sid)
    return resp


def _playground_page(
    session: dict[str, Any],
    request: Request,
    error: str,
    text: str = "",
    planner: str = "deterministic",
    preset: str = "shadow_fakewrite",
    store_kind: str = "fake",
    result: dict[str, Any] | None = None,
) -> str:
    planner_opts = "".join(f'<option value="{p}"{" selected" if p == planner else ""}>{p}</option>' for p in PLANNERS)
    preset_opts = "".join(f'<option value="{p}"{" selected" if p == preset else ""}>{p}</option>' for p in PRESETS)

    result_html = ""
    if result:
        guard = result.get("guard_result") or {}
        guard_color = "ok" if guard.get("passed") else "err"
        tools_html = "".join(
            f'<div class="card"><h4>{_esc(item.get("tool"))} <span class="dim">{_esc(item.get("status"))}</span></h4>{_json_block(item.get("data"))}</div>'
            for item in result.get("tool_results", [])
        ) or '<p class="dim">本次没有执行工具</p>'
        result_html = (
            f'<div class="card"><h3>回复 <span class="dim">reply_source={_esc(result.get("reply_source"))} · {result.get("latency_ms", 0)}ms</span></h3>'
            f'<p class="reply">{_esc(result.get("reply"))}</p>'
            f'<p>guard: <span class="{guard_color}">{"通过" if guard.get("passed") else "拦截"}</span>'
            f'{"" if guard.get("passed") else _json_block(guard.get("violations"))}</p>'
            f'<details><summary>plan / policy</summary>{_json_block({"plan": result.get("plan"), "policy_decision": result.get("policy_decision")})}</details></div>'
            f'<h3>工具执行事实</h3>{tools_html}'
            f'{f"<p class=err>异常: {_esc(error)}</p>" if error else ""}'
        )
    elif error:
        result_html = f'<p class="err">异常: {_esc(error)}</p>'

    history_html = "".join(
        f'<tr><td>{_esc(item["text"])}</td><td class="dim">{_esc(item["preset"])}/{_esc(item["store"])}</td>'
        f'<td>{_esc(item["reply"])[:80]}</td><td class="dim">{_esc(item["seconds"])}s</td></tr>'
        for item in reversed(session["history"])
    ) or '<tr><td colspan="4" class="dim">还没有对话</td></tr>'

    body = (
        f'<form method="post" action="/playground" enctype="multipart/form-data">'
        f'<div class="card"><p><input name="text" value="{_esc(text)}" size="60" placeholder="例如：米饭 / 鸡蛋100g / 今天吃了什么 / 撤销上一条"></p>'
        f'<p>planner <select name="planner">{planner_opts}</select>　preset <select name="preset">{preset_opts}</select>　'
        f'store <select name="store_kind"><option value="fake"{" selected" if store_kind == "fake" else ""}>fake(内存)</option>'
        f'<option value="bitable"{" selected" if store_kind == "bitable" else ""}>bitable(真实表格)</option></select>　'
        f'<button type="submit">运行</button>　'
        f'<a class="btn-dim" href="/playground/reset">重置会话存储</a></p>'
        f'<p class="dim">fake=会话内内存存储（可测追问闭环多轮）；bitable=真实多维表格（live 预设会真实写入；'
        f'只读影子预设自动用 bitable）。控制台永远不会发送飞书消息。planner=deepseek 需要 DEEPSEEK_API_KEY。</p>'
        f'</div></form>'
        f'{result_html}'
        f'<h3>会话历史（新→旧）</h3><table class="kv"><tr><th>输入</th><th>预设/存储</th><th>回复(截断)</th><th>耗时</th></tr>{history_html}</table>'
    )
    return _page("Playground", "/playground", body)


# ---------------------------------------------------------------- 桥接 API（DSH 插件调用）

@app.post("/api/agent")
def api_agent(payload: dict) -> Any:
    """DSH calorie_agent 工具的对接点：一条用户文本 -> 完整 agent 图运行结果。

    请求: {text, planner?, preset?, store_kind?}；回复只返回给调用方，绝不发送飞书。
    默认 preset=live + 真实表格（与飞书同口径）；preset=shadow_fakewrite 强制内存存储。
    """
    _load_env_file()
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"status": "error", "reply": "", "errors": ["text is required"]}
    preset = str(payload.get("preset") or "live")
    if preset not in PRESETS:
        return {"status": "error", "reply": "", "errors": [f"unknown preset: {preset}"]}
    planner = str(payload.get("planner") or load_config().planner)
    if planner not in PLANNERS:
        planner = "deterministic"
    store_kind = str(payload.get("store_kind") or "")
    if preset == "shadow_fakewrite":
        store_kind = "fake"
    elif preset != "live":
        store_kind = "bitable"
    elif store_kind not in {"fake", "bitable"}:
        store_kind = "bitable"

    config = dataclasses.replace(load_config(), preset=preset, planner=planner)
    if store_kind == "bitable":
        from agent.storage import BitableStore

        store = BitableStore(offset_hours=config.timezone_offset_hours)
    else:
        store = FakeStore()
    try:
        result = run(
            {"message_id": f"dsh-{uuid.uuid4().hex[:12]}", "user_id": "console_user", "chat_id": "dsh", "text": text},
            store=store,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - 异常转结构化错误返回给 DSH 轨迹
        return {"status": "error", "reply": "", "errors": [f"{type(exc).__name__}: {exc}"]}
    # tool_events: DSH 驱动据此追加原生 tool/call + tool/result 会话事件（轨迹卡片）
    tool_events = []
    for index, item in enumerate(result.get("tool_results", [])):
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        status = str(item.get("status") or "")
        tool_events.append({
            "callId": f"call_{index + 1}",
            "name": str(item.get("tool") or "tool"),
            "arguments": {},
            "content": json.dumps(data, ensure_ascii=False, default=str)[:2000],
            "isError": status not in {"ok", "duplicate_skipped"},
        })
    result["tool_events"] = tool_events
    return result


# ---------------------------------------------------------------- 路由：轨迹

@app.get("/trace", response_class=HTMLResponse)
def trace_page(request: Request, q: str = "", limit: int = 50) -> str:
    _load_env_file()
    path = Path(os.getenv("TRACE_PATH") or "/tmp/traces.jsonl")
    entries: list[dict[str, Any]] = []
    note = ""
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
            if q:
                needle = q.lower()
                entries = [e for e in entries if needle in json.dumps(e, ensure_ascii=False).lower()]
            entries = list(reversed(entries[-limit:]))
        except OSError as exc:
            note = f"读取失败: {exc}"
    else:
        note = f"轨迹文件不存在：{path}（本地跑 Playground 或 cli 后生成）"

    rows = "".join(
        f'<details><summary><code>{_esc(e.get("trace_id", "")[:12])}</code>　{_esc(e.get("raw_text", ""))[:40]}'
        f'　<span class="dim">{_esc(e.get("preset"))}/{_esc(e.get("planner"))} · {_esc(e.get("created_at", ""))[11:19]} · {_esc(e.get("latency_ms", 0))}ms</span></summary>'
        f'{_json_block(e)}</details>'
        for e in entries
    ) or f'<p class="dim">{_esc(note or "暂无轨迹")}</p>'

    body = (
        f'<form method="get" action="/trace"><p><input name="q" value="{_esc(q)}" size="40" placeholder="关键字过滤（文本/工具/trace_id）"> '
        f'<button type="submit">过滤</button> <span class="dim">文件：{_esc(path)}，共显示最新 {len(entries)} 条</span></p></form>'
        f'{rows}'
    )
    return _page("轨迹（calorie_agent_trace.v1）", "/trace", body)


# ---------------------------------------------------------------- 路由：评测

@app.get("/eval", response_class=HTMLResponse)
def eval_page() -> str:
    return _page("评测（harness/eval，FakeStore，不碰真实表格）", "/eval", _eval_form(""))


def _eval_form(output: str, exit_code: int | None = None, seconds: float = 0.0) -> str:
    tail = ""
    if exit_code is not None:
        color = "ok" if exit_code == 0 else "err"
        tail = f'<p>结果：<span class="{color}">{"全部通过" if exit_code == 0 else "有用例失败"}</span>（耗时 {seconds:.1f}s，退出码 {exit_code}）</p>'
    return (
        '<form method="post" action="/eval/run" enctype="multipart/form-data">'
        '<p><button name="scope" value="smoke">跑 smoke</button> '
        '<button name="scope" value="full">跑全量</button> '
        '<input name="case" size="10" placeholder="单用例 ID 如 T05"> <button name="scope" value="case">跑单用例</button></p></form>'
        f'{tail}<pre class="json">{_esc(output)}</pre>'
    )


@app.post("/eval/run")
def eval_run(scope: str = Form("smoke"), case: str = Form("")) -> Any:
    args = [sys.executable, str(ROOT / "harness" / "eval" / "runner.py")]
    if scope == "smoke":
        args.append("--smoke")
    elif scope == "case":
        case_id = case.strip()
        if not case_id:
            return HTMLResponse(_page("评测", "/eval", _eval_form("请填写用例 ID")), status_code=400)
        args += ["--case", case_id]
    started = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, cwd=ROOT, timeout=180)
        output = (proc.stdout or "") + (proc.stderr or "")
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        output, exit_code = "评测超时（>180s）", 1
    return HTMLResponse(_page("评测", "/eval", _eval_form(output, exit_code, time.monotonic() - started)))


if __name__ == "__main__":
    import uvicorn

    print("Calorie Agent 控制台: http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
