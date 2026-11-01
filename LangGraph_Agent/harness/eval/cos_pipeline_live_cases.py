"""Offline regression for the isolated latency driver's once-only gate."""
import json
from unittest.mock import patch

from harness.eval import cos_pipeline_live as live
from harness.eval.cos_pipeline_cases import Objects


class Clock:
    def __init__(self):
        self.elapsed = 0

    def time(self):
        return 1793548800 + self.elapsed

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


def C05():
    """隔餐序列超时后拒绝重跑旧结果；未满三小时不发HTTP。"""
    scope = "idle-pipeline-validation"
    root = "cos-probe/pipeline/" + scope + "/"
    event = {"operation": "pipeline_sequence", "scope": scope}
    objects, clock = Objects(), Clock()
    objects.create(root + "seed.json", json.dumps({"seeded_at": clock.time()-10800}).encode())

    def accepted(payload):
        # Simulate HTTP acceptance while the native message worker is still busy.
        assert payload["event"]["message"]["message_id"].endswith("-record")
        return {"client_started_at": clock.time(), "ack_received_at": clock.time()+0.1,
                "ack": {"ack": True, "received_at": clock.time()}}

    with patch.dict(live.os.environ, {"SCF_FUNCTIONNAME": live.FUNCTIONS["driver"]}), \
            patch.object(live, "_objects", return_value=objects), \
            patch.object(live, "_post", side_effect=accepted) as post, \
            patch.object(live, "time", clock):
        try:
            live.control(event)
        except TimeoutError as exc:
            assert "native pipeline did not complete" in str(exc)
        else:
            raise AssertionError("missing native result must time out")
        gate_key = root + "sequence-started.json"
        gate_body = objects.get(gate_key)
        assert gate_body is not None and post.call_count == 1
        assert json.loads(gate_body)["started_at"] == 1793548800
        # The old native attempt eventually finishes AFTER its driver timed out.
        # A second driver must reject before another HTTP request or old-result
        # latency calculation, rather than reuse this finished attempt.
        result_key = live.result_key("cos-probe-" + scope + "-record", scope=scope)
        old_result = {"completed": True, "timing": {"reply_sent_at": clock.time()-1},
                      "audit": {"count": 2, "kcal": 270}}
        assert objects.create(result_key, json.dumps(old_result).encode())
        try:
            live.control(event)
        except ValueError as exc:
            assert "already started" in str(exc)
        else:
            raise AssertionError("a timed-out sequence must never restart as fresh")
        assert post.call_count == 1 and objects.get(gate_key) == gate_body
        assert objects.get(root + "sequence.json") is None

    # This independent namespace image has no old gate; eligibility alone must
    # reject the request, with no HTTP side effect and no claimed measurement.
    too_early, clock = Objects(), Clock()
    too_early.create(root + "seed.json", json.dumps({"seeded_at": clock.time()-10799}).encode())
    with patch.dict(live.os.environ, {"SCF_FUNCTIONNAME": live.FUNCTIONS["driver"]}), \
            patch.object(live, "_objects", return_value=too_early), \
            patch.object(live, "_post") as post, patch.object(live, "time", clock):
        try:
            live.control(event)
        except ValueError as exc:
            assert "three hours" in str(exc)
        else:
            raise AssertionError("a premature sample must be refused")
        post.assert_not_called()
        assert too_early.get(root + "sequence-started.json") is None


CHECKS = {"C05": C05}
