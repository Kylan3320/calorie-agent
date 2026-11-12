"""Message-bound evidence for edible portion weights."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..storage import deepseek_web
from .base import positive_grams


class _WebArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _WebSearch(_WebArgs):
    operation: Literal["search"]
    query: str = Field(min_length=1, max_length=128, description="只传公开食物和规格关键词。")


class _WebFetch(_WebArgs):
    operation: Literal["fetch"]
    urls: list[Annotated[str, Field(min_length=1, max_length=2048)]] = Field(
        min_length=1, max_length=3, description="本消息用户给出的公开 URL，或本消息搜索实际返回的 URL。")

    @model_validator(mode="after")
    def distinct_urls(self):
        if len(set(self.urls)) != len(self.urls):
            raise ValueError("读取只接收1至3个不同的urls")
        return self


class _WebCache(_WebArgs):
    operation: Literal["read_cache"]
    ref: str = Field(min_length=1, max_length=64, description="当前工具返回的有效份量缓存引用。")


PortionWeb = Annotated[_WebSearch | _WebFetch | _WebCache, Field(discriminator="operation")]


class PortionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    ref: str = Field(min_length=1, max_length=64, description="本消息实际读取正文或已展开的有效缓存引用")
    quote: str = Field(min_length=1, max_length=500, description="逐字复制可食重量、食物、单位和数量关系所在的完整原句或表格行")
    source_food: str = Field(min_length=1, max_length=80, description="引文原样的食物及规格词")
    source_unit: str = Field(min_length=1, max_length=80, description="引文中的份量单位，不含数量；如 个、medium apple")
    applicability: str = Field(min_length=1, max_length=180, description="说明引文与本项的食物、规格、生熟、可食口径及单位为何一致")
    basis: Literal["edible_unit", "edible_total"]
    weight: float = Field(gt=0, description="引文所列重量，工具按原单位换算")
    weight_unit: Literal["g", "kg", "oz", "lb"]
    count: int | None = Field(None, ge=2, description="edible_total时引文明确给出的总重量对应数量")

    @model_validator(mode="after")
    def count_for_total(self):
        if (self.basis == "edible_total") != (self.count is not None):
            raise ValueError("只有可食总重依据需要引文中的数量")
        return self


def allow_estimate(rt, permitted=True):
    return permitted and rt.settings.get("preferences", {}).get("portion_precision") != "exact"


WEB_REQUEST_LIMIT = 3
_CACHE_LIFETIME = timedelta(days=7)
_USER_URL = re.compile(r"https?://[^\s<>{}\"'`，。！？；、]+", re.IGNORECASE)


def _search_key(query):
    return " ".join(unicodedata.normalize("NFKC", query).casefold().split())


def _web_state(rt):
    attempts, results, pages = [], {}, {}
    for call in rt.journal["calls"]:
        prepared = call.get("prepared", {})
        if "web_requests" in prepared:
            attempts.extend((request["operation"], request["key"]) for request in prepared["web_requests"])
        else:
            # Existing journals may have a reserved search or fetch batch.
            legacy = prepared.get("portion_web", {})
            if legacy.get("operation") == "search":
                query = call.get("args", {}).get("portion_web", {}).get("query", "")
                attempts.append(("search", _search_key(query)))
            elif legacy.get("operation") == "fetch":
                attempts.extend(("fetch", deepseek_web.public_url(url) or url) for url in legacy.get("urls", []))
        result = call.get("result", {}).get("data", {}).get("portion_web", {})
        for row in result.get("results", []):
            if url := deepseek_web.public_url(row.get("url")):
                results[url] = row
        for page in result.get("pages", []):
            if "ref" in page:
                pages[page["ref"]] = page
    return attempts, results, pages


def web_requests_remaining(rt):
    return max(0, WEB_REQUEST_LIMIT - len(_web_state(rt)[0]) - rt.journal.get('general_web_requests',0))


def _user_urls(rt):
    urls = set()
    for raw in _USER_URL.findall(rt.message["text"]):
        trimmed = raw.rstrip(".,;:!?）】」』")
        while trimmed.endswith(")") and trimmed.count(")") > trimmed.count("("):
            trimmed = trimmed[:-1]
        while trimmed.endswith("]") and trimmed.count("]") > trimmed.count("["):
            trimmed = trimmed[:-1]
        urls.update(url for value in (raw, trimmed) if (url := deepseek_web.public_url(value)))
    return urls


def _reserve_web_request(rt, operation, key):
    rt.check_deadline()
    if web_requests_remaining(rt) == 0:
        raise deepseek_web.WebLookupError("web_request_budget_exhausted")
    requests = (rt.prepared or {}).get("web_requests", [])
    rt.prepare({"web_requests": [*requests, {"operation": operation, "key": key}]})


def _fresh(evidence):
    if not evidence:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(evidence["fetched_at"])
    return timedelta(0) <= age <= _CACHE_LIFETIME


def portion_cache_refs(rt, food, unit):
    entries = rt.store.load_document("portion_cache_index", rt.user_id, food["food_id"]).get("entries", [])
    return [entry for entry in entries if _fresh(entry) and _normalized(entry["unit"]) == _normalized(unit)]


def cache_portion_evidence(rt, evidence):
    """Keep only evidence that actually supported a committed intake."""
    if not rt.preset.allow_write or not _fresh(evidence) or evidence["ref"].startswith("portion-cache:"):
        return
    ref = "portion-cache:" + hashlib.sha256(json.dumps([rt.user_id, evidence["ref"], evidence["food_id"],
        evidence["unit"], evidence["quote"]], ensure_ascii=False).encode()).hexdigest()[:24]
    source = {key: value for key, value in evidence.items() if key not in {"quantity", "adopted_grams"}}
    rt.store.save_document("portion_cache", rt.user_id, ref, {**source, "ref": ref})
    index = rt.store.load_document("portion_cache_index", rt.user_id, evidence["food_id"])
    entries = [entry for entry in index.get("entries", []) if entry["ref"] != ref and _fresh(entry)]
    metadata = {"ref": ref, **{key: evidence[key] for key in ("title", "url", "unit", "fetched_at")}}
    rt.store.save_document("portion_cache_index", rt.user_id, evidence["food_id"], {"entries": [metadata, *entries][:8]})


def query_web(rt, p):
    operation = p["operation"]
    attempts, results, pages_by_ref = _web_state(rt)

    def outcome(status, **details):
        return {"status": "ok", "data": {"portion_web": {
            "operation": operation, "status": status, "requests_remaining": web_requests_remaining(rt), **details}}}

    if rt.prepared is not None:
        # A request reserved before an interrupted call may have reached the
        # provider. Never send it a second time to recover an unknown outcome.
        return outcome("service_error", reason="lookup_interrupted")
    rt.check_deadline()
    if operation == "read_cache":
        cached = rt.store.load_document("portion_cache", rt.user_id, p["ref"])
        if not _fresh(cached):
            return outcome("not_applicable", reason="cache_missing_or_expired")
        return outcome("read", pages=[{"status": "read", "ref": cached["ref"], "url": cached["url"],
                       "title": cached["title"], "text": cached["quote"], "fetched_at": cached["fetched_at"],
                       "cache_evidence": cached}])
    if operation == "search":
        key = _search_key(p["query"])
        if (operation, key) in attempts:
            return outcome("repeated_request")
        if web_requests_remaining(rt) == 0:
            return outcome("budget_exhausted")
        try:
            found = deepseek_web.search(p["query"], deadline=rt.deadline,
                                        reserve=lambda: _reserve_web_request(rt, operation, key))
        except deepseek_web.WebLookupError as exc:
            return outcome("service_error", reason=exc.code)
        return outcome("located" if found else "no_results", results=found)

    urls = list(dict.fromkeys(deepseek_web.public_url(url) for url in p["urls"]))
    user_urls = _user_urls(rt)
    available = set(results) | user_urls
    previous = {page["url"]: page for page in pages_by_ref.values()}
    pages = []
    for url in urls:
        if not url or url not in available:
            pages.append({"url": url or "", "status": "not_applicable", "reason": "url_outside_current_sources"})
            continue
        if url in previous:
            pages.append(previous[url])
            continue
        if (operation, url) in attempts:
            pages.append({"url": url, "status": "repeated_request"})
            continue
        if web_requests_remaining(rt) == 0:
            pages.append({"url": url, "status": "budget_exhausted"})
            continue
        rt.check_deadline()
        try:
            body = deepseek_web.fetch(url, deadline=rt.deadline,
                                      reserve=lambda: _reserve_web_request(rt, operation, url))
        except deepseek_web.WebLookupError as exc:
            pages.append({"url": url, "status": "service_error", "reason": exc.code})
            continue
        if not body:
            pages.append({"url": url, "status": "unreadable"})
            continue
        fetched_at = datetime.now(timezone.utc).isoformat()
        key = [rt.user_id, rt.message["chat_id"], rt.message["message_id"], url, fetched_at, body["text"]]
        ref = "portion:" + hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:24]
        pages.append({"status": "read", "ref": ref, "url": url, "fetched_at": fetched_at,
                      "url_source": "user" if url in user_urls else "search", **body})
    statuses = {page["status"] for page in pages}
    status = next(value for value in ("read", "service_error", "unreadable", "budget_exhausted", "repeated_request", "not_applicable")
                  if value in statuses)
    return outcome(status, pages=pages)


_NUMBER = r"(?:[0-9]+(?:\.[0-9]+)?)"
_MASS_UNIT = r"(?:kilograms?|grams?|ounces?|pounds?|kg|g|oz|lbs?|千克|公斤|克|盎司|磅)"
_MASS = re.compile(rf"(?<![a-z0-9.])(?P<weight>{_NUMBER})\s*(?P<unit>{_MASS_UNIT})(?![a-z/])")
_UNITS = {"g": "g", "gram": "g", "grams": "g", "克": "g",
          "kg": "kg", "kilogram": "kg", "kilograms": "kg", "千克": "kg", "公斤": "kg",
          "oz": "oz", "ounce": "oz", "ounces": "oz", "盎司": "oz",
          "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb", "磅": "lb"}
_GRAMS = {"g": Decimal("1"), "kg": Decimal("1000"), "oz": Decimal("28.349523125"),
          "lb": Decimal("453.59237")}
_EDIBLE = r"(?:可食(?:用)?(?:部分|部位|部)?|食用部分|去(?:皮|核|壳|骨)后|\bedible(?:\s+(?:portion|part))?\b|without\s+(?:the\s+)?(?:peel|pit|shell|bone))"
_TOTAL = r"(?:总重(?:量)?|合计|共重|总计|\btotal\b|\bcombined\b)"
_CONNECTOR = r"(?:的|为|是|重(?:量)?|净重|质量|约|大约|\b(?:has|an?|of|is|are|weight|weighs?|about|approximately|net)\b)"
_RANGE = re.compile(r"[<>≤≥±~～–—]|[0-9]\s*[-至到]\s*[0-9]|至少|至多|超过|不超过|上下|以下|以上|\b(?:up to|at least|at most|between|less than|more than)\b")
_CLAUSE = re.compile(r"\n|[。！？!?；;]|(?<=[a-z])\.\s", re.IGNORECASE)
_NEGATION = re.compile(r"不代表|不等于|并非|不是|未证实|不能认定|\b(?:not|unverified)\b")
_COUNT_SCRIPT = str.maketrans("個隻顆條", "个只颗条")


def _normalized(value):
    return unicodedata.normalize("NFKC", value).translate(_COUNT_SCRIPT).lower().strip()


def _quantity_head(unit_pattern, evidence):
    if evidence["basis"] == "edible_total":
        count = evidence["count"]
        return rf"(?<![0-9./]){count}\s*{unit_pattern}"
    return rf"(?:每\s*(?:一|1)?|(?<![a-z])(?:each|per|one|a|an)(?![a-z])|(?<![0-9./第])1|(?<![0-9第])一)\s*{unit_pattern}"


def _valid_extraction(quote, evidence):
    source_food = _normalized(evidence["source_food"])
    source_unit = _normalized(evidence["source_unit"])
    unit_pattern = rf"(?<![a-z]){re.escape(source_unit)}(?![a-z])"
    total = evidence["basis"] == "edible_total"
    head = _quantity_head(unit_pattern, evidence)
    unit_names_food = bool(_food_forms(source_unit).keys() & _food_forms(source_food).keys())
    connector = re.compile(rf"[\s:：,，()（）=|]|{_EDIBLE}|{_TOTAL}|{_CONNECTOR}|{re.escape(source_food)}")

    def connects(segment):
        end = 0
        while end < len(segment) and (part := connector.match(segment, end)):
            end = part.end()
        return end == len(segment)

    def food_before(segment):
        return any((not segment[:food.start()].rstrip() or segment[:food.start()].rstrip()[-1] in ":：,，(（|")
                   and connects(segment[food.end():])
                   for food in re.finditer(re.escape(source_food), segment))
    # A single clause must express the relationship. Finding both numbers
    # somewhere in the quote would also match unrelated nutrition or packaging.
    for clause in _CLAUSE.split(_normalized(quote)):
        if (source_food not in clause or not re.search(_EDIBLE, clause)
                or _RANGE.search(clause) or _NEGATION.search(clause)):
            continue
        if total and not re.search(_TOTAL, clause):
            continue
        matches = list(_MASS.finditer(clause))
        if len(matches) != 1:
            continue
        mass = matches[0]
        if (_UNITS[mass["unit"]] != evidence["weight_unit"]
                or Decimal(mass["weight"]) != Decimal(str(evidence["weight"]))):
            continue
        before, after = clause[:mass.start()], clause[mass.end():]
        for start in re.finditer(head, before):
            following = before[start.end():]
            bound = (unit_names_food
                     or re.match(rf"\s*(?:of\s+)?{re.escape(source_food)}", following)
                     or food_before(before[:start.start()]))
            if bound and connects(following) and connects(after):
                return True
        if not total and food_before(before):
            for unit in re.finditer(rf"(?:每|\bper\b)\s*{unit_pattern}", after):
                if connects(after[:unit.start()]) and connects(after[unit.end():]):
                    return True
    return False


def _table_header(text, quote, evidence):
    """Find the row's own Markdown table and verify its edible weight column."""
    if "\n" in quote or quote.count("|") < 2:
        return None

    def cells(line):
        return [_normalized(cell) for cell in line.strip().strip("|").split("|")]

    lines = text.splitlines()
    row = cells(quote)
    food, unit = (_normalized(evidence[key]) for key in ("source_food", "source_unit"))
    head = _quantity_head(rf"(?<![a-z]){re.escape(unit)}(?![a-z])", evidence)

    def has_food_label(cell):
        if _food_forms(unit).keys() & _food_forms(food).keys() and re.search(head, cell):
            return True
        for match in re.finditer(re.escape(food), cell):
            before, after = cell[:match.start()].rstrip(), cell[match.end():].lstrip()
            left = not before or before[-1] in ",:;、)" or re.search(rf"{head}\s*$", before)
            right = (not after or after[0] in ",:;、" or re.match(head, after)
                     or re.match(r"\((?:小|中|大|生|熟|small|medium|large|raw|cooked|unpeeled|peeled)\)", after))
            if left and right:
                return True
        return False

    for index, line in enumerate(lines):
        if line.strip() != quote or index < 2:
            continue
        divider = index - 1
        while divider > 0 and len(cells(lines[divider])) == len(row):
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells(lines[divider])):
                headers = cells(lines[divider - 1])
                if len(headers) != len(row):
                    break
                for column, header in enumerate(headers):
                    measure = re.search(rf"\(\s*({_MASS_UNIT})\s*\)", header)
                    if (not measure or _UNITS[measure[1]] != evidence["weight_unit"]
                            or not re.search(_EDIBLE, header)
                            or not re.search(r"重量|重(?:量)?|\bweight\b", header)
                            or not re.fullmatch(_NUMBER, row[column])
                            or Decimal(row[column]) != Decimal(str(evidence["weight"]))):
                        continue
                    labels = [cell for pos, cell in enumerate(row) if pos != column and has_food_label(cell)]
                    if len(labels) == 1 and not _RANGE.search(labels[0]) and re.search(head, labels[0]):
                        return lines[divider - 1].strip()
                break
            divider -= 1
    return None


def _serving_table_header(text, quote, evidence):
    """Read a wrapped Markdown serving cell whose own table states edible weight."""
    food, unit = (_normalized(evidence[key]) for key in ("source_food", "source_unit"))
    head = _quantity_head(rf"(?<![a-z]){re.escape(unit)}(?![a-z])", evidence)
    serving = re.compile(rf"{re.escape(food)}\s*[,;:]?\s*{head}\s*\(\s*"
                         rf"({_NUMBER})\s*({_MASS_UNIT})(?:\s*/\s*({_NUMBER})\s*({_MASS_UNIT}))?\s*\)")
    lines = text.splitlines()
    previous_divider = -1
    for index, divider in enumerate(lines):
        columns = divider.strip().strip("|").split("|")
        if len(columns) < 2 or not all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in columns):
            continue
        leading = "\n".join(lines[max(previous_divider + 1, index - 90):index]).rsplit("\n#", 1)[-1]
        previous_divider = index
        header = leading.split("|", 1)[0]
        if not re.search(r"\bserving\s+size\b", header, re.IGNORECASE):
            continue
        caption = next((line.strip() for line in header.splitlines() if re.search(_EDIBLE, _normalized(line))
                        and not _NEGATION.search(_normalized(line)) and re.search(r"\bweight\b|重量", _normalized(line))), "")
        if not caption:
            continue
        row = []
        for line in lines[index + 1:]:
            if line.lstrip().startswith("#"):
                break
            row.append(line)
            if line.count("|") != len(columns) - 1:
                if len(row) > 15:
                    break
                continue
            first = "\n".join(row).split("|", 1)[0].strip()
            row = []
            if not (quote == first or quote.startswith(first + "\n") or quote.startswith(first + " |")):
                continue
            match = serving.fullmatch(" ".join(_normalized(first).split()))
            if not match:
                return None
            weight, mass_unit, alternate, alternate_unit = match.groups()
            units = [(Decimal(weight), _UNITS[mass_unit])]
            if alternate_unit and _UNITS[alternate_unit] != units[0][1]:
                units.append((Decimal(alternate), _UNITS[alternate_unit]))
            if (Decimal(str(evidence["weight"])), evidence["weight_unit"]) in units:
                return caption
            return None
    return None


_COUNT_UNITS = {"个", "只", "枚", "颗", "根", "条", "piece", "pieces"}
_SIZES = {
    "small": r"小(?:号|型|份|个|碗|杯|袋|包|盒|瓶|罐)|\(小\)|\bsmall\b",
    "medium": r"中(?:号|型|份|个|碗|杯|袋|包|盒|瓶|罐)|\(中\)|\bmedium\b",
    "large": r"大(?:号|型|份|个|碗|杯|袋|包|盒|瓶|罐)|\(大\)|\blarge\b",
}
_SIZE_WORD = r"(?:[小中大](?:号|型)|\b(?:small|medium|large)\b)"


def _without_size(value):
    value = re.sub(rf"^{_SIZE_WORD}\s*", "", value)
    return re.sub(rf"\s*\((?:[小中大]|{_SIZE_WORD})\)$", "", value).strip()


def _portion_unit_base(value):
    unit = _without_size(value)
    if re.fullmatch(r"[小中大][份个碗杯袋包盒瓶罐]", unit):
        return unit[1:]
    return unit


def personal_unit(food, unit, food_query="", quantity_text=""):
    """Normalize spelling without extending a user's portion to another size."""
    unit, query = _normalized(unit), _normalized(food_query)
    sizes = {size for size, pattern in _SIZES.items() if re.search(pattern, unit)}
    base = _normalized(food["name"])
    markers = {"small": "小", "medium": "中", "large": "大"}
    for size, marker in markers.items():
        scoped = {f"{marker}{suffix}{base}" for suffix in ("", "号", "型", "个")}
        if query in scoped or any(_normalized(quantity_text).endswith(name) for name in scoped):
            sizes.add(size)
    if len(sizes) > 1:
        return None
    if not sizes:
        return unit
    return f"{_portion_unit_base(unit)}({markers[next(iter(sizes))]})"


def personal_portion(settings, food, unit, food_query="", quantity_text=""):
    unit = personal_unit(food, unit, food_query, quantity_text)
    if not unit:
        return None
    prefix = food["food_id"] + ":"
    portions = settings.get("portions", {})
    if exact := portions.get(prefix + unit):
        return exact
    matches = [value for key, value in portions.items() if key.startswith(prefix)
               and personal_unit(food, key[len(prefix):]) == unit]
    return matches[0] if len(matches) == 1 else None


def _same_portion_unit(item_unit, evidence, *, table=False):
    user_unit = _normalized(item_unit)
    source_unit = _normalized(evidence["source_unit"])
    user_base = _portion_unit_base(user_unit)
    source_food = _normalized(evidence["source_food"])
    quote = _normalized(evidence["quote"])
    named_food_count = user_base in _COUNT_UNITS and _without_size(source_unit) == _without_size(source_food)
    bilingual_count = (table and user_base in _COUNT_UNITS and source_unit in {"piece", "pieces"}
                       and re.search(_quantity_head(re.escape(user_base), evidence), quote))
    size_as_whole_food = table and user_base in _COUNT_UNITS and source_unit in {"small", "medium", "large"}
    if _portion_unit_base(source_unit) != user_base and not (named_food_count or bilingual_count or size_as_whole_food):
        return False
    user_sizes = {size for size, pattern in _SIZES.items() if re.search(pattern, user_unit)}
    source_sizes = {size for size, pattern in _SIZES.items() if re.search(pattern, quote)}
    return not user_sizes or user_sizes == source_sizes


def _explicit_cooking_state(value):
    text = _normalized(value)
    raw = text == "生" or bool(re.search(r"\(生\)|生制|生重|\braw\b", text))
    cooked = text == "熟" or bool(re.search(r"\(熟\)|熟制|熟重|煮熟|\bcooked\b", text))
    return "raw" if raw and not cooked else "cooked" if cooked and not raw else ""


def _food_forms(value):
    label = _without_size(_normalized(value))
    if not label:
        return {}
    forms = {label: _explicit_cooking_state(label)}
    prepared = re.fullmatch(r"(raw\s+|cooked\s+|生制|熟制|生|熟)(.+)", label)
    suffixed = re.fullmatch(r"(.+?)\((raw|cooked|生制|熟制|生|熟)\)", label)
    if prepared and (len(prepared[1]) > 1 or len(prepared[2]) >= 2):
        forms[_without_size(prepared[2])] = _explicit_cooking_state(prepared[1])
    elif suffixed:
        forms[_without_size(suffixed[1])] = _explicit_cooking_state(suffixed[2])
    for name, state in list(forms.items()):
        if re.fullmatch(r"[a-z][a-z ]+s", name):
            forms.setdefault(name[:-1], state)
    return forms


def _check_food_scope(food, evidence, title=""):
    forms = _food_forms(evidence["source_food"])
    matches = [(forms[label], expected) for name in [food["name"], *food.get("aliases", [])]
               for label, expected in _food_forms(name).items() if label in forms]
    if not matches:
        raise ValueError("引文中的食物与营养来源名称或别名无法对应")
    if brand := _normalized(str(food.get("brand", ""))):
        source = _normalized(evidence["quote"] + " " + title)
        pattern = rf"(?<![a-z0-9]){re.escape(brand)}(?![a-z0-9])" if brand.isascii() else re.escape(brand)
        if not re.search(pattern, source):
            raise ValueError("原文未核实所选食物的品牌")
    cited = (_explicit_cooking_state(evidence["quote"] + " " + evidence.get("table_header", ""))
             or next((state for state, _ in matches if state), ""))
    expected = (_explicit_cooking_state(str(food.get("type", "")))
                or next((state for _, state in matches if state), ""))
    if cited and expected and cited != expected:
        raise ValueError("引文与营养来源的生熟口径不同")


def resolve_web_portion(rt, food, item, quantity, evidence):
    page = _web_state(rt)[2].get(evidence["ref"])
    if page is None:
        raise ValueError("份量证据须来自本条消息实际读取的正文或有效缓存")
    if cached := page.get("cache_evidence"):
        fields = ("ref", "quote", "source_food", "source_unit", "basis", "weight", "weight_unit", "count")
        if (not _fresh(cached) or cached["food_id"] != food["food_id"]
                or not _same_portion_unit(item.get("unit", ""), cached, table=bool(cached.get("table_header")))
                or any(evidence.get(key) != cached.get(key) for key in fields)):
            raise ValueError("份量缓存的食物、单位或提取依据与本项不符")
        _check_food_scope(food, cached, page["title"])
        unit_grams = Decimal(str(cached["weight"])) * _GRAMS[cached["weight_unit"]] / (cached.get("count") or 1)
        grams = positive_grams(float(unit_grams * Decimal(str(quantity))))
        return grams, {**cached, "quantity": quantity, "unit": item["unit"], "adopted_grams": grams}
    quote, text = evidence["quote"], page["text"]
    if quote not in text:
        # Web extraction can wrap the same Markdown cell across lines.
        match = re.search(r"\s+".join(re.escape(part) for part in quote.split()), text)
        if not match or not match[0]:
            raise ValueError("引文不在本条消息读取的原文中")
        quote = match[0]
    table = _table_header(text, quote, evidence) or _serving_table_header(text, quote, evidence)
    if not table:
        # Check the actual complete clause so a quotation cannot trim a range
        # or qualification off a number visible in the page.
        excerpt = quote.rstrip("。！？!?；;. ").lstrip()
        quote = next((clause.strip() for clause in _CLAUSE.split(text)
                      if excerpt and excerpt in clause and _valid_extraction(clause, evidence)), "")
    if not quote:
        raise ValueError("引文未明确支持该可食单位重量或可食总重与数量的关系")
    evidence = {**evidence, "quote": quote, **({"table_header": table} if table else {})}
    if not item.get("unit") or not _same_portion_unit(item["unit"], evidence, table=bool(table)):
        raise ValueError("引文不能支持本项实际份量的单位和规格")
    _check_food_scope(food, evidence, page["title"])
    unit_grams = Decimal(str(evidence["weight"])) * _GRAMS[evidence["weight_unit"]] / (evidence.get("count") or 1)
    grams = positive_grams(float(unit_grams * Decimal(str(quantity))))
    return grams, {**evidence, "url": page["url"], "title": page["title"], "fetched_at": page["fetched_at"],
                   "food_id": food["food_id"], "food_name": food["name"], "quantity": quantity,
                   "unit": item["unit"], "grams_per_unit": float(unit_grams), "adopted_grams": grams}
