"""Optional real COS transport for the isolated probe; never a live default."""
import os
import time


class CosObjects:
    def __init__(self, bucket, *, region="ap-shanghai", max_requests=500, max_bytes=64*1024*1024):
        from qcloud_cos import CosConfig, CosS3Client
        self.bucket = bucket
        self.events = []
        self.max_requests, self.max_bytes, self.bytes = max_requests, max_bytes, 0
        self.client = CosS3Client(CosConfig(
            Region=region, SecretId=os.environ["TENCENTCLOUD_SECRET_ID"],
            SecretKey=os.environ["TENCENTCLOUD_SECRET_KEY"],
            Token=os.getenv("TENCENTCLOUD_SESSION_TOKEN"), Scheme="https", Timeout=8), retry=0)
        # No-overwrite is ineffective on versioned buckets. Refuse them.
        self._call("versioning", lambda: self.client.get_bucket_versioning(Bucket=bucket))
        if self.events[-1]["response"].get("Status") in {"Enabled", "Suspended"}:
            raise ValueError("probe requires a bucket that has never enabled versioning")
        self.events[-1].pop("response", None)

    def _call(self, operation, fn, size=0):
        if len(self.events) >= self.max_requests or self.bytes + size > self.max_bytes:
            raise RuntimeError("isolated COS request/byte budget reached")
        started = time.perf_counter()
        event = {"operation": operation, "outcome": "error"}
        self.events.append(event)
        self.bytes += size
        try:
            result = fn()
            event.update(outcome="ok", response=result)
            return result
        finally:
            event["duration_ms"] = round((time.perf_counter()-started)*1000, 3)

    def get(self, key):
        from qcloud_cos.cos_exception import CosServiceError
        def download():
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            stream = response["Body"].get_raw_stream()
            try:
                body = stream.read(self.max_bytes-self.bytes+1)
            finally:
                stream.close()
            self.bytes += len(body)
            if self.bytes > self.max_bytes:
                raise RuntimeError("isolated COS byte budget reached")
            return body
        try:
            body = self._call("get", download)
        except CosServiceError as exc:
            if exc.get_error_code() == "NoSuchKey":
                self.events[-1]["outcome"] = "missing"
                return None
            raise
        self.events[-1].pop("response", None)
        return body

    def create(self, key, body):
        from qcloud_cos.cos_exception import CosServiceError
        try:
            self._call("create", lambda: self.client.put_object(
                Bucket=self.bucket, Key=key, Body=body,
                Metadata={"x-cos-forbid-overwrite": "true"},
                ContentType="application/json", EnableMD5=True), len(body))
            self.events[-1].pop("response", None)
            return True
        except CosServiceError as exc:
            if exc.get_status_code() == 409 and exc.get_error_code() in {"ObjectAlreadyExists", "FileAlreadyExists"}:
                self.events[-1]["outcome"] = "exists"
                return False
            raise

    def list(self, prefix):
        keys, marker = [], ""
        while True:
            response = self._call("list", lambda: self.client.list_objects(
                Bucket=self.bucket, Prefix=prefix, Marker=marker, MaxKeys=1000))
            self.events[-1].pop("response", None)
            keys.extend(item["Key"] for item in response.get("Contents", []))
            if str(response.get("IsTruncated", "false")).lower() != "true":
                return keys
            marker = response["NextMarker"]
