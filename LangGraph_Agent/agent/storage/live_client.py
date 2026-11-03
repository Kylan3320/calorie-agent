"""HTTP adapter for local live entries; business writes stay in the SCF writer."""
import os
import time
import httpx


class LiveClient:
    def __init__(self, *, client=None):
        self.endpoint = os.environ["CALORIE_SERVICE_URL"].rstrip("/")
        self.token = os.environ["CALORIE_SERVICE_TOKEN"]
        self.client = client or httpx.Client(timeout=10)

    def _request(self, method, path, **kwargs):
        request = self.client.build_request(method, self.endpoint + path, **kwargs)
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
        accepted = self._request("POST", "/internal/intake", json={"message": message, "source": source})
        rid = accepted["receipt_id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._request("GET", "/internal/result", params={"receipt_id": rid})
            if state.get("result"):
                digest = state["delivery"].get("result_digest")
                return {**state["result"], "delivery_reference": {
                    "receipt_id": rid, "uuid": state["delivery"]["uuid"], "source": source,
                    **({"result_digest": digest} if digest else {}),
                    "message": message}}
            if state.get("status") == "needs_attention":
                raise RuntimeError(state.get("error", "message requires recovery"))
            time.sleep(0.5)
        raise TimeoutError("durable message remains accepted; retry with the original message key: " + rid)

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
