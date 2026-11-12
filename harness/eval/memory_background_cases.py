"""Experience maintenance verification with isolated state and injected model."""
from dataclasses import replace
import json
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage

from agent.storage import experience, memory, memory_background
from agent.storage.cos_journal import Journal
from harness.eval.personal_cases import CONFIG, NativeFake
from harness.eval.production_store_cases import Objects
from harness.eval.stage_a_cases import first_feedback_fixture


def _delta(ref, quote, theme=None):
    links = [{"kind": "note", "key": "lunch"}]
    if theme:
        links.append({"ref": theme})
    return {"changes": [
        {"kind": "note", "key": "lunch", "status": "usable", "text": "午餐可以分开打包。",
         "context": "午休时间紧，需要加热", "supports": [ref], "quotes": [{"ref": ref, "text": quote}]},
        {"kind": "theme", "key": "meal-prep", "status": "usable", "title": "打包午餐", "text": "安排短时间午餐。",
         "links": links}], "entries": [{"key": "meal-prep"}]}


def PM04():
    """The oldest task gets one model slot, and a ready candidate only replays application."""
    store, source, quote = first_feedback_fixture()
    objects = Objects()
    progress = Journal(objects, "test/sync/")
    user = source["user_id"]
    ref = source["materials"][0]["ref"]
    event = []
    requests = []

    def model_http(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["max_tokens"] == 4096
        assert [tool["function"]["name"] for tool in body["tools"]] == ["query_memory"]
        snapshot = json.loads(body["messages"][-1]["content"])
        assert snapshot["materials"][0]["ref"] == ref and snapshot["remaining"]["decisions_after_this"] == 1
        delta = _delta(ref, quote)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(delta, ensure_ascii=False)}}], "usage": {}})

    timestamp = store.load_document("memory_active", "system")[user]
    store.save_document("summary_active", "system", "", {"summary": {"registered_at": timestamp - 1}})
    with patch.object(memory_background.conversation, "run_background", return_value={"status": "ready"}) as summary:
        assert memory_background.run_next(store, progress, config=CONFIG)["task_kind"] == "summary"
        assert summary.call_count == 1
    assert not requests and not event
    store.save_document("summary_active", "system", "", {"summary": {"registered_at": timestamp + 1}})
    with (patch.object(experience, "local_day", return_value=store.today),
          httpx.Client(transport=httpx.MockTransport(model_http)) as client,
          patch("agent.model._CLIENT", client), patch.dict("os.environ", {"DEEPSEEK_API_KEY": "fixture"})):
        first = memory_background.run_next(store, progress, config=CONFIG, dispatch=event.append)
        second = memory_background.run_next(store, Journal(objects, "test/sync/"), config=CONFIG, dispatch=event.append)
        assert first == second and first["status"] == "ready" and first["task_kind"] == "experience"
        assert len(requests) == 1 and event == [event[0], event[0]]
        assert not memory.state(store, user).get("entries")
        assert experience.apply_candidate(store, event[0], CONFIG)["status"] == "applied"
    note = memory.get(store, user, memory.state(store, user)["entries"][0])[0]["links"][0]
    assert memory.get(store, user, note)[0]["supports"] == [ref]
    assert len(requests) == 1


def PM05():
    """Freeze an in-progress input, preserve attempt limits, and skip with insufficient budget."""
    store, source, _ = first_feedback_fixture()
    progress = Journal(Objects(), "test/sync/")
    user = source["user_id"]
    model = NativeFake(responses=[AIMessage(content="malformed"), AIMessage(content="malformed")])
    with patch.object(experience, "local_day", return_value=store.today):
        assert memory_background.run_next(store, progress, config=CONFIG, model=model,
                                          remaining_seconds=24)["status"] == "skipped_budget"
        assert memory_background.run_next(store, progress, config=replace(CONFIG, preset="shadow_readonly"),
                                          model=model)["status"] == "skipped_preset"
        assert not model.requests
        first = memory_background.run_next(store, progress, config=CONFIG, model=model, remaining_seconds=25)
        assert first["status"] == "retry" and first["attempts"] == 1
        later = memory.save(store, user, "turn", "later", {"status": "delivered", "turn_id": "later", "text": "不同内容"})
        memory.enqueue(store, user, later["ref"])
        last = memory_background.run_next(store, progress, config=CONFIG, model=model)
        assert last["status"] == "failed" and last["candidate"]["attempts"] == 2
        assert memory_background.run_next(store, progress, config=CONFIG, model=model)["candidate"] == last["candidate"]
        assert len(model.requests) == 2
        first_input, second_input = [json.loads(request[-1].content) for request in model.requests]
        first_remaining, second_remaining = first_input.pop("remaining"), second_input.pop("remaining")
        assert first_input == second_input
        assert second_remaining["generated_tokens_remaining"] < first_remaining["generated_tokens_remaining"]
        assert second_remaining["execution_ms_remaining"] <= first_remaining["execution_ms_remaining"]
        assert experience.apply_candidate(store, last, CONFIG)["status"] == "failed"
        empty = NativeFake(responses=[AIMessage(content='{"changes":[]}')])
        next_batch = memory_background.run_next(store, progress, config=CONFIG, model=empty)
        assert next_batch["status"] == "empty" and next_batch["candidate"]["materials"] == [later["ref"]]
        assert next_batch["candidate"]["attempts"] == 1 and len(empty.requests) == 1


def PM06():
    """A newer publication can reject a candidate without resetting its calculation limit."""
    store, source, _ = first_feedback_fixture()
    progress = Journal(Objects(), "test/sync/")
    user = source["user_id"]
    ref = source["materials"][0]["ref"]
    model = NativeFake(responses=[AIMessage(content='{"changes":[]}')] * 2)
    batches = []
    with patch.object(experience, "local_day", return_value=store.today):
        for attempt in (1, 2):
            result = memory_background.run_next(store, progress, config=CONFIG, model=model)
            assert result["status"] == "empty" and result["candidate"]["attempts"] == attempt
            batches.append(result["batch_id"])
            view = memory.state(store, user)
            view["experience_revision"] = result["candidate"]["base_revision"] + 1
            store.save_document("memory_state", user, "", view)
            assert experience.apply_candidate(store, result, CONFIG)["status"] == "obsolete"
            if attempt == 1:
                budget = result["candidate"]["budget"]
                assert store.load_document("memory_work", user)["budgets"][ref] == budget
        assert memory_background.run_next(store, progress, config=CONFIG, model=model)["status"] == "idle"
    assert batches[0] != batches[1] and len(model.requests) == 2
    assert json.loads(model.requests[1][-1].content)["remaining"]["generated_tokens_remaining"] == 4096 - budget["generated_tokens"]
    assert store.load_document("memory_work", user)["processed"][ref] == "retry_required"


def PM07():
    """An actual native read can inform a candidate within cumulative wire limits."""
    store, source, quote = first_feedback_fixture()
    user, ref = source["user_id"], source["materials"][0]["ref"]
    prior = memory.save(store, user, "turn", "prior", {"status": "delivered", "turn_id": "prior", "text": "提前备餐的原始经历"})
    theme = memory.save(store, user, "theme", "prior-theme", {"key": "prior-theme", "status": "usable",
        "text": "工作日带饭", "title": "工作日带饭", "links": [prior["ref"]], "dependencies": [prior["ref"]]})
    view = memory.state(store, user)
    view["entries"] = [theme["ref"]]
    store.save_document("memory_state", user, "", view)
    requests, events = [], []
    inaccessible = memory.reference("other", "turn", "hidden")

    def model_http(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            context = json.loads(body["messages"][-1]["content"])
            assert theme["ref"] in context["entry_refs"] and theme["ref"] not in {item["ref"] for item in context["related"]}
            assert body["max_tokens"] == 4096
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {"content": None,
                "reasoning_content": "读取确切入口", "tool_calls": [{"id": "read-1", "type": "function", "function": {
                    "name": "query_memory", "arguments": json.dumps({"refs": [theme["ref"], inaccessible]})}}]}}],
                "usage": {"completion_tokens": 190}})
        assert len(requests) == 2 and body["max_tokens"] == 4096 - 190 and "tools" not in body
        assert body["messages"][-2]["reasoning_content"] == "读取确切入口"
        answer = body["messages"][-1]
        assert answer["role"] == "tool" and answer["tool_call_id"] == "read-1"
        results = json.loads(answer["content"])["results"]
        assert results[0]["item"]["title"] == "工作日带饭" and results[1]["status"] == "ref_not_available"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(_delta(ref, quote, theme["ref"]), ensure_ascii=False)}}],
            "usage": {"completion_tokens": 210}})

    with (patch.object(experience, "local_day", return_value=store.today),
          httpx.Client(transport=httpx.MockTransport(model_http)) as client,
          patch("agent.model._CLIENT", client), patch.dict("os.environ", {"DEEPSEEK_API_KEY": "fixture"})):
        progress = Journal(Objects(), "test/sync/")
        result = memory_background.run_background(store, progress, config=CONFIG, dispatch=events.append)
        assert result["status"] == "ready" and len(events) == 1
        candidate = result["candidate"]
        assert candidate["attempts"] == 2
        assert candidate["budget"]["generated_tokens"] == 400 and candidate["budget"]["reads"] == 1
        assert candidate["source_reads"] == [{"ref": theme["ref"], "digest": memory_background.conversation.digest(theme)}]
        assert experience.apply_candidate(store, events[0], CONFIG)["status"] == "applied"
    assert len(requests) == 2 and not store.intake


def PM08():
    """A raw result saved before interruption recovers without generating again."""
    store, source, quote = first_feedback_fixture()
    objects = Objects()
    progress = Journal(objects, "test/sync/")
    ref = source["materials"][0]["ref"]
    requests = []
    original_commit = progress.commit

    def model_http(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(_delta(ref, quote), ensure_ascii=False)}}],
            "usage": {"completion_tokens": 173}})

    def interrupt_candidate(changes):
        if next(iter(changes.values())).get("candidate"):
            raise OSError("interrupted after durable model response")
        return original_commit(changes)

    with (patch.object(experience, "local_day", return_value=store.today),
          httpx.Client(transport=httpx.MockTransport(model_http)) as client,
          patch("agent.model._CLIENT", client), patch.dict("os.environ", {"DEEPSEEK_API_KEY": "fixture"})):
        with patch.object(progress, "commit", side_effect=interrupt_candidate):
            try:
                memory_background.run_background(store, progress, config=CONFIG)
            except OSError as error:
                assert str(error) == "interrupted after durable model response"
            else:
                raise AssertionError("candidate save did not reach the interruption fixture")
        recovered = memory_background.run_background(store, Journal(objects, "test/sync/"), config=CONFIG)
        assert len(requests) == 1 and recovered["status"] == "ready"
        assert recovered["candidate"]["budget"]["generated_tokens"] == 173
        assert recovered["candidate"]["budget"]["elapsed_ms"] == 20_000
        assert experience.apply_candidate(store, recovered, CONFIG)["status"] == "applied"


CHECKS = {"PM04": PM04, "PM05": PM05, "PM06": PM06, "PM07": PM07, "PM08": PM08}
