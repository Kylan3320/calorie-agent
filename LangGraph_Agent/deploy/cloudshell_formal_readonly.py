"""One formal read-only webhook check after approved activation (Python 3.10).

send persists its attempt before exactly one POST. resume only reads that fixed
receipt; it never resubmits. Uses the frozen runtime and private cutover baseline.
No application token, verification token, request payload or signed URL is saved.
"""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import time

from cloudshell_cutover import (Cutover, CutoverError, PREFIX, BUCKET, BUILD, PACKAGE_SHA,
                               INGRESS, MESSAGE, environment, canonical, load, sha, package_proof)

MESSAGE_ID = 'formal-readonly-20261102-v1'
TEXT = '请查询我今天已记录的热量，只做只读查询，不记录、不修改、不设置。'
FIELDS = ('record_id', 'user_id', 'date', 'message_id', 'food_id', 'food_name', 'meal_type',
          'grams', 'nutrition', 'status', 'snapshot', 'raw_text', 'created_at', 'feishu_record_id')
VALID = {'', 'valid', 'active', 'normal', '启用', '正常', '有效', '已记录'}
BUSINESS_KEYS = ('item:', 'day:', 'food:', 'source:', 'archive:', 'intake_action:')


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def rows_audit(rows):
    fixed = [{key: row.get(key) for key in FIELDS} for row in sorted(rows, key=lambda row: row['record_id'])]
    if len({row['record_id'] for row in fixed}) != len(fixed):
        raise CutoverError('formal readback contains duplicate intake identities')
    totals = {}
    for row in fixed:
        if str(row['status'] or '').strip().lower() in VALID:
            key = row['user_id']+':'+row['date']
            totals[key] = totals.get(key, Decimal(0))+Decimal(str(row['nutrition']['kcal']))
    return {'intake_count':len(fixed), 'rows_sha256':sha(canonical(fixed)),
            'daily_kcal':{key:str(value) for key,value in sorted(totals.items())},
            'record_ids':[row['record_id'] for row in fixed]}


def settings_audit(settings):
    return {user:{'business_sha256':sha(canonical({key:value for key,value in payload.items() if key!='feishu_chat_id'})),
                  'feishu_chat_id':payload.get('feishu_chat_id')} for user,payload in settings.items()}


def observed(rt, objects, raw_class, expected, manifest, minimum, own_user):
    reader = rt['CosStore'](objects=objects, prefix=PREFIX, readonly=True)
    reader.journal.refresh(confirm=False, minimum_revision=minimum)
    raw = raw_class(objects, PREFIX+'message/')
    revision, index = raw.snapshot(minimum=reader.journal.revision)
    reader.journal.refresh(confirm=False, minimum_revision=revision)
    raw_rows = [raw.document(index, key) for key in index if key.startswith('item:')]
    users = {row['user_id'] for row in raw_rows} | {row['user_id'] for row in expected['intake']}
    store_rows = [row for user in users for row in reader.intake_items(user, include_deleted=True)]
    wanted = rows_audit(expected['intake'])
    if rows_audit(raw_rows) != wanted or rows_audit(store_rows) != wanted:
        raise CutoverError('formal raw/fresh Store intake differs from the original private baseline')
    if wanted['daily_kcal'] != manifest['daily_kcal'] or wanted['intake_count'] != manifest['intake_count']:
        raise CutoverError('formal independent daily totals differ from the private manifest')
    if reader.journal.get('baseline') != manifest['source_sha256']:
        raise CutoverError('formal baseline watermark differs')
    settings_users = users | {own_user} | {task['user_id'] for task in expected.get('pending', []) if task['intent']=='user_settings'}
    settings = {user:reader.load_document('user_settings', user) for user in settings_users}
    original_settings = {task['user_id']:task['payload'] for task in expected.get('pending', [])
                         if task['intent']=='user_settings' and not task.get('chat_id')}
    expected_settings = settings_audit({user:original_settings.get(user, {}) for user in settings_users})
    actual_settings = settings_audit(settings)
    if any(actual_settings[user]['business_sha256'] != expected_settings[user]['business_sha256'] for user in settings_users):
        raise CutoverError('formal personal preferences/targets differ from the private baseline')
    return {'revision':revision, 'rows':raw_rows, 'audit':wanted,
            'settings_audit':actual_settings, 'own_business_settings':{key:value for key,value in settings[own_user].items() if key!='feishu_chat_id'},
            'own_settings_archive':raw.document(index, 'archive:settings:'+own_user) if 'archive:settings:'+own_user in index else None,
            'business_refs':{key:value for key,value in index.items() if key.startswith(BUSINESS_KEYS)}}


def metadata_check(report, after, user, chat):
    before_settings, now_settings = report['before_settings_audit'], after['settings_audit']
    if set(before_settings) != set(now_settings) or any(before_settings[uid]['business_sha256'] != now_settings[uid]['business_sha256'] for uid in before_settings):
        raise CutoverError('formal query changed personal preferences/targets')
    if any(before_settings[uid]['feishu_chat_id'] != now_settings[uid]['feishu_chat_id'] for uid in before_settings if uid!=user):
        raise CutoverError('formal query changed another user delivery routing')
    if now_settings[user]['feishu_chat_id'] != chat:
        raise CutoverError('formal own chat binding does not match the guarded destination')
    routing_changed = before_settings[user]['feishu_chat_id'] != chat
    before, current = report['before_business_refs'], after['business_refs']
    changed = {key for key in set(before)|set(current) if before.get(key)!=current.get(key)}
    archive_key = 'archive:settings:'+user
    if changed - {archive_key}:
        raise CutoverError('formal query changed intake/source/other business content references')
    if archive_key in changed:
        archive = after['own_settings_archive']
        previous = report.get('before_own_settings_archive') or {}
        if (not routing_changed or not archive or archive.get('kind')!='settings' or archive.get('id')!=user
                or archive.get('user_id')!=user or archive.get('value')!=after['own_business_settings']
                or not archive.get('value') or previous.get('value')==archive['value']
                or archive.get('revision')!=previous.get('revision', 0)+1):
            raise CutoverError('formal settings archive change is not the exact automatic chat-binding side effect')
    return {'own_chat_binding_changed':routing_changed, 'matching_settings_archive_changed':archive_key in changed,
            'personal_preferences_unchanged':True, 'other_user_routing_unchanged':True}


def verify_result(sample, baseline, receipt, state, expected_user, expected_chat):
    if receipt.get('source') != 'feishu' or receipt['message'].get('message_id') != MESSAGE_ID:
        raise CutoverError('formal persisted receipt is not the fixed Feishu message')
    if any(receipt['message'].get(key) != value for key,value in
           (('user_id', expected_user), ('chat_id', expected_chat), ('text', TEXT))):
        raise CutoverError('formal normalized receipt identity/text differs')
    result = state.get('result', {})
    delivery = state.get('delivery', {})
    if (state.get('status') != 'completed' or state.get('work_status') == 'needs_attention'
            or result.get('status') != 'ok' or result.get('errors') or not delivery.get('presented') or not delivery.get('sent_at')):
        raise CutoverError('formal read-only request or Feishu delivery is incomplete')
    if state.get('receipt_id') != sample['receipt_id']:
        raise CutoverError('formal receipt identity differs')
    facts = result.get('tool_results', [])
    calls = result.get('native_calls', [])
    if not facts or not calls or any(f.get('tool') != 'query_today' or f.get('status') != 'ok' for f in facts) or any(c.get('tool') != 'query_today' for c in calls):
        raise CutoverError('formal query did not use only successful query_today calls')
    day = receipt['intake_date']
    key = expected_user+':'+day
    expected_total = round(float(Decimal(baseline['daily_kcal'].get(key, '0'))), 1)
    expected_ids = {row['record_id'] for row in baseline['rows'] if row['user_id']==expected_user and row['date']==day
                    and str(row['status'] or '').strip().lower() in VALID}
    fact = facts[-1]['data']
    if (fact.get('date') != day or fact.get('total', {}).get('kcal') != expected_total or fact.get('count') != len(expected_ids)
            or {row['record_id'] for row in fact.get('items', [])} != expected_ids):
        raise CutoverError('formal query facts differ from the original daily intake')
    values = re.findall(r'(\d+(?:\.\d+)?)\s*(?:千卡|大卡|kcal)', result.get('reply', ''), re.I)
    if not values or any(float(value) != expected_total for value in values):
        raise CutoverError('formal natural reply does not report the independently expected heat total')
    measured = round((delivery['sent_at']-receipt['received_at'])*1000, 3)
    if measured != delivery.get('t_service_ms'):
        raise CutoverError('formal service timing does not retain original received_at')
    return {'expected_date':day, 'expected_total_kcal':expected_total, 'expected_record_ids':sorted(expected_ids),
            'query_fact':fact, 'reply':result['reply'], 'automated_fact_checks_passed':True,
            'independent_semantic_review':'pending operator review of the saved natural reply',
            't_service_ms':measured, 'synthetic_client_to_send_ms':delivery.get('t_user_ms'), 't_user_ms':None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('send', 'resume'))
    parser.add_argument('--candidate-report', required=True, type=Path)
    args = parser.parse_args(argv)
    cutover = Cutover(args.candidate_report)
    path = cutover.root/'cloud-evidence/public/production-formal-readonly.json'
    report = load(path) if path.exists() else None
    try:
        activated = cutover.require('activate')
        cutover.gate()
        rt = cutover.runtime()
        # Reuse only independent raw decoding and the HTTP transport, without
        # invoking the candidate Driver or its production-forbidding plan check.
        from harness.eval.production_live import RawCommits, _post_webhook
        from agent.storage.execution_pipeline import receipt_id
        private = cutover.private()
        guard = private.get('GUARD_MESSAGE') or {}
        sender = private.get('GUARD_USER_ID') or guard.get('user_id')
        chat = private.get('GUARD_CHAT_ID') or guard.get('chat_id')
        if not sender or not chat:
            raise CutoverError('provide the own GUARD identity privately before the formal check')
        original_env = environment(cutover.original(INGRESS))
        token = original_env.get('FEISHU_VERIFICATION_TOKEN')
        if not token:
            raise CutoverError('the original Feishu verification token is missing')
        frozen_body = (cutover.root/'calorie-agent-v4.zip').read_bytes()
        if sha(frozen_body) != PACKAGE_SHA:
            raise CutoverError('the local frozen package changed')
        code_readback = {}
        for name in (INGRESS, MESSAGE):
            current = cutover.get_function(name)
            actual_env = environment(current)
            if current.get('Status') != 'Active' or actual_env.get('COS_PREFIX') != PREFIX or actual_env.get('COS_BUCKET') != BUCKET:
                raise CutoverError('formal function is not active on the approved production prefix')
            if name == INGRESS and (actual_env.get('INGRESS_MAINTENANCE') != '0' or actual_env.get('FEISHU_VERIFICATION_TOKEN') != token):
                raise CutoverError('formal ingress is not active with the original verification token')
            actual_body, _, request_id = cutover.code(name)
            proof = package_proof(name, actual_body, frozen_body)
            if not proof:
                raise CutoverError('formal actual code differs from the frozen build')
            code_readback[name] = {**proof, 'code_request_id':request_id, 'config_request_id':current.get('RequestId')}
        data = load(cutover.private_dir/'production-baseline.json')
        manifest = load(cutover.private_dir/'production-baseline-manifest.json')
        if rt['manifest'](data) != manifest or cutover.require('migrate')['baseline_source_sha256'] != manifest['source_sha256']:
            raise CutoverError('the private baseline or migration manifest changed')
        user = data.get('aliases', {}).get(sender, sender)
        rid = receipt_id('feishu', MESSAGE_ID)
        objects = rt['CosObjects'](bucket=BUCKET, region='ap-shanghai')
        minimum = cutover.require('migrate')['cos_revision']
        if args.operation == 'send':
            if report is not None:
                raise CutoverError('a formal attempt is already saved; use resume, never repost')
            before = observed(rt, objects, RawCommits, data, manifest, minimum, user)
            inbox = RawCommits(objects, PREFIX+'ingress/')
            _, index = inbox.snapshot()
            if 'receipt:'+rid in index:
                raise CutoverError('the fixed formal receipt already exists; do not submit it again')
            report = {'schema':'production-formal-readonly.v1', 'message_id':MESSAGE_ID, 'receipt_id':rid,
                'build_id':BUILD, 'package_sha256':PACKAGE_SHA, 'prefix':PREFIX, 'bucket':BUCKET,
                'user_id':user, 'sender_open_id':sender, 'chat_id':chat, 'text':TEXT,
                'webhook_url':private['CALORIE_SERVICE_URL'].rstrip('/')+'/webhook/feishu/events',
                'baseline_source_sha256':manifest['source_sha256'], 'baseline_audit':before['audit'],
                'before_business_refs':before['business_refs'], 'before_revision':before['revision'],
                'before_settings_audit':before['settings_audit'], 'before_own_settings_archive':before['own_settings_archive'],
                'activate_completed_at':activated['completed_at'], 'code_readback':code_readback,
                'post_status':'pending', 'post_attempted_at':time.time(), 'status':'awaiting_receipt'}
            save(path, report)  # Durable attempt precedes the only HTTP POST.
            payload = {'schema':'2.0', 'header':{'event_id':MESSAGE_ID, 'event_type':'im.message.receive_v1', 'token':token},
                'event':{'sender':{'sender_id':{'open_id':sender}}, 'message':{'message_id':MESSAGE_ID, 'chat_id':chat,
                    'message_type':'text', 'create_time':str(int(report['post_attempted_at']*1000)),
                    'content':json.dumps({'text':TEXT}, ensure_ascii=False)}}}
            started = time.perf_counter()
            try:
                response = _post_webhook({'webhook_auth':'feishu_token', 'webhook_url':report['webhook_url']}, payload)
                ack = response.json()
                report.update(post_status='responded', ack_http_status=response.status_code,
                    ack_elapsed_ms=round((time.perf_counter()-started)*1000, 3), caller_ack_at=time.time(),
                    ack={key:ack.get(key) for key in ('status','reason','receipt_id','sequence','message_id','chat_id') if key in ack})
            except Exception:
                report.update(post_status='unknown', post_stopped_at=time.time())
                save(path, report)
                raise
            save(path, report)
            if response.status_code != 200 or ack.get('status') != 'accepted' or ack.get('receipt_id') != rid:
                raise CutoverError('formal webhook ACK did not confirm the fixed receipt')
        elif report is None:
            raise CutoverError('resume requires the saved original formal attempt')
        if any(report.get(key) != value for key,value in (('message_id', MESSAGE_ID), ('receipt_id',rid), ('build_id',BUILD),
                ('prefix',PREFIX), ('baseline_source_sha256',manifest['source_sha256']), ('user_id',user), ('chat_id',chat))):
            raise CutoverError('saved formal attempt identity/build/baseline differs')
        deadline = time.monotonic()+180
        state = None
        while time.monotonic() < deadline:
            reader = rt['CosStore'](objects=objects, prefix=PREFIX, readonly=True)
            state = reader.load_document('receipt_state', 'system', rid)
            if state.get('status') == 'completed': break
            if state.get('status') == 'needs_attention':
                raise CutoverError('formal receipt requires operator recovery')
            time.sleep(1)
        if not state or state.get('status') != 'completed':
            raise CutoverError('formal receipt is not confirmed; resume may only read this original key')
        inbox = RawCommits(objects, PREFIX+'ingress/')
        _, index = inbox.snapshot()
        receipt = inbox.document(index, 'receipt:'+rid)
        report.update(raw_receipt=receipt, receipt_state=state, original_received_at=receipt['received_at'], observed_at=time.time())
        save(path, report)
        after = observed(rt, objects, RawCommits, data, manifest, state['delivery']['cos_revision'], user)
        metadata = metadata_check(report, after, user, chat)
        semantic = verify_result(report, {'rows':data['intake'], **manifest}, receipt, state, user, chat)
        report.update(status='automated_checks_passed', raw_receipt=receipt, receipt_state=state,
            actual_post_count=1, original_received_at=receipt['received_at'], after_revision=after['revision'],
            worker_attempts=state.get('attempts'),
            fresh_readonly_store_verified=True, raw_commit_verified=True, after_audit=after['audit'],
            business_refs_unchanged=after['business_refs']==report['before_business_refs'], permitted_metadata=metadata,
            semantic=semantic, completed_at=time.time())
        save(path, report)
        print(json.dumps({'report':str(path), 'status':report['status'], 'message_id':MESSAGE_ID,
                          'build_id':BUILD, 't_service_ms':semantic['t_service_ms'], 'reply':semantic['reply']}, ensure_ascii=False))
        return 0
    except Exception as exc:
        error = str(exc) if isinstance(exc, CutoverError) else type(exc).__name__
        if report is not None:
            report.update(status='stopped', error=error, stopped_at=time.time())
            save(path, report)
        print(json.dumps({'status':'stopped', 'error':error, 'message_id':MESSAGE_ID}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
