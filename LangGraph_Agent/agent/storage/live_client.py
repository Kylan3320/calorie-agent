"""HTTP adapter for local live entries; business writes stay in the SCF writer."""
import os
import time
import httpx
from .model_transport import create_client, deadline_scope, remaining
from .timeutil import remaining_timeout

_CLIENT = create_client()


class LiveClient:
    def __init__(self, *, client=None):
        self.endpoint = os.environ["CALORIE_SERVICE_URL"].rstrip("/")
        self.token = os.environ["CALORIE_SERVICE_TOKEN"]
        self.client = client or _CLIENT

    def _request(self, method, path, **kwargs):
        with deadline_scope(time.monotonic() + remaining_timeout(10)):
            request = self.client.build_request(method, self.endpoint + path, timeout=remaining(), **kwargs)
            if os.getenv("CALORIE_SERVICE_AUTH", "bearer") == "cam":
                from .scf_dispatch import function_url_headers
                request.headers.update(function_url_headers(request))
                request.headers["X-Calorie-Service-Token"] = self.token
            else:
                request.headers["Authorization"] = "Bearer " + self.token
            response = self.client.send(request)
            response.raise_for_status()
            return response.json()

    def submit(self, message, *, source, timeout=180):
        rid = None
        try:
            with deadline_scope(time.monotonic() + remaining_timeout(timeout)):
                accepted = self._request("POST", "/internal/intake", json={"message": message, "source": source})
                rid = accepted["receipt_id"]
                while True:
                    state = self._request("GET", "/internal/result", params={"receipt_id": rid})
                    if state.get("result"):
                        digest = state["delivery"].get("result_digest")
                        return {**state["result"], "delivery_reference": {
                            "receipt_id": rid, "uuid": state["delivery"]["uuid"], "source": source,
                            **({"result_digest": digest} if digest else {}),
                            "message": message}}
                    if state.get("status") == "needs_attention":
                        raise RuntimeError(state.get("error", "message requires recovery"))
                    time.sleep(min(0.5, remaining()))
        except (TimeoutError, httpx.TimeoutException) as exc:
            state = "durable message remains accepted" if rid else "durable message acceptance is unknown"
            raise TimeoutError(state + "; retry with the original message key: " + str(message["message_id"])) from exc

    def presented(self, reference):
        digest = reference.get("result_digest")
        suffix = ":presented:" + digest[:16] if digest else ":presented"
        message = {**reference["message"], "message_id": reference["message"]["message_id"] + suffix, "text": ""}
        return self._request("POST", "/internal/presented", json={
            "message": message, "source": reference["source"], "reference": {
                "receipt_id": reference["receipt_id"], "uuid": reference["uuid"],
                **({"result_digest": digest} if digest else {}), "presented_at": time.time()}})

    def submit_reminder(self, scheduled_time):
        # This timer is itself running in the serial message function. Waiting
        # for the newly queued execution here would block its own consumer.
        return self._request("POST", "/internal/reminder", json={"scheduled_time": scheduled_time})
