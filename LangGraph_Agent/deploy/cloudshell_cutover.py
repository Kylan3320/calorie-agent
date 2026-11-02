"""Historical 2026-11-02 first cutover/recovery; routine updates follow README.md.

No stage runs at import time. Keep original release identities for recovery.

Management stages support Python 3.6; migrate requires the frozen Python 3.10
workspace. Supply --candidate-report explicitly. production-private.json holds
CALORIE_SERVICE_URL, CALORIE_SERVICE_TOKEN and GUARD_MESSAGE (a valid, read-only
internal message), or GUARD_USER_ID/GUARD_CHAT_ID. Never put secrets in reports.
No policy changes, deletes, provisioned instances, or SCF Invoke APIs are used.
"""
import argparse
import base64
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import zipfile

from cloudshell_release import ROOT, api, identity, wait_ready, record

PREFIX = 'calorie-agent/production/20261102/'
BUCKET = 'calorie-cos-probe-20261101-1440448581'
BUILD = 'cos-production-4c3fe0d5ea618cc5'
PACKAGE_SHA = 'd81e0cfc0477f6120cdaa003334e042ea6e5d3db4ad951bc0819fc881a492564'
LAYER_SHA = '3ac484a780b3adff15d2c2c625a67ac4fd05247994dc1ee6193071e9f7662936'
INDEX = 'sourcefix-20261102-69ca0bd3e30f6317'
INGRESS, MESSAGE, ARCHIVE = 'calorie-agent-api-v3-test', 'calorie-agent-reminder', 'calorie-agent-archive'
ROLES = {kind: 'calorie-agent-cos-'+kind+'-role' for kind in ('ingress', 'message', 'archive')}
ARCHIVE_TIMER, ARCHIVE_CRON = 'calorie-maintenance-five-minutes', '0 */5 * * * * *'
READS = {'GetFunction', 'GetFunctionAddress', 'ListTriggers', 'GetReservedConcurrencyConfig', 'GetLayerVersion'}
WRITES = {'CreateFunction', 'CreateTrigger', 'UpdateTrigger', 'UpdateFunctionCode', 'UpdateFunctionConfiguration', 'PutReservedConcurrencyConfig'}
SOURCE_KEYS = ('BITABLE_APP_TOKEN', 'BITABLE_FOOD_TABLE_ID', 'BITABLE_INTAKE_TABLE_ID',
               'BITABLE_PENDING_TABLE_ID', 'BITABLE_STANDARD_TABLE_ID', 'BITABLE_KNOWLEDGE_TABLE_ID',
               'FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'DEEPSEEK_API_KEY', 'SILICONFLOW_API_KEY')


class CutoverError(RuntimeError):
    pass


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(body):
    return hashlib.sha256(body).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def document_intent_hash(operation):
    """Recover a document hash only from its address AND the original intent."""
    prefix = PREFIX+'message/documents/'
    key = operation['target']
    if not key.startswith(prefix) or not key.endswith('.json'):
        raise CutoverError('only original content-addressed documents may be resumed')
    digest = key[len(prefix):-5]
    if not re.fullmatch('[0-9a-f]{64}', digest):
        raise CutoverError('original document key is not a SHA256 address')
    if operation.get('body_sha256') not in (None, digest):
        raise CutoverError('original document body hash differs from its address')
    if operation.get('payload_sha256') != sha(canonical({'body_sha256': digest})):
        raise CutoverError('original document intent does not pin this address hash')
    return digest


def package_proof(name, actual_body, frozen_body):
    """SCF Web may repackage an identical ZIP; Event code stays byte-exact.

    Production callers have already pinned frozen_body to PACKAGE_SHA. A Web
    exception compares every member, rejects duplicates/extra/missing entries,
    and retains the immutable BUILD_ID. It does not accept a subset of sources.
    """
    actual_sha, frozen_sha = sha(actual_body), sha(frozen_body)
    if name != INGRESS and actual_sha != frozen_sha:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(actual_body)) as actual, zipfile.ZipFile(io.BytesIO(frozen_body)) as frozen:
            actual_names, frozen_names = actual.namelist(), frozen.namelist()
            if (len(set(actual_names)) != len(actual_names) or len(set(frozen_names)) != len(frozen_names)
                or set(actual_names) != set(frozen_names)):
                return None
            if actual.read('BUILD_ID').decode().strip() != BUILD or frozen.read('BUILD_ID').decode().strip() != BUILD:
                return None
            members = []
            for member in sorted(frozen_names):
                if actual.getinfo(member).file_size != frozen.getinfo(member).file_size:
                    return None
                expected = frozen.read(member)
                if actual.read(member) != expected:
                    return None
                members.append([member, sha(expected)])
    except (zipfile.BadZipFile, KeyError, UnicodeError):
        return None
    return {'actual_zip_sha256': actual_sha, 'frozen_package_sha256': frozen_sha,
            'semantic_package_sha256': sha(canonical(members)), 'verified_members': len(members),
            'build_id': BUILD, 'verification': 'exact_zip_sha256' if actual_sha == frozen_sha else 'web_all_members_equal'}


def environment(config):
    return {row['Key']: row['Value'] for row in config.get('Environment', {}).get('Variables', [])}


def enabled(trigger):
    return trigger.get('Enable') in (1, True, 'OPEN')


def raw_cron(trigger):
    value = trigger['TriggerDesc']
    if value.startswith('{'):
        value = json.loads(value)['cron']
    return ' '.join(value.split())


def cron(trigger):
    return ' '.join('*' if part == '*/1' else part for part in raw_cron(trigger).split())


def http_urls(triggers):
    """Only URLs actually supplied in HTTP trigger metadata; no domain synthesis."""
    found = set()
    def walk(value):
        if isinstance(value, dict):
            for nested in value.values(): walk(nested)
        elif isinstance(value, list):
            for nested in value: walk(nested)
        elif isinstance(value, str):
            if value.startswith('https://') and not urlsplit(value).query:
                found.add(value.rstrip('/'))
            elif value.startswith(('{', '[')):
                walk(json.loads(value))
    for trigger in triggers:
        if trigger.get('Type') == 'http': walk(trigger)
    if not found:
        raise CutoverError('HTTP trigger returned no actual HTTPS URL; inspect its metadata, do not guess')
    return sorted(found)


def download(url):
    if not url.startswith('https://'):
        raise CutoverError('artifact download requires the actual HTTPS API location')
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def guard_post(url, payload, token):
    request = urllib.request.Request(url, data=canonical(payload), method='POST', headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer '+token})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


class Cutover:
    def __init__(self, candidate_report=None, root=ROOT, call=api, emit=record,
                 ready=wait_ready, fetch=download, probe=guard_post, now=time.time):
        self.root, self.report_path = Path(root), candidate_report
        self.call, self.emit, self.ready, self.fetch, self.probe, self.now = call, emit, ready, fetch, probe, now
        self._state_lock = threading.RLock()
        self.private_dir = self.root / 'production-backup-private'
        self.state_path = self.root / 'production-cutover-state.json'
        self.state = load(self.state_path) if self.state_path.exists() else {'operations': [], 'stages': {}}
        self._package_stamp = None

    def save(self):
        with self._state_lock:
            temporary = self.state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.state_path)
            self.emit('production-cutover-state', self.state)

    def private_file(self, name, body):
        self.private_dir.mkdir(exist_ok=True)
        os.chmod(str(self.private_dir), 0o700)
        path = self.private_dir / name
        if path.exists():
            if path.read_bytes() != body:
                raise CutoverError('refusing to replace a different private backup: '+name)
            return path
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream: stream.write(body)
        return path

    def original(self, name):
        return load(self.private_dir / (name+'-config.json'))

    def original_triggers(self, name):
        return load(self.private_dir / (name+'-triggers.json'))['Triggers']

    def private(self):
        path = self.root / 'production-private.json'
        if os.name != 'nt' and path.stat().st_mode & 0o077:
            raise CutoverError('production-private.json must be private mode 0600')
        data = load(path)
        if not data.get('CALORIE_SERVICE_TOKEN') or not data.get('CALORIE_SERVICE_URL', '').startswith('https://'):
            raise CutoverError('provide the approved production service URL/token privately')
        if data['CALORIE_SERVICE_URL'].rstrip('/') not in http_urls(self.original_triggers(INGRESS)):
            raise CutoverError('private service URL does not match the actual original HTTP trigger')
        return data

    def frozen(self):
        manifest = load(self.root / 'frozen-build.json')
        if manifest.get('build_id') != BUILD or manifest.get('sha256') != PACKAGE_SHA:
            raise CutoverError('frozen production build manifest does not match the reviewed candidate')
        package = self.root / 'calorie-agent-v4.zip'
        stamp = (package.stat().st_size, package.stat().st_mtime_ns)
        if stamp != self._package_stamp:
            body = package.read_bytes()
            if sha(body) != PACKAGE_SHA:
                raise CutoverError('frozen production ZIP hash differs')
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                if archive.read('BUILD_ID').decode().strip() != BUILD:
                    raise CutoverError('frozen ZIP BUILD_ID differs')
            self._package_stamp = stamp
        layer = load(self.root / 'candidate-layer.json')
        if (layer.get('LayerName'), layer.get('LayerVersion'), layer.get('CodeSha256'), layer.get('index_version')) != ('calorie-rag', 7, LAYER_SHA, INDEX):
            raise CutoverError('candidate layer identity differs from verified layer 7')
        return manifest

    def gate(self):
        if not self.report_path:
            raise CutoverError('an explicit actual candidate final report is required')
        report = load(self.report_path)
        plan = report.get('fixed_plan', {})
        if report.get('release_gate_passed') is not True or plan.get('build_id') != BUILD:
            raise CutoverError('candidate final release gate is not passed for the frozen BUILD_ID')
        if not plan.get('prefix', '').startswith('cos-probe/production-candidate/20261102/') or plan.get('bucket') != BUCKET:
            raise CutoverError('candidate report is not from the approved isolated namespace')
        if report.get('archive', {}).get('status') != 'passed' or report.get('cleanup', {}).get('status') != 'passed':
            raise CutoverError('candidate archive and cleanup evidence are incomplete')
        self.frozen()
        return sha(Path(self.report_path).read_bytes())

    def read(self, action, payload):
        if action not in READS:
            raise CutoverError('unapproved read operation')
        return self.call(action, payload)

    def get_function(self, name, absent_ok=False):
        try:
            return self.read('GetFunction', identity(name))
        except RuntimeError as exc:
            if absent_ok and str(exc).split(':')[1:2] in (['ResourceNotFound.Function'], ['ResourceNotFound.FunctionName']):
                return None
            raise

    def intent(self, action, target, payload):
        self.gate()  # Re-read the actual report and frozen manifest before every write.
        digest = sha(canonical(payload))
        unresolved = [op for op in self.state['operations'] if op['action'] == action and op['target'] == target and op['status'] in ('pending', 'unknown')]
        if unresolved:
            raise CutoverError('previous write is unresolved; read back its exact target before any replay')
        event = {'action': action, 'target': target, 'payload_sha256': digest, 'status': 'pending', 'started_at': self.now()}
        self.state['operations'].append(event)
        self.save()
        return event

    def reconciled(self, action, target):
        for op in self.state['operations']:
            if op['action'] == action and op['target'] == target and op['status'] in ('pending', 'unknown'):
                op.update(status='reconciled_by_readback', reconciled_at=self.now())
        self.save()

    def write(self, action, payload):
        if action not in WRITES or payload.get('FunctionName') not in (INGRESS, MESSAGE, ARCHIVE):
            raise CutoverError('unapproved production write')
        target = payload['FunctionName'] + (':'+payload['TriggerName'] if 'TriggerName' in payload else '')
        event = self.intent(action, target, payload)
        try:
            result = self.call(action, payload)
        except Exception:
            event.update(status='unknown', stopped_at=self.now())
            self.save()
            raise
        event.update(status='responded', request_id=result.get('RequestId'), responded_at=self.now())
        self.save()
        return result

    def mark(self, stage, data):
        self.state['stages'][stage] = dict(data, completed_at=self.now(), candidate_report_sha256=self.gate())
        self.save()

    def require(self, stage):
        if stage not in self.state['stages']:
            raise CutoverError('required earlier stage is incomplete: '+stage)
        return self.state['stages'][stage]

    def triggers(self, name):
        return self.read('ListTriggers', identity(name)).get('Triggers', [])

    def code(self, name):
        address = self.read('GetFunctionAddress', identity(name))
        body = self.fetch(address['Url'])
        actual = sha(body)
        reported = address.get('CodeSha256')
        if reported and reported != actual:
            raise CutoverError('downloaded code differs from the function-address checksum')
        return body, actual, address.get('RequestId')

    def verify_layer(self):
        result = self.read('GetLayerVersion', {'LayerName': 'calorie-rag', 'LayerVersion': 7})
        if result.get('Status') != 'Active' or 'Python3.10' not in result.get('CompatibleRuntimes', []):
            raise CutoverError('reviewed layer 7 is not active and compatible')
        body = self.fetch(result['Location'])
        if sha(body) != LAYER_SHA:
            raise CutoverError('actual layer 7 content hash differs')
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            if archive.read('rag_index/INDEX_VERSION').decode().strip() != INDEX:
                raise CutoverError('actual layer index version differs')
        self.emit('production-layer-readback', {'layer': 7, 'sha256': LAYER_SHA, 'index_version': INDEX, 'request_id': result.get('RequestId')})

    def merged_env(self, worker=False):
        old = environment(self.original(INGRESS))
        if worker:
            old = dict(environment(self.original(MESSAGE)), **old)
        secret = self.private()
        supplements = secret.get('SOURCE_ENV_IF_MISSING', {})
        if set(supplements) - set(SOURCE_KEYS) - {'BITABLE_ALIASES_TABLE_ID', 'USER_ID_ALIASES'}:
            raise CutoverError('private source supplement contains an unapproved key')
        supplied = []
        for key, value in supplements.items():
            if not old.get(key) and value:
                old[key] = value
                supplied.append(key)
        missing = [key for key in SOURCE_KEYS if not old.get(key)]
        if missing:
            raise CutoverError('original production environment lacks required keys: '+','.join(missing))
        if supplied:
            self.emit('production-explicit-source-supplements', {'keys': sorted(supplied), 'values_recorded': False})
        old.update(PRESET='live', COS_BUCKET=BUCKET, COS_PREFIX=PREFIX, COS_REGION='ap-shanghai',
                   CALORIE_MESSAGE_FUNCTION=MESSAGE, CALORIE_SERVICE_AUTH='bearer',
                   CALORIE_SERVICE_URL=secret['CALORIE_SERVICE_URL'].rstrip('/'),
                   CALORIE_SERVICE_TOKEN=secret['CALORIE_SERVICE_TOKEN'])
        return old

    def config_matches(self, actual, wanted):
        for key, value in wanted.items():
            if key in ('FunctionName', 'Namespace'): continue
            if key == 'Environment':
                if environment(actual) != environment({'Environment': value}): return False
            elif key == 'Layers':
                pairs = lambda rows: [(row['LayerName'], row['LayerVersion']) for row in rows]
                if pairs(actual.get('Layers', [])) != pairs(value): return False
            elif key == 'InstanceConcurrencyConfig':
                if any((actual.get(key) or {}).get(field) != expected for field, expected in value.items()): return False
            elif actual.get(key) != value:
                return False
        return True

    def ensure_config(self, name, **parameters):
        wanted = dict(identity(name), **parameters)
        actual = self.get_function(name)
        if actual.get('Status') != 'Active': actual = self.ready(name)
        if not self.config_matches(actual, wanted):
            self.write('UpdateFunctionConfiguration', wanted)
            actual = self.ready(name)
        else:
            self.reconciled('UpdateFunctionConfiguration', name)
        if not self.config_matches(actual, wanted):
            raise CutoverError('function configuration readback differs: '+name)
        return actual

    def ensure_code(self, name, handler=None):
        actual = self.get_function(name)
        if actual.get('Status') != 'Active': actual = self.ready(name)
        body, _, _ = self.code(name)
        frozen_body = (self.root/'calorie-agent-v4.zip').read_bytes()
        if sha(frozen_body) != PACKAGE_SHA:
            raise CutoverError('frozen package changed before code verification')
        if package_proof(name, body, frozen_body) is None or (handler and actual.get('Handler') != handler):
            parameters = dict(identity(name), ZipFile=base64.b64encode((self.root/'calorie-agent-v4.zip').read_bytes()).decode())
            if handler: parameters['Handler'] = handler
            self.write('UpdateFunctionCode', parameters)
            self.ready(name)
        else:
            self.reconciled('UpdateFunctionCode', name)
        actual = self.get_function(name)
        body, digest, request_id = self.code(name)
        proof = package_proof(name, body, frozen_body)
        if proof is None or (handler and actual.get('Handler') != handler):
            raise CutoverError('function code readback differs: '+name)
        self.emit('production-code-'+name, dict(proof, sha256=digest, get_request_id=request_id))

    def reserve_one(self, name, memory):
        actual = self.read('GetReservedConcurrencyConfig', identity(name))
        if actual.get('ReservedMem') != memory:
            self.write('PutReservedConcurrencyConfig', dict(identity(name), ReservedConcurrencyMem=memory))
        else:
            self.reconciled('PutReservedConcurrencyConfig', name)
        if self.read('GetReservedConcurrencyConfig', identity(name)).get('ReservedMem') != memory:
            raise CutoverError('global one-instance quota is not confirmed: '+name)

    def set_timer(self, name, trigger, opened):
        current = next((row for row in self.triggers(name) if row['TriggerName'] == trigger['TriggerName']), None)
        if not current or current['Type'] != 'timer' or cron(current) != cron(trigger):
            raise CutoverError('timer identity/cron changed')
        target = name+':'+current['TriggerName']
        if enabled(current) != opened:
            self.write('UpdateTrigger', dict(identity(name), TriggerName=current['TriggerName'], Type='timer',
                Qualifier=current.get('Qualifier', '$LATEST'), TriggerDesc=raw_cron(current), Enable='OPEN' if opened else 'CLOSE'))
        else:
            self.reconciled('UpdateTrigger', target)
        readback = next(row for row in self.triggers(name) if row['TriggerName'] == trigger['TriggerName'])
        if enabled(readback) != opened:
            raise CutoverError('timer status was not confirmed')

    def reminder_timers(self):
        rows = [row for row in self.original_triggers(MESSAGE) if row['Type'] == 'timer' and enabled(row)]
        if len(rows) != 1 or cron(rows[0]) != '0 * * * * * *':
            raise CutoverError('original enabled minute reminder timer is not unambiguous')
        return rows

    def inventory(self):
        if self.state.get('stages', {}).get('inventory'):
            raise CutoverError('original inventory is already frozen; do not replace it after cutover work')
        summary = []
        for name in (INGRESS, MESSAGE):
            config = self.get_function(name)
            triggers = self.read('ListTriggers', identity(name))
            body, digest, request_id = self.code(name)
            self.private_file(name+'-config.json', canonical(config))
            self.private_file(name+'-triggers.json', canonical(triggers))
            self.private_file(name+'-code.zip', body)
            summary.append({'function': name, 'status': config.get('Status'), 'type': config.get('Type'),
                'runtime': config.get('Runtime'), 'handler': config.get('Handler'), 'timeout': config.get('Timeout'),
                'role': config.get('Role'), 'code_sha256': digest, 'code_address_request_id': request_id,
                'config_request_id': config.get('RequestId'), 'triggers_request_id': triggers.get('RequestId')})
        if self.original(INGRESS).get('Type') not in ('HTTP', 'Web') or self.original(MESSAGE).get('Type') != 'Event':
            raise CutoverError('original Web/Event function types do not match the approved plan')
        urls = http_urls(self.original_triggers(INGRESS))
        self.reminder_timers()
        self.state['stages']['inventory'] = {'functions': summary, 'actual_http_trigger_urls': urls, 'completed_at': self.now()}
        self.save()

    def prepare(self):
        self.require('inventory'); self.gate(); self.verify_layer()
        if not self.original(INGRESS).get('ClsLogsetId') or not self.original(INGRESS).get('ClsTopicId'):
            raise CutoverError('original production CLS identifiers are required; do not create logging resources implicitly')
        env = self.merged_env(worker=True)
        env.update(KNOWLEDGE_RETRIEVER='lightrag', RAG_INDEX_DIR='/opt/rag_index', TIKTOKEN_CACHE_DIR='/opt/tiktoken_cache')
        description = 'Calorie COS production archive 20261102 '+BUILD
        wanted = dict(identity(ARCHIVE), Runtime='Python3.10', Handler='app.maintenance_handler', Type='Event',
            Description=description, MemorySize=512, Timeout=120, Role=ROLES['archive'],
            Environment={'Variables': [{'Key': k, 'Value': v} for k, v in sorted(env.items())]},
            Layers=[{'LayerName': 'calorie-rag', 'LayerVersion': 7}])
        current = self.get_function(ARCHIVE, absent_ok=True)
        if current is None:
            self.write('CreateFunction', dict(wanted, Code={'ZipFile': base64.b64encode((self.root/'calorie-agent-v4.zip').read_bytes()).decode()},
                CodeSource='ZipFile', ClsLogsetId=self.original(INGRESS).get('ClsLogsetId', ''),
                ClsTopicId=self.original(INGRESS).get('ClsTopicId', '')))
            current = self.ready(ARCHIVE)
        else:
            if not self.config_matches(current, wanted):
                raise CutoverError('existing archive differs; no overwrite or deletion is authorized')
            self.reconciled('CreateFunction', ARCHIVE)
        if not self.config_matches(current, wanted) or self.code(ARCHIVE)[1] != PACKAGE_SHA:
            raise CutoverError('new archive function readback differs')
        self.reserve_one(ARCHIVE, 512)
        timers = self.triggers(ARCHIVE)
        if timers:
            if len(timers) != 1 or timers[0]['TriggerName'] != ARCHIVE_TIMER or timers[0]['Type'] != 'timer' or cron(timers[0]) != ARCHIVE_CRON or enabled(timers[0]):
                raise CutoverError('existing archive trigger differs; will not replace it')
            self.reconciled('CreateTrigger', ARCHIVE+':'+ARCHIVE_TIMER)
        else:
            self.write('CreateTrigger', dict(identity(ARCHIVE), TriggerName=ARCHIVE_TIMER, Type='timer', TriggerDesc=ARCHIVE_CRON, Enable='CLOSE'))
        verified = self.triggers(ARCHIVE)
        if len(verified) != 1 or verified[0]['TriggerName'] != ARCHIVE_TIMER or enabled(verified[0]) or cron(verified[0]) != ARCHIVE_CRON:
            raise CutoverError('closed archive timer readback differs')
        self.mark('prepare', {'archive': ARCHIVE, 'archive_timer': ARCHIVE_TIMER, 'archive_timer_open': False})

    def maintenance(self):
        self.require('prepare'); self.gate()
        secret = self.private()
        message = secret.get('GUARD_MESSAGE') or {'message_id': 'maintenance-guard-20261102',
            'user_id': secret.get('GUARD_USER_ID'), 'chat_id': secret.get('GUARD_CHAT_ID'), 'text': '查询今天已记录的热量'}
        if not all(message.get(key) for key in ('message_id', 'user_id', 'chat_id', 'text')):
            raise CutoverError('provide a valid internal read-only guard message privately before maintenance')
        for timer in self.reminder_timers(): self.set_timer(MESSAGE, timer, False)
        old = self.original(INGRESS)
        env = self.merged_env(); env['INGRESS_MAINTENANCE'] = '1'
        self.ensure_config(INGRESS, Environment={'Variables': [{'Key': k, 'Value': v} for k, v in sorted(env.items())]},
            Role=ROLES['ingress'], Timeout=max(old['Timeout'], 120),
            InstanceConcurrencyConfig={'DynamicEnabled': 'FALSE', 'MaxConcurrency': 1})
        self.ensure_code(INGRESS)  # Keep Web type, bootstrap, handler and HTTP trigger.
        self.reserve_one(INGRESS, old['MemorySize'])
        actual = self.get_function(INGRESS)
        if (actual['Type'] != old['Type'] or actual.get('Handler') != old.get('Handler')
            or environment(actual).get('FEISHU_VERIFICATION_TOKEN') != environment(old).get('FEISHU_VERIFICATION_TOKEN')
            or http_urls(self.triggers(INGRESS)) != http_urls(self.original_triggers(INGRESS))):
            raise CutoverError('formal Web handler/type/URL changed')
        self.gate()
        status, body = self.probe(secret['CALORIE_SERVICE_URL'].rstrip('/')+'/internal/intake',
                                  {'source': 'cli', 'message': message}, secret['CALORIE_SERVICE_TOKEN'])
        if status != 503 or body.get('reason') != 'accept_or_handoff_failed':
            raise CutoverError('actual valid ingress probe did not confirm maintenance HTTP 503')
        guard_at = self.now()
        original_timeout = max(self.original(INGRESS)['Timeout'], self.original(MESSAGE)['Timeout'])
        self.mark('maintenance', {'http_status': status, 'guard_at': guard_at, 'original_max_timeout': original_timeout,
            'drain_not_before': guard_at+original_timeout, 'frozen_guard_build_id': BUILD})

    def runtime(self):
        if sys.version_info < (3, 10):
            raise CutoverError('migrate requires Python 3.10 and the frozen workspace, not management Python 3.6')
        workspace = self.root / 'workspace'
        if (workspace/'BUILD_ID').read_text().strip() != BUILD:
            raise CutoverError('extract the frozen ZIP with cloudshell_release bootstrap before migration')
        existing = sys.modules.get('agent')
        if existing and workspace.resolve() not in Path(existing.__file__).resolve().parents:
            raise CutoverError('another agent checkout is already imported; use a fresh migration process')
        sys.path[:0] = [str(workspace/'deps'), str(workspace)]
        env = self.merged_env(worker=True)
        os.environ.update(env)
        token = os.getenv('TENCENTCLOUD_SECURITY_TOKEN') or os.getenv('TENCENTCLOUD_TOKEN')
        if token: os.environ['TENCENTCLOUD_SESSION_TOKEN'] = token
        from agent.storage.bitable_adapter import BitableStore
        from agent.storage.cos_store import CosStore
        from agent.storage.cos_transport import CosObjects
        from agent.storage import feishu_client
        from agent.storage.migration import ensure_schema, export_cos_baseline, baseline_manifest, import_cos_baseline
        return {'BitableStore': BitableStore, 'CosStore': CosStore, 'CosObjects': CosObjects, 'feishu': feishu_client,
                'ensure_schema': ensure_schema, 'export': export_cos_baseline, 'manifest': baseline_manifest, 'import': import_cos_baseline}

    @contextmanager
    def guarded_feishu(self, rt, store, plan):
        feishu = rt['feishu']
        feishu.get_tenant_access_token()  # Authentication is not a table mutation.
        original = feishu._http_request
        rows = {change['record_id']: change for change in plan['changes']}
        schema = {store.food_table: {'kcal_per_100g': 2, 'units': 1, 'detail_loaded': 1, 'source': 1, 'boohee_id': 1},
                  store.intake_table: {'snapshot': 1, 'agent_item_id': 1, 'agent_revision': 2}}
        from agent.storage.bitable_adapter import _pending_field
        schema[store.pending_table] = {_pending_field('payload'): 1}
        def single(url, payload, headers, method):
            if method == 'GET': return original(url, payload, headers, method)
            parts = urlsplit(url).path.split('/')
            table = parts[parts.index('tables')+1] if 'tables' in parts else None
            target = table+':'+parts[-1] if table else 'unapproved'
            if method == 'PUT':
                change = rows.get(parts[-1])
                if table != plan['table_id'] or not change or payload != {'fields': change['fields_to_update']}:
                    raise CutoverError('unapproved Bitable source mutation')
            elif method == 'POST' and parts[-1] == 'fields':
                if payload.get('field_name') not in schema.get(table, {}) or schema[table][payload['field_name']] != payload.get('type'):
                    raise CutoverError('unapproved additive Bitable schema mutation')
            else:
                raise CutoverError('unapproved Feishu mutation')
            event = self.intent('feishu:'+method, target+':'+str(payload.get('field_name', '')), payload)
            try:
                response = feishu._CLIENT.request(method, url, content=feishu._json_bytes(payload),
                    headers={'Content-Type': 'application/json; charset=utf-8', **headers}, timeout=20)
                response.raise_for_status()
                result = response.json()
                if result.get('code') != 0: raise CutoverError('Bitable mutation was not acknowledged')
            except Exception:
                event.update(status='unknown', stopped_at=self.now()); self.save(); raise
            event.update(status='responded', request_id=result.get('request_id') or result.get('log_id'), responded_at=self.now()); self.save()
            return result
        feishu._http_request = single
        try: yield
        finally: feishu._http_request = original

    def source_updates(self, store, plan):
        from agent.storage.bitable_adapter import _list_records
        if plan['table_id'] != store.knowledge_table or {change['record_id'] for change in plan['changes']} != {'recvuqQgevbAMT', 'recvuqQgevJVjl'}:
            raise CutoverError('only the two reviewed source rows may be updated')
        before = dict(_list_records(store.app_token, store.knowledge_table))
        changed = []
        digest = lambda value: sha(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
        for change in plan['changes']:
            row = before.get(change['record_id'])
            if row is None: raise CutoverError('reviewed source row is missing')
            fields = {key: row.get(key, '') for key in ('entry_id', 'question', 'answer', 'source')}
            if digest(fields) == change['after_sha256']:
                self.reconciled('feishu:PUT', plan['table_id']+':'+change['record_id']+':')
            elif digest(fields) == change['before_sha256']:
                store._update_record(store.knowledge_table, change['record_id'], change['fields_to_update'])
            else:
                raise CutoverError('reviewed source row changed; will not overwrite it')
            changed.append(change['record_id'])
        after = dict(_list_records(store.app_token, store.knowledge_table))
        for rid in set(before) | set(after):
            if rid not in changed and before.get(rid) != after.get(rid):
                raise CutoverError('an unrelated knowledge row changed during migration')
        for change in plan['changes']:
            fields = {key: after[change['record_id']].get(key, '') for key in ('entry_id', 'question', 'answer', 'source')}
            if digest(fields) != change['after_sha256']:
                raise CutoverError('reviewed source update readback differs')
        self.emit('production-source-update-readback', {'record_ids': changed, 'other_rows_unchanged': len(before)-2,
            'after_sha256': [change['after_sha256'] for change in plan['changes']]})

    def schema_readback(self, rt, store):
        from agent.storage.bitable_adapter import _record_url, _pending_field
        feishu = rt['feishu']
        auth = {'Authorization': 'Bearer '+feishu.get_tenant_access_token()}
        wanted = {store.food_table: {'kcal_per_100g': 2, 'units': 1, 'detail_loaded': 1, 'source': 1, 'boohee_id': 1},
                  store.intake_table: {'snapshot': 1, 'agent_item_id': 1, 'agent_revision': 2},
                  store.pending_table: {_pending_field('payload'): 1}}
        for table, fields in wanted.items():
            url = _record_url(store.app_token, table).rsplit('/', 1)[0]+'/fields?page_size=100'
            observed = {}
            while True:
                response = feishu.http_json_get(url, auth)
                if response.get('code') != 0: raise CutoverError('schema readback failed')
                observed.update({row['field_name']: row['type'] for row in response['data'].get('items', [])})
                if not response['data'].get('has_more'): break
                from urllib.parse import quote
                url = url.split('&page_token=')[0]+'&page_token='+quote(response['data']['page_token'], safe='')
            for name, kind in fields.items():
                if observed.get(name) != kind:
                    raise CutoverError('required field type differs: '+name)
                self.reconciled('feishu:POST', table+':fields:'+name)

    def independent_baseline(self, rt, objects, data, minimum_revision):
        import re
        reader = rt['CosStore'](objects=objects, prefix=PREFIX, readonly=True)
        reader.journal.refresh(confirm=False, minimum_revision=minimum_revision)
        users = {row['user_id'] for row in data['intake']}
        rows = [row for user in users for row in reader.intake_items(user, include_deleted=True)]
        tasks = [reader.get_pending(task['pending_id']) for task in data['pending']]
        if any(task is None for task in tasks):
            raise CutoverError('new reader cannot find an imported pending identity')
        expected = rt['manifest'](data)
        observed = rt['manifest'](dict(data, intake=rows, pending=tasks))
        for key in ('intake_count', 'valid_count', 'daily_kcal', 'ledger_sha256', 'pending_sha256', 'pending_count'):
            if observed[key] != expected[key]:
                raise CutoverError('fresh COS reader baseline mismatch: '+key)
        if reader.journal.get('baseline') != expected['source_sha256']:
            raise CutoverError('fresh COS reader baseline watermark differs')
        if reader.load_user_aliases() != data['aliases'] or reader.load_knowledge() != data['knowledge'] or reader.journal.get('source:standards') != data['standards']:
            raise CutoverError('fresh COS reader source configuration differs')
        if sorted(reader.load_foods(), key=lambda row: row['food_id']) != sorted(data['foods'], key=lambda row: row['food_id']):
            raise CutoverError('fresh COS reader food snapshot differs')
        for task in data['pending']:
            if task['intent'] in ('agent_session', 'user_settings', 'reminder_receipt'):
                if reader.load_document(task['intent'], task['user_id'], task['chat_id']) != task['payload']:
                    raise CutoverError('fresh COS reader session/settings/reminder reference differs')
            if task['intent'] == 'message_received' and task['status'] == 'completed':
                rid = sha(('feishu:'+task['source_message_id']).encode())
                if reader.load_document('receipt_state', 'system', rid).get('status') != 'completed':
                    raise CutoverError('fresh COS reader completed message dedup state differs')
        for row in data['intake']:
            if row['message_id']:
                mid = re.sub(r':agent:\d+$', '', row['message_id'])
                if not reader.load_document('legacy_message', 'system', mid).get('completed'):
                    raise CutoverError('fresh COS reader legacy intake dedup state differs')
        return {'fresh_store_instance': True, 'cos_revision': reader.journal.revision,
                'verified_keys': ['counts', 'daily_kcal', 'nutrition', 'soft_deletes', 'old_ids', 'sources', 'sessions', 'dedup']}

    def migrate(self):
        maintenance = self.require('maintenance'); self.gate()
        if self.now() < maintenance['drain_not_before']:
            raise CutoverError('old execution timeout has not drained; rerun after drain_not_before, without a blocking sleep')
        if environment(self.get_function(INGRESS)).get('INGRESS_MAINTENANCE') != '1':
            raise CutoverError('ingress maintenance is no longer enabled')
        rt = self.runtime(); store = rt['BitableStore']()
        if store.load_unfinished_executions():
            raise CutoverError('old accepted execution/reply is still pending; do not mutate or migrate before drain')
        plan = load(self.root/'bitable-update-plan.json')
        from agent.storage.knowledge_seed import KNOWLEDGE_SEED
        approved = {row['question']: {'answer': row['answer'], 'source': row['source']} for row in KNOWLEDGE_SEED[:2]}
        if len(plan['changes']) != 2 or any(change['fields_to_update'] != approved.get(change['question']) for change in plan['changes']):
            raise CutoverError('source update plan differs from the verified frozen source repair')
        with self.guarded_feishu(rt, store, plan):
            self.source_updates(store, plan)
            added = rt['ensure_schema'](store)
            self.schema_readback(rt, store)
        data = rt['export'](store)  # Refuses uncompleted accepted executions/replies.
        if any(str(row.get('user_id', '')).startswith(('cos-probe', 'candidate-')) or str(row.get('message_id', '')).startswith(('cos-probe', 'candidate-')) for row in data['intake']):
            raise CutoverError('formal ledger contains test rows; isolate them explicitly before migration')
        manifest = rt['manifest'](data)
        self.private_file('production-baseline.json', canonical(data))
        self.private_file('production-baseline-manifest.json', canonical(manifest))
        owner = self
        objects = rt['CosObjects'](bucket=BUCKET, region='ap-shanghai')
        for operation in self.state['operations']:
            if operation['action'] == 'cos:create' and operation['status'] in ('pending', 'unknown'):
                is_document = operation['target'].startswith(PREFIX+'message/documents/')
                expected = document_intent_hash(operation) if is_document else operation.get('body_sha256')
                if is_document and not operation.get('body_sha256'):
                    operation.update(body_sha256=expected, body_hash_recovery='original_intent_payload_and_content_address',
                                     body_hash_recovered_at=self.now())
                    self.save()
                actual = objects.get(operation['target'])
                if actual is None and is_document:
                    continue  # Regenerated bytes must match this exact original intent before resuming it.
                if actual is None or sha(actual) != expected:
                    raise CutoverError('previous COS create is not yet confirmed at its original key')
                self.reconciled('cos:create', operation['target'])
        class GuardedObjects:
            def get(self, key): return objects.get(key)
            def list(self, prefix): return objects.list(prefix)
            def create(self, key, body):
                with owner._state_lock:
                    return self._create_locked(key, body)
            def _create_locked(self, key, body):
                if not key.startswith(PREFIX+'message/'):
                    raise CutoverError('migration may write only the formal message namespace')
                unresolved = [op for op in owner.state['operations'] if op['action'] == 'cos:create' and op['target'] == key and op['status'] in ('pending', 'unknown')]
                if unresolved:
                    expected = [document_intent_hash(operation) for operation in unresolved]
                    if any(digest != sha(body) for digest in expected):
                        raise CutoverError('regenerated document differs from the original pinned body')
                    raw = objects.get(key)
                    created = False
                    if raw is None:
                        owner.gate()
                        for operation in unresolved:
                            operation.update(exact_body_resume_at=owner.now(), exact_body_resume_count=operation.get('exact_body_resume_count', 0)+1)
                        owner.save()  # Retain the original intent, key and body hash; do not create another intent.
                        try:
                            created = objects.create(key, body)  # Existing transport always forbids overwrite.
                        except Exception:
                            for operation in unresolved: operation.update(status='unknown', stopped_at=owner.now())
                            owner.save(); raise
                        raw = objects.get(key)
                    if raw != body or sha(raw) != expected[0]:
                        for operation in unresolved: operation.update(status='unknown', stopped_at=owner.now())
                        owner.save()
                        raise CutoverError('exact original document create/readback is not confirmed')
                    owner.reconciled('cos:create', key)
                    return created
                prior_documents = any(operation['action'] == 'cos:create'
                    and operation['status'] in ('pending', 'unknown')
                    and operation['target'].startswith(PREFIX+'message/documents/')
                    for operation in owner.state['operations'])
                if prior_documents:
                    if key.startswith(PREFIX+'message/commits/'):
                        confirmation = json.loads(body)
                        if confirmation.get('schema') != 'calorie.cos.v1' or confirmation.get('changes') != {}:
                            raise CutoverError('confirm every original document before publishing a business manifest')
                        # An empty freshness confirmation is required by Journal;
                        # it does not publish the unconfirmed business documents.
                    elif key.startswith(PREFIX+'message/documents/'):
                        digest = key[len(PREFIX+'message/documents/'):-5] if key.endswith('.json') else ''
                        if not re.fullmatch('[0-9a-f]{64}', digest) or digest != sha(body) or objects.get(key) != body:
                            raise CutoverError('do not create a new document intent before original documents are confirmed')
                        return False  # Already committed bytes need no new create or intent.
                    else:
                        raise CutoverError('original document recovery must finish before other writes')
                event = owner.intent('cos:create', key, {'body_sha256': sha(body)})
                event['body_sha256'] = sha(body); owner.save()
                try: created = objects.create(key, body)
                except Exception:
                    event.update(status='unknown', stopped_at=owner.now()); owner.save(); raise
                event.update(status='created' if created else 'exists', responded_at=owner.now()); owner.save()
                return created
        destination = rt['CosStore'](objects=GuardedObjects(), prefix=PREFIX)
        result = rt['import'](destination, data)
        independent = self.independent_baseline(rt, GuardedObjects(), data, destination.journal.revision)
        result['fresh_reader'] = independent
        self.private_file('production-import-audit.json', canonical(result))
        self.mark('migrate', {'baseline_source_sha256': manifest['source_sha256'], 'baseline_id': destination.journal.get('baseline'),
            'intake_count': manifest['intake_count'], 'valid_count': manifest['valid_count'], 'pending_count': manifest['pending_count'],
            'ledger_sha256': manifest['ledger_sha256'], 'pending_sha256': manifest['pending_sha256'],
            'cos_revision': destination.journal.revision, 'schema_added': added, 'independent_readback': independent})

    def deploy_workers(self):
        self.require('migrate'); self.gate(); self.verify_layer()
        archive = self.get_function(ARCHIVE)
        if (archive.get('Description') != 'Calorie COS production archive 20261102 '+BUILD
            or archive.get('Role') != ROLES['archive'] or self.code(ARCHIVE)[1] != PACKAGE_SHA):
            raise CutoverError('prepared archive identity changed; will not overwrite it')
        env = self.merged_env(worker=True)
        env.update(KNOWLEDGE_RETRIEVER='lightrag', RAG_INDEX_DIR='/opt/rag_index', TIKTOKEN_CACHE_DIR='/opt/tiktoken_cache')
        for name, kind, handler in ((MESSAGE, 'message', 'app.message_handler'), (ARCHIVE, 'archive', 'app.maintenance_handler')):
            self.ensure_code(name, handler)
            self.ensure_config(name, Role=ROLES[kind], Timeout=120,
                Environment={'Variables': [{'Key': k, 'Value': v} for k, v in sorted(env.items())]},
                Layers=[{'LayerName': 'calorie-rag', 'LayerVersion': 7}])
            self.reserve_one(name, self.get_function(name)['MemorySize'])
        if any(enabled(timer) for timer in self.triggers(ARCHIVE)) or any(enabled(row) for row in self.triggers(MESSAGE) if row['Type'] == 'timer'):
            raise CutoverError('worker timers must stay closed until activation')
        self.mark('deploy-workers', {'build_id': BUILD, 'layer': 7, 'worker_timers_closed': True})

    def activate(self):
        self.require('deploy-workers'); self.require('migrate'); self.gate(); self.verify_layer()
        frozen_body = (self.root/'calorie-agent-v4.zip').read_bytes()
        if sha(frozen_body) != PACKAGE_SHA:
            raise CutoverError('frozen package changed before activation verification')
        for name, kind, handler in ((INGRESS, 'ingress', None), (MESSAGE, 'message', 'app.message_handler'), (ARCHIVE, 'archive', 'app.maintenance_handler')):
            actual = self.get_function(name)
            body, _, request_id = self.code(name)
            proof = package_proof(name, body, frozen_body)
            if actual.get('Status') != 'Active' or actual.get('Role') != ROLES[kind] or proof is None:
                raise CutoverError('production code/role/availability changed before activation')
            self.emit('production-activation-code-'+name, dict(proof, get_request_id=request_id))
            env = environment(actual)
            expected_env = self.merged_env(worker=kind != 'ingress')
            if kind == 'ingress':
                if env.get('INGRESS_MAINTENANCE') not in ('0', '1'):
                    raise CutoverError('production ingress maintenance flag changed')
                expected_env['INGRESS_MAINTENANCE'] = env['INGRESS_MAINTENANCE']
            else:
                expected_env.update(KNOWLEDGE_RETRIEVER='lightrag', RAG_INDEX_DIR='/opt/rag_index', TIKTOKEN_CACHE_DIR='/opt/tiktoken_cache')
            if env != expected_env:
                raise CutoverError('production original/service/storage/model environment changed before activation')
            if handler and (actual.get('Handler') != handler or [(row['LayerName'], row['LayerVersion']) for row in actual.get('Layers', [])] != [('calorie-rag', 7)]):
                raise CutoverError('worker handler/layer changed before activation')
            if self.read('GetReservedConcurrencyConfig', identity(name)).get('ReservedMem') != actual['MemorySize']:
                raise CutoverError('one-instance quota changed before activation')
        resumable = {('UpdateFunctionConfiguration', INGRESS), ('UpdateTrigger', ARCHIVE+':'+ARCHIVE_TIMER)}
        resumable.update(('UpdateTrigger', MESSAGE+':'+row['TriggerName']) for row in self.reminder_timers())
        if any(operation['status'] in ('pending', 'unknown') and (operation['action'], operation['target']) not in resumable for operation in self.state['operations']):
            raise CutoverError('an earlier write remains unresolved; do not activate')
        if http_urls(self.triggers(INGRESS)) != http_urls(self.original_triggers(INGRESS)):
            raise CutoverError('formal ingress URL changed before activation')
        archive = self.triggers(ARCHIVE)
        if len(archive) != 1 or archive[0]['TriggerName'] != ARCHIVE_TIMER or cron(archive[0]) != ARCHIVE_CRON:
            raise CutoverError('approved archive timer changed before activation')
        current = self.get_function(INGRESS); env = environment(current); env['INGRESS_MAINTENANCE'] = '0'
        self.ensure_config(INGRESS, Environment={'Variables': [{'Key': k, 'Value': v} for k, v in sorted(env.items())]})
        for timer in self.reminder_timers(): self.set_timer(MESSAGE, timer, True)
        self.set_timer(ARCHIVE, archive[0], True)
        if any(operation['status'] in ('pending', 'unknown') for operation in self.state['operations']):
            raise CutoverError('activation write is not independently confirmed')
        self.mark('activate', {'ingress_maintenance': False, 'reminder_enabled': True, 'archive_enabled': True,
            'formal_readonly_check': 'pending; parent must send exactly one own read-only request and independently compare baseline'})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('inventory', 'prepare', 'maintenance', 'migrate', 'deploy-workers', 'activate'))
    parser.add_argument('--candidate-report', type=Path)
    args = parser.parse_args(argv)
    cutover = Cutover(args.candidate_report)
    try:
        if args.stage != 'inventory': cutover.gate()
        getattr(cutover, args.stage.replace('-', '_'))()
        print(json.dumps({'stage': args.stage, 'status': 'completed', 'build_id': BUILD}))
        return 0
    except Exception as exc:
        # Unknown provider exceptions may contain signed URLs or request bodies.
        error = str(exc) if isinstance(exc, CutoverError) else type(exc).__name__
        record('production-cutover-failure', {'stage': args.stage, 'status': 'stopped', 'error': error})
        print(json.dumps({'stage': args.stage, 'status': 'stopped', 'error': error}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
