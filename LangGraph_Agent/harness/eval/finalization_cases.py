"""Real runtime and durable Pipeline counterexamples for terminal publication."""
from copy import deepcopy
import hashlib
from unittest.mock import patch


def FN01():
    """无动作及部分提交的模型终止，展示后均保留清洁终止语义而非旧业务数字。"""
    from langchain_core.messages import AIMessage
    from agent import run
    from agent.graph import mark_reply_presented
    from agent.storage import conversation
    from harness.eval.personal_cases import setup, CONFIG, NativeFake, call, MEAL

    def fail(messages):
        raise RuntimeError("injected model unavailable")

    for partial in (False, True):
        store = setup()
        steps = [call("record_meal", MEAL), fail] if partial else [fail]
        message = dict(message_id="terminal-partial" if partial else "terminal-empty",
                       user_id="user", chat_id="test", text="记录夹具A和夹具B，再给建议。")
        result = run(message, store=store, config=CONFIG, model=NativeFake(responses=steps))
        assert result["terminal"] and result["completion_status"] == "terminated"
        assert len(store.intake) == (2 if partial else 0)
        before, _ = conversation.history(store, "user", "test")
        assert not any(isinstance(m, AIMessage) for m in before)
        mark_reply_presented(store, "user", "test", result)
        history, _ = conversation.history(store, "user", "test")
        last = history[-1]
        assert isinstance(last, AIMessage) and last.additional_kwargs["fact_scope"] == "dialogue"
        assert "未执行的动作已终止" in last.content
        assert "270" not in last.content and "千卡" not in last.content
        replay = run(message, store=store, config=CONFIG, model=NativeFake(responses=[fail]))
        assert replay["reply"] == result["reply"]


def FN02():
    """最终正文发布后进程死亡，迟到的旧展示不能确认新正文；仅恢复同UUID最终交付。"""
    from harness.eval.production_pipeline_cases import fixture, event, message
    _, pipeline, config = fixture()
    original = pipeline.accept(message("progress-promotion"), source="cli")
    interim = {"status": "error", "reply": "仍在核对原提交。", "errors": ["ConnectionError:unknown"],
               "tool_results": [], "native_calls": []}
    with patch("agent.config.load_config", return_value=config):
        for attempt in (1, 2, 3):
            try:
                pipeline.worker(event(original), execute=lambda *a, **k: deepcopy(interim))
            except RuntimeError:
                assert attempt < 3
        prior = pipeline.result(original["receipt_id"])
        old_reference = {"receipt_id": original["receipt_id"], "uuid": prior["delivery"]["uuid"],
                         "result_digest": prior["delivery"]["result_digest"], "presented_at": 1}

        class Death(BaseException):
            pass

        def die(*args, **kwargs):
            raise Death()

        final = {"status": "ok", "reply": "原提交已查明。", "errors": []}
        with patch.object(pipeline, "deliver", side_effect=die):
            try:
                pipeline.worker({**event(original), "manual_recovery": True}, execute=lambda *a, **k: final)
            except Death:
                pass
            else:
                raise AssertionError("publication interruption missing")
        published = pipeline.result(original["receipt_id"])
        expected_digest = hashlib.sha256(final["reply"].encode()).hexdigest()
        assert published["result"] == final
        assert published["delivery"]["result_digest"] == expected_digest != old_reference["result_digest"]
        stale = pipeline.accept(message("progress-old-presented", ""), source="cli", kind="presented", reference=old_reference)
        rejected = pipeline.worker(event(stale))
        assert rejected["presentation_rejected"] and "older reply" in rejected["error"]
        assert pipeline.result(original["receipt_id"])["status"] == "retry"
        # Even though the original work exhausted three attempts, this saved
        # final body has its own bounded delivery recovery; it cannot replan.
        recovered = pipeline.worker(event(original), execute=die, send=die)
        assert recovered["status"] == "awaiting_presentation"
        assert recovered["delivery"]["uuid"] == old_reference["uuid"]
        assert recovered["result"] == final and recovered["delivery"]["recovery_attempts"] == 2
        confirmed = pipeline.accept(message("progress-final-presented", ""), source="cli", kind="presented",
                                    reference={**old_reference, "result_digest": expected_digest, "presented_at": 2})
        assert pipeline.worker(event(confirmed))["status"] == "completed"
        assert pipeline.result(original["receipt_id"])["status"] == "completed"
        unwoken = pipeline.accept(message("progress-stale-unwoken", ""), source="cli", kind="presented", reference=old_reference)
        following = pipeline.accept(message("after-stale-presentation"), source="cli")
        next_result = pipeline.worker(event(following), execute=lambda *a, **k: {"status": "ok", "reply": "新请求已处理。"})
        assert next_result["status"] == "awaiting_presentation"
        assert pipeline.result(unwoken["receipt_id"])["presentation_rejected"]


CHECKS = {"FN01": FN01, "FN02": FN02}
