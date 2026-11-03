"""Single source of native tool descriptions, schemas, risk and execution."""
from contextvars import ContextVar
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from langchain_core.tools import StructuredTool
from . import meals, settings
from .reads import run_query_knowledge
from ..storage.timeutil import round_number

CURRENT_RUNTIME = ContextVar("calorie_runtime")

class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Item(Args):
    food_query: str = Field(description="完整食物名称，保留种类、生熟、品牌和配料特征；数量及单位规格（如只(中)、小碗）单独放quantity/unit，不拼进食物名")
    grams: float | None = Field(None, gt=0, description="用户明确的可食克重")
    quantity: float | None = Field(None, gt=0, description="用户原文数量；同时传unit")
    unit: str = Field("", description="与quantity一起传用户原文单位，如个、只(中)；工具读取已有资料换算")
    quantity_text: str = ""
    pending_id: str | None = None
    choice: int | None = Field(None, ge=1, description="该未完项已展示候选的编号，从1起")

class Reuse(Args):
    favorite: str = ""
    date: str = ""
    meal_type: str = ""
    latest: bool = False
    record_ids: list[str] = []
    ratio: float = Field(1, gt=0)

class Write(Args):
    mode: Literal["execute", "propose", "confirm", "cancel"] = Field("execute", description="execute立即办理；propose先呈现待确认建议；confirm执行紧邻提案；cancel取消提案")

class LedgerFinish(Args):
    finish_after_tools: bool = Field(False, description="本批结果已足以回应整条用户请求（含必要缺项问询）时显式设true；本批每个调用均需声明。仍需后续查询、改量、解释、知识或建议时不能结束；默认false，继续原生循环。这是批次控制信息，不是业务参数。")

class Record(Write, LedgerFinish):
    items: list[Item] = []
    date: str = ""
    meal_type: Literal["unknown", "breakfast", "lunch", "dinner", "snack"] | None = None
    reuse: Reuse | None = None
    cancel_pending: list[str] = []

class Revise(Write, LedgerFinish):
    record_id: str = ""
    food_query: str = ""
    delete: bool = False
    grams: float | None = Field(None, gt=0)
    quantity: float | None = Field(None, gt=0)
    unit: str = ""
    ratio: float | None = Field(None, gt=0)

class TargetValues(Args):
    kcal: float | None = Field(None, gt=0)
    carbs: float | None = Field(None, gt=0)
    protein: float | None = Field(None, gt=0)
    fat: float | None = Field(None, gt=0)

class PortionValues(Args):
    food_query: str
    unit: str
    eat_weight: float = Field(gt=0)

class FavoriteValues(Args):
    name: str
    record_ids: list[str] = []
    remove: bool = False

class ReminderValues(Args):
    enabled: bool | None = None
    meal_type: Literal["breakfast","lunch","dinner","snack"] | None = None
    time: str | None = None
    weekdays: list[int] | None = None
    rule_id: str | None = None

class Settings(Write):
    kind: Literal["targets", "preferences", "portion", "favorite", "reminder"]
    scope: Literal["long_term", "today"] = "long_term"
    values: TargetValues | PortionValues | FavoriteValues | ReminderValues | dict = Field(description="与kind对应的本次指定字段；preferences为偏好键值")

    @model_validator(mode="after")
    def validate_values(self):
        raw = self.values.model_dump(exclude_none=True, exclude_unset=True) if isinstance(self.values,BaseModel) else self.values
        schemas={"targets":TargetValues,"portion":PortionValues,"favorite":FavoriteValues,"reminder":ReminderValues}
        self.values=schemas[self.kind].model_validate(raw).model_dump(exclude_none=True,exclude_unset=True) if self.kind in schemas else raw
        return self

class Query(LedgerFinish):
    detail: Literal["calories","summary","items"] = Field("calories", description="只问热量用calories；宏量/目标/剩余用summary；逐项明细用items")

class History(LedgerFinish):
    detail: Literal["summary", "daily", "items"] = Field("summary", description="范围合计用summary；逐日合计用daily（无记录日标记未记录）；历史修改、复用或明细定位直接用items，一次取得完整匹配项及record_id")
    start_date: str = ""
    end_date: str = ""
    meal_type: str = ""
    food_query: str = ""
    period: Literal["today", "yesterday", "this_week", "last_week", "last_7_days", "last_30_days"] = "last_7_days"

class Nutrition(Args):
    food_query: str = ""
    record_id: str = ""
    grams: float | None = Field(None, gt=0)
    quantity: float | None = Field(None, gt=0)
    unit: str = ""

class Knowledge(Args):
    query: str

class Calculate(Args):
    items: list[Item]

def query_today(rt, p):
    lean = p.get("detail","calories") == "calories"
    summary = rt.store.today_summary(rt.user_id, calories_only=lean)
    estimated = (any(row["snapshot"]["estimated"] for row in summary["items"])
                 if all(isinstance(row.get("snapshot", {}).get("estimated"), bool) for row in summary["items"]) else None)
    if lean:
        rt.add_refs(summary["items"])
        return {"status":"ok","data":{"date":summary["date"],"count":summary["count"],"total":{"kcal":summary["totals"]["kcal"]},"items":[{**{k:v for k,v in i.items() if k not in {"snapshot","nutrition"}},"nutrition":{"kcal":i["nutrition"]["kcal"]}} for i in summary["items"]],"detail":"calories","estimated":estimated}}
    target = rt.store.get_standard(rt.user_id)
    rt.add_refs(summary["items"])
    return {"status": "ok", "data": {"date": summary["date"], "items": summary["items"], "total": summary["totals"],
        "count": summary["count"], "detail": p.get("detail", "calories"), "estimated": estimated, "targets": target,
        "remaining": {k: round_number(target[k]-summary["totals"][k]) for k in meals.NUTRIENTS}}}

def query_history(rt, p):
    from datetime import date, timedelta
    today = date.fromisoformat(rt.day)
    period = p.get("period", "last_7_days")
    start = today - timedelta(days=6)
    end = today + timedelta(days=1)
    if period == "today": start = today
    elif period == "yesterday": start,end = today-timedelta(days=1),today
    elif period == "this_week": start = today-timedelta(days=today.weekday())
    elif period == "last_week": end=today-timedelta(days=today.weekday());start=end-timedelta(days=7)
    elif period == "last_30_days": start=today-timedelta(days=29)
    start = meals.resolve_date(p["start_date"], rt.day) if p.get("start_date") else start.isoformat()
    end = meals.resolve_date(p["end_date"], rt.day) if p.get("end_date") else end.isoformat()
    rows = rt.store.intake_items(rt.user_id, start, end)
    if p.get("meal_type"): rows=[r for r in rows if r["meal_type"]==p["meal_type"]]
    if p.get("food_query"): rows=[r for r in rows if p["food_query"] in r["food_name"]]
    rt.add_refs(rows)
    detail = p.get("detail", "summary")
    data = {"start_date": start, "end_date": end, "items": rows, "count": len(rows), "total": meals.totals(rows), "detail": detail}
    data.update({key: p[key] for key in ("meal_type", "food_query") if p.get(key)})
    if detail == "daily":
        grouped = {}
        for row in rows:
            grouped.setdefault(row["date"], []).append(row)
        daily = []
        current, stop = date.fromisoformat(start), date.fromisoformat(end)
        while current < stop:
            day = current.isoformat()
            items = grouped.get(day, [])
            daily.append({"date": day, "count": len(items), "total": meals.totals(items), "status": "recorded" if items else "unrecorded"})
            current += timedelta(days=1)
        data["daily"] = daily
    return {"status": "ok", "data": data}

def query_nutrition(rt, p):
    rows = []
    if p.get("record_id"):
        rows = meals.select_rows(rt, [p["record_id"]])
        if not rows:
            return {"status": "pending", "data": {"items": [], "question": "该摄入项已不可用，请重新指定"}}
    elif p.get("grams") is None and p.get("quantity") is None:
        from datetime import date, timedelta
        end = (date.fromisoformat(rt.day) + timedelta(days=1)).isoformat()
        rows = [r for r in rt.store.intake_items(rt.user_id, rt.day, end) if r["food_name"] == p.get("food_query")]
    if rows:
        rt.add_refs(rows)
        return {"status": "ok", "data": {"items": rows, "total": meals.totals(rows), "source": "intake_snapshot"}}
    item, missing = meals.resolve_item(rt, {**p, "grams": p.get("grams") or (None if p.get("quantity") else 100)})
    return {"status": "pending" if missing else "ok", "data": {"items": [item] if item else [], "unfinished": [missing] if missing else []}}

def query_knowledge(rt, p):
    result = run_query_knowledge(rt, p)
    for index, entry in enumerate(result.get("data", {}).get("entries", []), 1):
        entry["source_id"] = f"K{rt.current['index'] * 100 + index}"
    return result

# These are real native tool objects. Runtime binds only their execution context.
def entry(name, schema, description, risk, runner):
    def invoke(**kwargs):
        params = schema.model_validate(kwargs).model_dump(exclude_none=True, exclude_unset=True)
        params.pop("finish_after_tools", None)
        return runner(CURRENT_RUNTIME.get(), params)
    return StructuredTool.from_function(invoke, name=name, description=description,
        args_schema=schema, metadata={"risk": risk})

TOOL_REGISTRY = {t.name: t for t in [
    entry("record_meal", Record, "记录整餐多项食物；可确定项先写，其余合并追问。补全传pending_id及新增信息，候选choice绑定已展示快照；支持cancel_pending及历史/常用餐reuse和ratio。未给餐次用unknown。只有报告已食用或明确要求记录才执行，用户明确请求记录使用execute；待讨论的写入建议使用propose。", "write", meals.record_meal),
    entry("revise_intake", Revise, "按已返回的record_id改量或软删除指定摄入项；无ID时从本会话刚才那餐按food_query定位。目标不唯一先询问，明确修改/撤销直接execute。", "destructive", meals.revise_intake),
    entry("query_today", Query, "查询今日有效摄入。默认calories给出完整热量合计，足够直接回答只问热量的请求。用户需要宏量、目标或剩余时用summary，需要逐项明细时用items；返回可操作record_id。", "read_only", query_today),
    entry("query_history", History, "查询指定日期/时间窗和餐次的有效摄入。默认summary仅给范围、项数和准确合计；daily给范围内每日合计，unrecorded表示未记录而非未进食；历史改量、复用和明细直接用items取得完整匹配快照及record_id，无需先查summary。end_date不含尾日。", "read_only", query_history),
    entry("query_food_nutrition", Nutrition, "查询指定摄入快照或一般食物营养及份量。", "read_only", query_nutrition),
    entry("query_knowledge", Knowledge, "检索营养知识，entries按source_id提供对应source出处及answer原文证据。回答中的食物份量、比例和营养数字取自检索原文；资料未覆盖的部分可提供一般定性解释，并简短说明具体证据缺口。", "read_only", query_knowledge),
    entry("calculate_meal", Calculate, "计算方案逐项营养、合计与当前目标/摄入的预算差额。替换食物或份量必须重算后展示。", "read_only", meals.calculate_meal),
    entry("update_user_settings", Settings, "更新明确指定的目标、偏好、常用份量、命名常用餐或提醒。目标默认长期，只改指定字段；提醒默认关闭，缺时间/餐次/本人飞书会话时不启用。主动写入建议用propose，只有紧邻请求确认的提案可confirm。", "write", settings.update_user_settings),
]}

def get_tool(name):
    return TOOL_REGISTRY.get(name)
