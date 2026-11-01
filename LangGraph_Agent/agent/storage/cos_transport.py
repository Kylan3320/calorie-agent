"""Production COS object boundary; all writes prohibit overwrite."""
import os
from .telemetry import span
from .timeutil import remaining_timeout


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
            SecretId=secret_id, SecretKey=secret_key, Token=token, Scheme='https', Timeout=remaining_timeout(8)), retry=0)
        with span('cos_versioning'):
            versioning = self.client.get_bucket_versioning(Bucket=self.bucket)
        if versioning.get('Status') in {'Enabled', 'Suspended'}:
            raise ValueError('COS commit bucket must have versioning disabled')

    def _budget(self):
        # Pin the supported SDK's configured requests timeout to this attempt.
        # Requests uses inactivity timeouts; this is not a wall-clock interrupt.
        self.client._conf._timeout = remaining_timeout(8)

    def get(self, key):
        from qcloud_cos.cos_exception import CosServiceError
        self._budget()
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
        self._budget()
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
            self._budget()
            with span('cos_list', prefix=prefix):
                result = self.client.list_objects(Bucket=self.bucket, Prefix=prefix, Marker=marker, MaxKeys=1000)
            keys.extend(row['Key'] for row in result.get('Contents', []))
            if str(result.get('IsTruncated', 'false')).lower() != 'true': return keys
            marker = result['NextMarker']
