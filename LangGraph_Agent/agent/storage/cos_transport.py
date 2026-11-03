"""Production COS object boundary; all writes prohibit overwrite."""
import atexit
from functools import lru_cache
from io import BytesIO
import os
import time

import requests
from urllib3.response import HTTPResponse

from .model_transport import create_client, deadline_scope, remaining
from .telemetry import span
from .timeutil import remaining_timeout


class _DeadlineAdapter(requests.adapters.BaseAdapter):
    """Keep COS SDK signing/preparation, bound and buffer its actual HTTP RPC."""
    def __init__(self):
        self.client = create_client()

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        # Production has direct endpoints and standard TLS verification. Do not
        # silently route a signed request through an unbounded proxy transport.
        if proxies or cert or verify is not True:
            raise ValueError("COS deadline transport requires direct standard TLS")
        deadline = time.monotonic() + remaining_timeout(8)
        with deadline_scope(deadline):
            with self.client.stream(request.method, request.url, headers=request.headers,
                                    content=request.body, timeout=remaining()) as received:
                body = b"".join(received.iter_raw())
                remaining()
                response = requests.Response()
                response.status_code = received.status_code
                response.headers = requests.structures.CaseInsensitiveDict(received.headers)
                response.url = request.url
                response.request = request
                response.reason = received.reason_phrase
                response.encoding = requests.utils.get_encoding_from_headers(response.headers)
                # urllib3 operates solely on these buffered bytes. Preserve its
                # normal raw/decompressed read semantics for the existing SDK.
                response.raw = HTTPResponse(body=BytesIO(body), headers=dict(response.headers),
                                            status=response.status_code, preload_content=False, decode_content=False)
                response.connection = self
                return response

    def close(self):
        self.client.close()


@lru_cache(maxsize=1)
def _cos_session():
    session = requests.Session()
    session.trust_env = False
    adapter = _DeadlineAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    atexit.register(session.close)
    return session


class CosObjects:
    def __init__(self, bucket=None, region=None, credentials=None):
        from qcloud_cos import CosConfig, CosS3Client
        self.bucket = bucket or os.environ['COS_BUCKET']
        secret_id, secret_key, token = credentials or (
            os.environ.get('TENCENTCLOUD_SECRET_ID') or os.environ['TENCENTCLOUD_SECRETID'],
            os.environ.get('TENCENTCLOUD_SECRET_KEY') or os.environ['TENCENTCLOUD_SECRETKEY'],
            os.getenv('TENCENTCLOUD_SESSION_TOKEN') or os.getenv('TENCENTCLOUD_SESSIONTOKEN'))
        self.client = CosS3Client(CosConfig(
            Region=region or os.getenv('COS_REGION', 'ap-shanghai'),
            SecretId=secret_id, SecretKey=secret_key, Token=token, Scheme='https', Timeout=8), retry=0, session=_cos_session())
        with span('cos_versioning'):
            versioning = self.client.get_bucket_versioning(Bucket=self.bucket)
        if versioning.get('Status') in {'Enabled', 'Suspended'}:
            raise ValueError('COS commit bucket must have versioning disabled')

    def get(self, key):
        from qcloud_cos.cos_exception import CosServiceError
        try:
            with span('cos_get', key=key):
                response = self.client.get_object(Bucket=self.bucket, Key=key)
                stream = response['Body'].get_raw_stream()
                try: return stream.read()
                finally: stream.close()
        except CosServiceError as exc:
            if exc.get_error_code() == 'NoSuchKey': return None
            raise

    def create(self, key, body):
        from qcloud_cos.cos_exception import CosServiceError
        try:
            with span('cos_create', key=key, bytes=len(body)):
                self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                    Metadata={'x-cos-forbid-overwrite': 'true'}, ContentType='application/json', EnableMD5=True)
            return True
        except CosServiceError as exc:
            if exc.get_status_code() == 409 and exc.get_error_code() in {'ObjectAlreadyExists', 'FileAlreadyExists'}: return False
            raise

    def list(self, prefix):
        keys, marker = [], ''
        while True:
            with span('cos_list', prefix=prefix):
                result = self.client.list_objects(Bucket=self.bucket, Prefix=prefix, Marker=marker, MaxKeys=1000)
            keys.extend(row['Key'] for row in result.get('Contents', []))
            if str(result.get('IsTruncated', 'false')).lower() != 'true': return keys
            marker = result['NextMarker']
