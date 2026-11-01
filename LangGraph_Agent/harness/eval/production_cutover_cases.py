"""Offline cutover checks; every cloud boundary is an injected transport."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'deploy'))
import cloudshell_cutover as cutover


def save(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def report(root, passed=True, build=cutover.BUILD):
    path = root/'candidate.json'
    save(path, {'release_gate_passed': passed, 'fixed_plan': {'build_id': build,
         'bucket': cutover.BUCKET, 'prefix': 'cos-probe/production-candidate/20261102/recheck-v2/'},
         'archive': {'status': 'passed'}, 'cleanup': {'status': 'passed'}})
    return path


def controller(root, call, passed=True):
    events = []
    obj = cutover.Cutover(report(root, passed), root=root, call=call, emit=lambda name, data: events.append(deepcopy(data)),
                          ready=lambda name: call('GetFunction', cutover.identity(name)))
    obj.frozen = lambda: {'build_id': cutover.BUILD, 'sha256': cutover.PACKAGE_SHA}
    return obj, events


def CUT01():
    """Failed/mismatched actual candidate gates prevent every formal mutation."""
    for passed, build in ((False, cutover.BUILD), (True, 'different-build')):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); calls = []
            obj, _ = controller(root, lambda action, payload: calls.append(action), passed)
            report(root, passed, build)
            try:
                obj.write('UpdateFunctionConfiguration', cutover.identity(cutover.INGRESS))
            except cutover.CutoverError:
                pass
            else:
                raise AssertionError('unpassed candidate was allowed to mutate production')
            assert calls == []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        obj = cutover.Cutover(report(root), root=root, emit=lambda *args: None)
        save(root/'frozen-build.json', {'build_id': cutover.BUILD, 'sha256': cutover.PACKAGE_SHA})
        (root/'calorie-agent-v4.zip').write_bytes(b'wrong package')
        try: obj.gate()
        except cutover.CutoverError: pass
        else: raise AssertionError('wrong frozen ZIP hash was accepted')


def CUT02():
    """An ambiguous SCF write is retained and never blindly replayed or leaked."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); calls = []
        def cloud(action, payload):
            calls.append(action)
            raise TimeoutError('injected response loss')
        obj, events = controller(root, cloud)
        request = dict(cutover.identity(cutover.INGRESS), Environment={'Variables': [{'Key': 'SECRET', 'Value': 'do-not-record-this-secret'}]})
        try: obj.write('UpdateFunctionConfiguration', request)
        except TimeoutError: pass
        else: raise AssertionError('ambiguous write should stop the stage')
        assert obj.state['operations'][-1]['status'] == 'unknown'
        try: obj.write('UpdateFunctionConfiguration', request)
        except cutover.CutoverError: pass
        else: raise AssertionError('ambiguous write was replayed')
        assert calls == ['UpdateFunctionConfiguration']
        assert 'do-not-record-this-secret' not in json.dumps(events)
        assert 'do-not-record-this-secret' not in obj.state_path.read_text()
        obj.reconciled('UpdateFunctionConfiguration', cutover.INGRESS)
        obj.call = lambda action, payload: {'RequestId': 'after-independent-readback'}
        assert obj.write('UpdateFunctionConfiguration', request)['RequestId'] == 'after-independent-readback'
        for action in ('Invoke', 'InvokeFunctionUrl', 'DeleteFunction', 'CreatePolicy'):
            try: obj.write(action, cutover.identity(cutover.INGRESS))
            except cutover.CutoverError: pass
            else: raise AssertionError('unapproved control operation')


def CUT03():
    """Minute timer equivalence, raw UpdateTrigger and private original backups."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); calls = []; zip_body = b'original-function-zip'
        timer = {'Type': 'timer', 'TriggerName': 'actual-existing-minute', 'TriggerDesc': '{"cron":"0 */1 * * * * *"}', 'Enable': 1}
        http = {'Type': 'http', 'TriggerName': 'actual-http', 'TriggerDesc': json.dumps({'NetConfig': {'ExtranetURL': 'https://actual.example.test'}})}
        configs = {name: {'FunctionName': name, 'Type': 'HTTP' if name == cutover.INGRESS else 'Event', 'Status': 'Active',
                         'Timeout': 120, 'MemorySize': 512, 'Environment': {'Variables': [{'Key': 'FEISHU_VERIFICATION_TOKEN', 'Value': 'private-old-token'}]}}
                   for name in (cutover.INGRESS, cutover.MESSAGE)}
        def cloud(action, payload):
            calls.append((action, deepcopy(payload)))
            if action == 'GetFunction': return deepcopy(configs[payload['FunctionName']])
            if action == 'GetFunctionAddress': return {'Url': 'https://signed.example.test/original.zip', 'CodeSha256': hashlib.sha256(zip_body).hexdigest()}
            if action == 'ListTriggers': return {'Triggers': [deepcopy(http if payload['FunctionName'] == cutover.INGRESS else timer)]}
            if action == 'UpdateTrigger':
                assert payload['TriggerDesc'] == '0 */1 * * * * *' and payload['Enable'] == 'CLOSE'
                timer['Enable'] = 0
                return {'RequestId': 'timer-closed'}
            raise AssertionError('unexpected cloud operation: '+action)
        obj, events = controller(root, cloud)
        obj.fetch = lambda url: zip_body
        obj.inventory()
        assert all(action in cutover.READS for action, _ in calls)
        assert obj.state['stages']['inventory']['actual_http_trigger_urls'] == ['https://actual.example.test']
        assert 'private-old-token' not in json.dumps(events)
        if os.name != 'nt':
            assert all(path.stat().st_mode & 0o077 == 0 for path in obj.private_dir.iterdir())
        original_timer = obj.reminder_timers()[0]
        obj.set_timer(cutover.MESSAGE, original_timer, False)
        assert not cutover.enabled(timer)
        assert [action for action, _ in calls].count('UpdateTrigger') == 1
        obj.set_timer(cutover.MESSAGE, original_timer, False)
        assert [action for action, _ in calls].count('UpdateTrigger') == 1
        try: cutover.http_urls([{'Type': 'http', 'TriggerDesc': '{"NetConfig":{"EnableExtranet":true}}'}])
        except cutover.CutoverError: pass
        else: raise AssertionError('an absent HTTP URL must not be guessed')


def CUT04():
    """Migration verification uses a fresh exact COS version, sessions and dedup."""
    from agent.storage.cos_store import CosStore
    from agent.storage.cos_journal import CommitUnavailable
    from agent.storage.migration import baseline_manifest, import_cos_baseline
    class Objects:
        def __init__(self): self.data, self.hidden = {}, set()
        def list(self, prefix): return [key for key in self.data if key.startswith(prefix)]
        def get(self, key): return None if key in self.hidden else self.data.get(key)
        def create(self, key, body):
            if key in self.data: return False
            self.data[key] = body; return True
    objects = Objects()
    rows = [{'record_id': identity, 'user_id': 'owner', 'date': '2026-11-02', 'message_id': 'old-inbound:agent:1',
             'status': status, 'food_name': 'source food', 'food_id': 'food', 'meal_type': 'lunch',
             'created_at': 1000, 'grams': 100, 'nutrition': {'kcal': 105, 'carbs': 0, 'fat': 0, 'protein': 0}, 'snapshot': {}}
            for identity, status in (('original-row', 'valid'), ('original-deleted', 'deleted'))]
    tasks = [{'record_id': 'old-session', 'pending_id': 'old-session', 'user_id': 'owner', 'chat_id': 'chat',
              'source_message_id': 'agent_session:owner:chat', 'intent': 'agent_session', 'status': 'pending',
              'payload': {'refs': ['original-row', 'original-deleted'], 'last_meal': ['original-row']}},
             {'record_id': 'old-receipt', 'pending_id': 'old-receipt', 'user_id': 'owner', 'chat_id': 'chat',
              'source_message_id': 'old-inbound', 'intent': 'message_received', 'status': 'completed',
              'payload': {'delivery': {'uuid': 'stable-delivery', 'sent_at': 1000}}}]
    data = {'schema': 'calorie-agent.baseline.v1', 'intake': rows, 'pending': tasks,
            'foods': [], 'knowledge': [], 'aliases': {}, 'standards': []}
    writer = CosStore(objects=objects, prefix=cutover.PREFIX)
    import_cos_baseline(writer, data)
    with tempfile.TemporaryDirectory() as directory:
        obj, _ = controller(Path(directory), lambda *args: None)
        rt = {'CosStore': CosStore, 'manifest': baseline_manifest}
        proof = obj.independent_baseline(rt, objects, data, writer.journal.revision)
        assert proof['fresh_store_instance'] and 'dedup' in proof['verified_keys']
        objects.hidden.add(writer.journal.key(writer.journal.revision))
        assert writer.intake_items('owner')  # The original instance still has cached facts.
        try: obj.independent_baseline(rt, objects, data, writer.journal.revision)
        except CommitUnavailable: pass
        else: raise AssertionError('a fresh read must not reuse the writer cache when a committed version is invisible')


def CUT05():
    """Only Web accepts an identical member set in a repackaged ZIP; Event stays exact."""
    def package(compression, changed=False, duplicate=False):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=compression) as archive:
            archive.writestr('BUILD_ID', cutover.BUILD)
            archive.writestr('app.py', b'changed source' if changed else b'frozen source')
            archive.writestr('scf_bootstrap', b'python3 app.py\n')
            if duplicate:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive.writestr('app.py', b'frozen source')
        return output.getvalue()
    frozen = package(zipfile.ZIP_STORED)
    repacked = package(zipfile.ZIP_DEFLATED)
    assert hashlib.sha256(frozen).digest() != hashlib.sha256(repacked).digest()
    web = cutover.package_proof(cutover.INGRESS, repacked, frozen)
    assert web and web['verification'] == 'web_all_members_equal'
    assert web['actual_zip_sha256'] == hashlib.sha256(repacked).hexdigest()
    assert web['frozen_package_sha256'] == hashlib.sha256(frozen).hexdigest()
    assert cutover.package_proof(cutover.INGRESS, frozen, frozen)['semantic_package_sha256'] == web['semantic_package_sha256']
    assert cutover.package_proof(cutover.MESSAGE, repacked, frozen) is None
    assert cutover.package_proof(cutover.ARCHIVE, repacked, frozen) is None
    assert cutover.package_proof(cutover.MESSAGE, frozen, frozen)
    changed = package(zipfile.ZIP_DEFLATED, changed=True)
    for name in (cutover.INGRESS, cutover.MESSAGE, cutover.ARCHIVE):
        assert cutover.package_proof(name, changed, frozen) is None
    assert cutover.package_proof(cutover.INGRESS, package(zipfile.ZIP_DEFLATED, duplicate=True), frozen) is None


CHECKS = {key: globals()[key] for key in ('CUT01', 'CUT02', 'CUT03', 'CUT04', 'CUT05')}
