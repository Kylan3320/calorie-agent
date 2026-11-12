"""Single source of native tool descriptions, schemas, risk and execution."""
from contextvars import ContextVar
from copy import deepcopy
from datetime import date, time
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from langchain_core.tools import StructuredTool
from . import meals, settings
from .reads import run_query_knowledge
from .portions import PortionEvidence, PortionWeb
from ..storage import diet_goals, memory
from ..storage.timeutil import round_number

CURRENT_RUNTIME = ContextVar("calorie_runtime")

class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ToolArgs(Args):
    action_note: Any = Field(None, description="若即将执行的批次仍需明显等待，可给一句具体的下一步行动说明。快路径或已说明时省略。")

class Item(Args):
    food_query: str = Field("", description="完整食物名称，保留种类、生熟、品牌和规格。已有核实的同会话营养引用时可省略。")
    source_ref: str = Field("", description="本会话实际返回的营养来源引用，与候选编号互斥")
    grams: float | None = Field(None, gt=0, description="用户明确的可食克重")
    quantity: float | None = Field(None, gt=0, description="用户原文数量；同时传unit")
    unit: str = Field("", description="与quantity一起传用户原文单位，如个、只(中)；工具读取已有资料换算")
    quantity_text: str = ""
    pending_id: str | None = None
    choice: int | None = Field(None, ge=1, description="该未完项已展示候选的编号，从1起")
    portion_evidence: PortionEvidence | None = Field(None, description="本轮网页证实的份量，仍传用户的quantity/unit，不冒充用户给定的grams")

    @model_validator(mode="after")
    def source_and_portion(self):
        if not self.food_query and not self.source_ref:
            raise ValueError("请提供食物名称或营养来源引用")
        if self.choice is not None and self.source_ref:
            raise ValueError("营养来源引用与待办候选编号互斥")
        if self.grams is not None and self.portion_evidence is not None:
            raise ValueError("用户给定克重与网页份量证据互斥")
        return self

class Reuse(Args):
    favorite: str = ""
    date: str = ""
    meal_type: str = ""
    latest: bool = False
    record_ids: list[str] = []
    ratio: float = Field(1, gt=0)

class Write(ToolArgs):
    mode: Literal["execute", "propose", "confirm", "cancel"] = Field("execute", description="execute立即办理；propose先呈现待确认建议；confirm执行紧邻提案；cancel取消提案")
    proposal_id: str = Field("", description="confirm/cancel使用刚展示的提案ID")
    authorization_quote: str = Field("", max_length=500, description="确认、取消或明确改写待确认动作时，引用当前用户授权的原话")

class LedgerFinish(ToolArgs):
    finish_after_tools: bool = Field(False, description="本批结果已足以回应整条用户请求（含必要缺项问询）时显式设true；本批每个调用均需声明。仍需后续查询、改量、解释、知识或建议时不能结束；默认false，继续原生循环。这是批次控制信息，不是业务参数。")

class Record(Write, LedgerFinish):
    items: list[Item] = []
    date: str = ""
    meal_type: Literal["unknown", "breakfast", "lunch", "dinner", "snack"] | None = None
    reuse: Reuse | None = None
    cancel_pending: list[str] = []
    allow_estimate: bool = Field(True, description="用户本轮要求精确或不估算时传false")

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

class ConditionValues(Args):
    operation: Literal["save", "revise", "end"]
    ref: str = ""
    quote: str = Field("", max_length=800, description="当前用户原话中声明、修订或结束该条件的原文片段")
    text: str = Field("", max_length=240, description="明确条件的简述")
    period: Literal["today", "this_week"] | None = None
    starts: str = ""
    ends: str = ""
    draft_id: str = ""

    @model_validator(mode="after")
    def required_fields(self):
        if (self.operation == "save") == bool(self.ref):
            raise ValueError("only a new condition omits ref")
        if self.operation != "end" and not (self.quote.strip() and self.text.strip()):
            raise ValueError("a condition requires the user's quote and its meaning")
        return self

class FeedbackValues(Args):
    operation: Literal["feedback"] = "feedback"
    ref: str = Field(description="实际已读且本次需要停用的经验 note 引用")
    signal: Literal["reject", "correct"]
    quote: str = Field(min_length=1, max_length=800, description="当前消息明确否定或纠正已发布经验的逐字原话")

class WithdrawValues(Args):
    operation: Literal["withdraw"] = "withdraw"
    ref: str = Field(description="要停止使用的现行记忆或设置引用")

class Settings(Write):
    kind: Literal["targets", "preferences", "portion", "favorite", "reminder", "condition", "feedback", "memory"]
    scope: Literal["long_term", "today"] = "long_term"
    values: TargetValues | PortionValues | FavoriteValues | ReminderValues | ConditionValues | FeedbackValues | WithdrawValues | dict = Field(default_factory=dict, description="与kind对应的本次指定字段；确认或取消提案时省略")

    @model_validator(mode="after")
    def validate_values(self):
        raw = self.values.model_dump(exclude_none=True, exclude_unset=True) if isinstance(self.values,BaseModel) else self.values
        if self.mode in {"confirm", "cancel"} and not raw:
            self.values = {}
            return self
        schemas={"targets":TargetValues,"portion":PortionValues,"favorite":FavoriteValues,"reminder":ReminderValues,
                 "condition":ConditionValues,"feedback":FeedbackValues,"memory":WithdrawValues}
        self.values=schemas[self.kind].model_validate(raw).model_dump(exclude_none=True,exclude_unset=True) if self.kind in schemas else raw
        if self.kind in {"feedback", "memory"}:
            self.values.setdefault("operation", "feedback" if self.kind == "feedback" else "withdraw")
        return self

class MemoryQuery(ToolArgs):
    operation: Literal["search", "read"]
    ref: str = Field("", description="read使用实际返回的版本引用，可沿主题链接继续读取")
    query: str = Field("", max_length=240, description="search按用户原话或主题关键词查询")
    match: Literal["terms", "phrase"] = Field("terms", description="默认按相关词排序；phrase查原句。省略query可以按类型浏览并翻页。")
    kinds: list[Literal["turn", "draft", "condition", "setting", "feedback", "note", "theme"]] = []
    start_date: str = ""
    end_date: str = Field("", description="日期范围不含末日")
    cursor: str = ""
    limit: int = Field(6, ge=1, le=12)

    @model_validator(mode="after")
    def read_ref(self):
        if self.operation == "read" and not self.ref:
            raise ValueError("read requires a memory reference")
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
    allow_estimate: bool | None = Field(None, description="明确要求精确时传false")

class Nutrition(NutritionQuery, ToolArgs):
    portion_web: PortionWeb | None = Field(None, description='查份量：fetch只传operation和urls；search只传operation和query；read_cache只传operation和ref。可另传food_query同时定位食物。')
    queries: list[NutritionQuery] | None = Field(None, min_length=1, max_length=8,
        description="一次查询1至8项食物，按序返回每项真实结果和source_ref；菜单所需来源优先批量查询，保留后续calculate_meal预算。不得同时填写顶层单项定位或份量；action_note只放顶层")

    @model_validator(mode="after")
    def exclusive_query_mode(self):
        if self.portion_web is not None and self.queries is not None:
            raise ValueError("portion_web不能同时使用queries")
        if self.queries is not None and any(getattr(self, key) not in (None, "") for key in NutritionQuery.model_fields):
            raise ValueError("queries批量查询不能同时指定顶层单项定位或份量，请将单项字段放入queries元素")
        if self.portion_web is not None and any(getattr(self, key) not in (None, "")
                for key in ("record_id", "choice")):
            raise ValueError("网页补证不能同时读取账本记录或使用展示候选编号")
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

class DayPlan(Args):
    date: str = Field(description="同一日使用 YYYY-MM-DD")
    scope: Literal["full_day", "remaining"] = "full_day"
    items: list[PlanItem] = Field(min_length=1, max_length=24)
    condition_refs: list[str] | None = Field(None, max_length=12)
    experience_refs: list[str] | None = Field(None, max_length=8)
    tolerance_kcal: float | None = Field(None, ge=0)

class Calculate(LedgerFinish):
    selection_mode: Literal["default", "compare"] = Field("default", description="compare只核算并返回候选，按用户取舍采用。default交付默认结果。")
    candidate_ref: str = Field("", description="采用本轮已算候选；此时省略其他计算参数。")
    use_default_calibration: bool = Field(True, description="是否生成默认调量候选，可关闭以比较原方案。")
    items: list[PlanItem] | None = None
    scope: Literal["daily", "full_day", "remaining"] | None = Field(None, description="daily日常菜单用长期目标不扣今日摄入；full_day今日完整菜单用当日目标不扣已吃；remaining今天剩余用当日目标扣一次有效摄入；旧调用省略沿用remaining，改稿省略沿用草案范围")
    date: str | None = None
    constraints: list[str] | None = Field(None, description="需要后续修改保留的固定餐、排除配料、烹饪条件和偏好；省略沿用草案条件")
    tolerance_kcal: float | None = Field(None, ge=0)
    draft_id: str = Field("", description="接续最近实际展示草案的draft_id；通过replacements替换指定item_id，其余保留")
    draft_ref: str = Field("", description="跨会话改稿使用query_memory读取的已交付版本引用，与draft_id互斥")
    condition_refs: list[str] | None = Field(None, max_length=12, description="本稿实际采用的明确条件引用；引用已有记录时不要再把同一条件写入constraints")
    experience_refs: list[str] | None = Field(None, max_length=8, description="只列实际用于选材、备餐或排序的可用经验引用")
    replacements: list[PlanItem] | None = None
    adjustment: PlanAdjustment | None = Field(None, description="可选另一初稿，与原稿在同一次工具内计算选择；工具还会按缺口校正已有suggested份量。改已展示草案时不得使用，避免扩大用户指定的修改范围")
    portion_preference: Literal["smaller_portions"] | None = Field(None, description="用户要求单项食物不宜过多时指定；宏量规则并列后比较非固定项最大每份克重，再比较备餐项数，仍并列保留初稿")

    @model_validator(mode="after")
    def candidate_only(self):
        controls = {"candidate_ref", "finish_after_tools", "action_note"}
        if self.candidate_ref and any(getattr(self, key) != field.get_default(call_default_factory=True)
                                     for key, field in type(self).model_fields.items() if key not in controls):
            raise ValueError("采用已计算候选时只传candidate_ref和批次控制字段")
        return self


class GoalCalculate(ToolArgs):
    days: list[DayPlan] = Field(min_length=1, max_length=7, description="每个日期独立核算。餐项group用早餐、午餐、晚餐或加餐。")


class GoalQuery(ToolArgs):
    operation: Literal["status", "show_latest", "show_delivered"] = "status"
    goal_id: str = Field("", description="省略时用当前用户的委托")
    view_quote: str = Field("", max_length=600, description="show时给出本条明确要求展示菜单内容的逐字原话。查进度或要求检查不使用show。")


class GoalLock(Args):
    date: str
    meal: Literal["breakfast", "lunch", "dinner", "snack"]

    @field_validator("date")
    @classmethod
    def iso_day(cls, value):
        return date.fromisoformat(value).isoformat()


class GoalSpec(Args):
    objective: str | None = Field(None, min_length=1, max_length=500)
    done_when: str | None = Field(None, min_length=1, max_length=500, description="用户要求的交付或检查标准")
    starts: str | None = Field(None, description="含首日 YYYY-MM-DD")
    ends: str | None = Field(None, description="含尾日 YYYY-MM-DD")
    constraints: list[str] | None = Field(None, max_length=16, description="明确的硬条件；revise带change_dates时本次条件只应用于那些日期")
    preferences: list[str] | None = Field(None, max_length=16)
    meals: list[Literal["breakfast", "lunch", "dinner", "snack"]] | None = Field(None, max_length=4)
    locks: list[GoalLock] | None = Field(None, max_length=28, description="增加这些明确锁定的餐次，保留其他已有锁定")
    triggers: list[Literal["time", "feedback", "intake", "settings"]] | None = Field(None, max_length=4)
    check_time: str | None = Field(None, description="每天的本地时间 HH:MM，只有用户授权定时才传")
    notify_here: bool | None = Field(None, description="用户明确授权在此会话主动通知才传true")
    scheduled_delivery: bool | None = Field(None, description="用户明确授权约定时点交付时为true")
    external_portions: bool | None = Field(None, description="用户明确授权查外部份量资料时为true")

    @field_validator("starts", "ends")
    @classmethod
    def iso_day(cls, value):
        return date.fromisoformat(value).isoformat() if value is not None else None

    @field_validator("check_time")
    @classmethod
    def local_time(cls, value):
        if value is None:
            return None
        parsed = time.fromisoformat(value)
        if parsed.tzinfo or parsed.second or parsed.microsecond:
            raise ValueError("check_time须为本地HH:MM")
        return parsed.isoformat(timespec="minutes")


class ManageGoal(ToolArgs):
    operation: Literal["start", "revise", "request_run", "stop_run", "set_auto", "pause", "resume", "set_notifications", "end"]
    user_quote: str = Field(min_length=1, max_length=600, description="本条用户明确委托、授权改范围或控制的逐字原话")
    goal_id: str = ""
    revision: int | None = Field(None, ge=1, description="start以外传本轮看到的委托版本")
    spec: GoalSpec | None = Field(None, description="start填写范围；revise只给本次修改的字段，列表若修改则传完整值")
    change_dates: list[str] | None = Field(None, min_length=1, max_length=7, description="只修改指定日期时，revise传明确授权的日期；其他日期保持")
    unlock: list[GoalLock] | None = Field(None, max_length=28, description="仅revise，用户明确要求解锁的餐次；已食用或过去餐次仍保持")
    enabled: bool | None = None
    notify_here: bool = False

    @model_validator(mode="after")
    def control_fields(self):
        if self.operation == "start" and (self.spec is None or not self.spec.objective):
            raise ValueError("开始委托须有明确目标")
        if self.operation == "revise" and self.spec is None:
            raise ValueError("修改委托须有变更范围")
        if self.operation in {"set_auto", "set_notifications"} and self.enabled is None:
            raise ValueError("开关操作须给出enabled")
        if self.change_dates:
            if self.operation != "revise":
                raise ValueError("仅修改委托可限定本次变更日期")
            self.change_dates = sorted({date.fromisoformat(day).isoformat() for day in self.change_dates})
        if self.unlock and self.operation != "revise":
            raise ValueError("仅修改委托可明确解除锁定")
        return self


class GoalResult(ToolArgs):
    outcome: Literal["plan", "waiting", "unchanged"]
    selected_drafts: list[str] = Field(default_factory=list, max_length=7, description="本次已计算并实际采用的草案ID，每日最多一个")
    next_step: str = Field(default="", max_length=400)
    waiting_for: str = Field(default="", max_length=400, description="影响后续的具体未决事项和由谁决定")
    reason: str = Field(min_length=1, max_length=400, description="根据实际资料的定性取舍，不写未经核实的数字")
    preparation: str = Field(default="", max_length=400, description="有实际作用的备餐方法变化，省略沿用当前安排")
    notify: bool = Field(default=False, description="本批存在需用户行动的变化、需要决定或已到授权交付时点时才为true")


def submit_goal(rt, params):
    return rt.goal_run.submit(rt, params)


def calculate(rt, params):
    return rt.goal_run.calculate(rt, params)

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
        **({"meal_totals": meals.meal_totals(rt, summary["items"], rows=summary["items"])} if p.get("detail") == "items" else {}),
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
    if rt.goal_run and p.get("portion_web") and not rt.goal_run.goal["spec"].get("external_portions"):
        return {"status": "pending", "data": {"reason": "external_portion_lookup_not_authorized"}}
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
        params.pop("proposal_id", None)
        params.pop("authorization_quote", None)
        return runner(CURRENT_RUNTIME.get(), params)
    return StructuredTool.from_function(invoke, name=name, description=description,
        args_schema=schema, metadata={"risk": risk})

TOOL_REGISTRY = {t.name: t for t in [
    entry("record_meal", Record, "记录整餐多项食物；可确定项先写，其余合并追问。新消息明确记录/复用时新增一餐，同日已有相同餐食也照常新增；系统按原消息ID和固定动作键防重，不按食物/份量防重。核对旧账不调用本工具。补全传pending_id及新增信息，候选choice绑定已展示快照；支持cancel_pending及历史/常用餐reuse和ratio。未给餐次用unknown。只有报告已食用或明确要求记录才执行，用户明确请求记录使用execute；待讨论的写入建议使用propose。", "write", meals.record_meal),
    entry("revise_intake", Revise, "按已返回的record_id改量或软删除指定摄入项。用户说刚才那餐且last_meal内食物唯一时，直接传该record_id或food_query执行，不先查历史；今天某餐未指明哪一份且可能多餐时先定位消歧。目标不唯一先询问，明确修改/撤销直接execute。", "destructive", meals.revise_intake),
    entry("query_today", Query, "查询今日有效摄入。默认calories给出完整热量合计，足够直接回答只问热量的请求。用户需要宏量、目标或剩余时用summary，需要逐项明细时用items；返回可操作record_id。", "read_only", query_today),
    entry("query_history", History, "查询指定日期/时间窗和餐次的有效摄入。默认summary仅给范围、项数和准确合计；daily给范围内每日合计，unrecorded表示未记录而非未进食；历史改量、复用和明细直接用items取得完整匹配快照及record_id，无需先查summary。end_date不含尾日。", "read_only", query_history),
    entry("query_food_nutrition", Nutrition, "查询指定摄入快照或一般食物营养及份量。菜单需多种来源时用queries一次批量查1至8项，保留后续核算预算；批量和顶层单项字段互斥。逐项返回可辨认候选及会话专属source_ref，后续营养查询和calculate_meal可复用；choice仅绑定最近实际展示的营养选项，不与记餐候选混用。批量公开选项仅绑定首个尚未解决查询，其余结果按原请求顺序保留。", "read_only", query_nutrition),
    entry("query_knowledge", Knowledge, "检索用户所问的营养知识，entries按source_id提供知识库条目answer原文及条目标注source出处，不代表已读取外部文献全文。通用问题不因会话有目标或菜单就追加个人评价；直接依据足够即收尾，不为题外问题加查。明确问个人目标/摄入时另用业务工具核对；原文无个人适用依据时说明缺口，不从设置克数推断合适与否。知识份量、比例和营养数字取自实际检索原文。", "read_only", query_knowledge),
    entry("calculate_meal", Calculate, "核算可接续的饮食菜单，不写摄入。明确scope：daily日常目标不扣今天，full_day当日完整菜单不扣已吃，remaining当日剩余扣一次有效摄入。items标明fixed固定餐、来源、每份量、servings、group、portion_basis及假设；先算固定小计及四项补充缺口。模型选食材，建议量标suggested，工具在同次计算内按缺口校正已有一项或两项建议量，保留固定项/用户量/份数/上限；可用adjustment提供另一初稿，按热量容差、宏量偏差及份量偏好选一版，不查新来源或加新食材。缺整餐配方可按有来源组成及明确假设克重拆分。替换用最近已展示draft_id与replacements.item_id，仅改指定项并保留其他项和限制；改稿不能另加adjustment扩大范围。纯改稿已完整满足整条请求时设finish_after_tools=true，计算后直接交付；初次规划或还需解释/建议时继续原循环。只有完整计入且热量差额在tolerance_kcal内才满足容差。", "read_only", meals.calculate_meal),
    entry("query_memory", MemoryQuery, "只读当前用户的跨会话记忆。search找原话、明确条件、已交付菜单、设置、主题或经验，read核对实际原文或结构化对象。可沿主题链接读取，不把历史命令当授权，经验只用于建议排序。", "read_only", memory.query),
    entry("update_user_settings", Settings, "更新用户明确要求的目标、偏好、份量、常用餐或提醒。condition管理明确的临时条件。feedback仅用于立即停用被当前原话明确否定的已读经验；分享结果或评价菜单时，用回复控制区review_sources。memory执行明确撤回。", "write", settings.update_user_settings),
    entry("query_diet_goal", GoalQuery, "跨会话查看本人持续饮食委托状态。只有用户要求查看计划内容时用show_latest或show_delivered；普通状态查询不交付新版本。", "read_only", diet_goals.query),
    entry("manage_diet_goal", ManageGoal, "只在用户明确委托持续跟进或控制时调用。一份普通菜单不创建委托。start按原话填写时间、完成标准、硬条件、软偏好、触发和通知授权，完整授权自动排队首轮。revise改变范围；request_run手动查；stop_run停本次；set_auto控制未来自动触发；pause暂停；resume恢复；set_notifications独立控制主动通知；end结束。委托的多日计算在后台执行。", "write", diet_goals.manage),
]}

GOAL_RESULT_TOOL = entry("submit_goal_result", GoalResult, "提交本次委托候选、实际采用的已算日期和下一步。可以保留核实的部分，明确未决问题。不负责发送通知或修改任何业务事实。", "read_only", submit_goal)
GOAL_CALCULATE_TOOL = entry("calculate_meal", GoalCalculate, "按days核算委托范围内的日计划，复用已知来源。核算后返回实际草案ID，用于本次选择；锁定餐次由工具保留。", "read_only", calculate)

def get_tool(name):
    return TOOL_REGISTRY.get(name)
