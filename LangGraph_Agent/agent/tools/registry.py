"""Single source of native tool descriptions, schemas, risk and execution."""
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from langchain_core.tools import StructuredTool
from . import meals, settings
from .reads import run_query_knowledge
from ..storage.timeutil import round_number

CURRENT_RUNTIME = ContextVar("calorie_runtime")

class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ToolArgs(Args):
    action_note: Any = Field(None, description="飞书长任务首批任一调用可给一句具体未来行动；首批均遗漏时仅首个后续含calculate_meal/query_knowledge批次再给一次机会。普通账本省略，不声称已完成或已保存。运行时独立处理，不属于业务参数；省略或非文本不影响工具执行。")

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

class Write(ToolArgs):
    mode: Literal["execute", "propose", "confirm", "cancel"] = Field("execute", description="execute立即办理；propose先呈现待确认建议；confirm执行紧邻提案；cancel取消提案")

class LedgerFinish(ToolArgs):
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
    detail: Literal["summary", "daily", "items"] | None = Field(None, description="范围合计默认summary；逐日合计用daily（无记录日标记未记录）；历史修改、复用或明细定位直接用items，一次取得完整匹配项及record_id")
    start_date: str | None = None
    end_date: str | None = None
    meal_type: str | None = None
    food_query: str | None = None
    period: Literal["today", "yesterday", "this_week", "last_week", "last_7_days", "last_30_days"] | None = None
    continue_scope: bool = Field(False, description="追问同一历史范围时设true，省略的日期、餐次及食物筛选沿用上次；显式空meal_type/food_query清除对应筛选。新查询默认false")

class NutritionQuery(Args):
    food_query: str = ""
    record_id: str = ""
    grams: float | None = Field(None, gt=0)
    quantity: float | None = Field(None, gt=0)
    unit: str = ""
    source_ref: str = Field("", description="本会话营养查询或计划实际返回的食物来源引用；不能用记餐pending_id或choice代替")
    choice: int | None = Field(None, ge=1, description="最近已展示的营养查询选项编号；独立于记餐待办")

class Nutrition(NutritionQuery, ToolArgs):
    queries: list[NutritionQuery] | None = Field(None, min_length=1, max_length=8,
        description="一次查询1至8项食物，按序返回每项真实结果和source_ref；菜单所需来源优先批量查询，保留后续calculate_meal预算。不得同时填写顶层单项定位或份量；action_note只放顶层")

    @model_validator(mode="after")
    def exclusive_query_mode(self):
        if self.queries is not None and any(getattr(self, key) not in (None, "")
                for key in NutritionQuery.model_fields):
            raise ValueError("queries批量查询不能同时指定顶层单项定位或份量，请将单项字段放入queries元素")
        return self

class Knowledge(ToolArgs):
    query: str

class PlanItem(Args):
    item_id: str = Field("", description="草案餐项身份；替换时引用最近已展示草案item_id")
    source_ref: str = Field("", description="选择实际返回的食物来源身份；给出本字段时省略food_query，来源原名（含品牌、生熟）由工具填写，营养/单位只能取该来源")
    food_query: str = Field("", description="没有source_ref时用于查找食物；已有source_ref请省略，不要另写去品牌、去配料或自改生熟的名称")
    choice: int | None = Field(None, ge=1, description="营养查询已展示选项编号，不接受记餐选择号")
    grams: float | None = Field(None, gt=0, description="每份可食克重；建议或假设须通过portion_basis标明")
    quantity: float | None = Field(None, gt=0)
    unit: str = ""
    quantity_text: str = ""
    group: str = Field("", description="菜单分组，如早餐/午餐/晚餐")
    servings: float = Field(1, gt=0, description="该餐项重复份数，工具只乘一次")
    portion_basis: Literal["user_given", "suggested", "assumed_takeout"] = Field("user_given", description="用户给量、模型建议量、外卖组成假设分别标注；不得把排除配料视为已核实的配方")
    assumption: str = Field("", description="份量或组成尚未核实的明确前提")
    fixed: bool = Field(False, description="固定餐组成项；按来源、每份量及servings先计算固定小计，自动比较调整时不得改动；后续仅在用户明确指定该项时修改")
    max_grams: float | None = Field(None, gt=0, description="用户明确的该项每份可食克重上限；替代方案不得突破，省略表示无明确上限")

class PlanAdjustment(Args):
    replacements: list[PlanItem] = Field(default_factory=list, description="同一初稿内一个可行调整：按item_id改非固定项，未指定字段和其他项保留；同食物改量复用source_ref")
    additions: list[PlanItem] = Field(default_factory=list, description="这个调整中补充的新项，须有唯一item_id和实际食物来源；不得重新列固定餐")

class Calculate(LedgerFinish):
    items: list[PlanItem] | None = None
    scope: Literal["daily", "full_day", "remaining"] | None = Field(None, description="daily日常菜单用长期目标不扣今日摄入；full_day今日完整菜单用当日目标不扣已吃；remaining今天剩余用当日目标扣一次有效摄入；旧调用省略沿用remaining，改稿省略沿用草案范围")
    date: str | None = None
    constraints: list[str] | None = Field(None, description="需要后续修改保留的固定餐、排除配料、烹饪条件和偏好；省略沿用草案条件")
    tolerance_kcal: float | None = Field(None, ge=0)
    draft_id: str = Field("", description="接续最近实际展示草案的draft_id；通过replacements替换指定item_id，其余保留")
    replacements: list[PlanItem] | None = None
    adjustment: PlanAdjustment | None = Field(None, description="可选另一初稿，与原稿在同一次工具内计算选择；工具还会按缺口校正已有suggested份量。改已展示草案时不得使用，避免扩大用户指定的修改范围")
    portion_preference: Literal["smaller_portions"] | None = Field(None, description="用户要求单项食物不宜过多时指定；宏量规则并列后比较非固定项最大每份克重，再比较备餐项数，仍并列保留初稿")

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
    if p.get("continue_scope"):
        previous = rt.session.get("history_scope", {})
        inherited = {key: value for key, value in previous.items() if key in {"start_date", "end_date", "meal_type", "food_query", "detail"}}
        if "period" in p:
            inherited.pop("start_date", None)
            inherited.pop("end_date", None)
        p = {**inherited, **p}
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
    scope = {"start_date": start, "end_date": end, "meal_type": p.get("meal_type", ""), "food_query": p.get("food_query", ""), "detail": detail}
    data["filters"] = {key: scope[key] for key in ("meal_type", "food_query")}
    rt.session["history_scope"] = deepcopy(scope)
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
    from ..planning import query_food_nutrition
    return query_food_nutrition(rt, p)


def query_knowledge(rt, p):
    result = run_query_knowledge(rt, p)
    from ..evidence import assign_evidence
    data = result.get("data", {})
    data["entries"] = assign_evidence(rt, data.get("entries", []))
    return result

# These are real native tool objects. Runtime binds only their execution context.
def entry(name, schema, description, risk, runner):
    def invoke(**kwargs):
        params = schema.model_validate(kwargs).model_dump(exclude_none=True, exclude_unset=True)
        params.pop("finish_after_tools", None)
        params.pop("action_note", None)
        return runner(CURRENT_RUNTIME.get(), params)
    return StructuredTool.from_function(invoke, name=name, description=description,
        args_schema=schema, metadata={"risk": risk})

TOOL_REGISTRY = {t.name: t for t in [
    entry("record_meal", Record, "记录整餐多项食物；可确定项先写，其余合并追问。补全传pending_id及新增信息，候选choice绑定已展示快照；支持cancel_pending及历史/常用餐reuse和ratio。未给餐次用unknown。只有报告已食用或明确要求记录才执行，用户明确请求记录使用execute；待讨论的写入建议使用propose。", "write", meals.record_meal),
    entry("revise_intake", Revise, "按已返回的record_id改量或软删除指定摄入项。用户说刚才那餐且last_meal内食物唯一时，直接传该record_id或food_query执行，不先查历史；今天某餐未指明哪一份且可能多餐时先定位消歧。目标不唯一先询问，明确修改/撤销直接execute。", "destructive", meals.revise_intake),
    entry("query_today", Query, "查询今日有效摄入。默认calories给出完整热量合计，足够直接回答只问热量的请求。用户需要宏量、目标或剩余时用summary，需要逐项明细时用items；返回可操作record_id。", "read_only", query_today),
    entry("query_history", History, "查询指定日期/时间窗和餐次的有效摄入。默认summary仅给范围、项数和准确合计；daily给范围内每日合计，unrecorded表示未记录而非未进食；历史改量、复用和明细直接用items取得完整匹配快照及record_id，无需先查summary。end_date不含尾日。", "read_only", query_history),
    entry("query_food_nutrition", Nutrition, "查询指定摄入快照或一般食物营养及份量。菜单需多种来源时用queries一次批量查1至8项，保留后续核算预算；批量和顶层单项字段互斥。逐项返回可辨认候选及会话专属source_ref，后续营养查询和calculate_meal可复用；choice仅绑定最近实际展示的营养选项，不与记餐候选混用。批量公开选项仅绑定首个尚未解决查询，其余结果按原请求顺序保留。", "read_only", query_nutrition),
    entry("query_knowledge", Knowledge, "检索营养知识，entries按source_id提供对应source出处及answer原文证据。回答中的食物份量、比例和营养数字取自检索原文；资料未覆盖的部分可提供一般定性解释，并简短说明具体证据缺口。", "read_only", query_knowledge),
    entry("calculate_meal", Calculate, "核算可接续的饮食菜单，不写摄入。明确scope：daily日常目标不扣今天，full_day当日完整菜单不扣已吃，remaining当日剩余扣一次有效摄入。items标明fixed固定餐、来源、每份量、servings、group、portion_basis及假设；先算固定小计及四项补充缺口。模型选食材，建议量标suggested，工具在同次计算内按缺口校正已有一项或两项建议量，保留固定项/用户量/份数/上限；可用adjustment提供另一初稿，按热量容差、宏量偏差及份量偏好选一版，不查新来源或加新食材。缺整餐配方可按有来源组成及明确假设克重拆分。替换用最近已展示draft_id与replacements.item_id，仅改指定项并保留其他项和限制；改稿不能另加adjustment扩大范围。纯改稿已完整满足整条请求时设finish_after_tools=true，计算后直接交付；初次规划或还需解释/建议时继续原循环。只有完整计入且热量差额在tolerance_kcal内才满足容差。", "read_only", meals.calculate_meal),
    entry("update_user_settings", Settings, "更新明确指定的目标、偏好、常用份量、命名常用餐或提醒。目标默认长期，只改指定字段；提醒默认关闭，缺时间/餐次/本人飞书会话时不启用。主动写入建议用propose，只有紧邻请求确认的提案可confirm。", "write", settings.update_user_settings),
]}

def get_tool(name):
    return TOOL_REGISTRY.get(name)
