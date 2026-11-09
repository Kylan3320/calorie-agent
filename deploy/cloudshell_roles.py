"""Inspect/apply only the three approved production CAM roles (Python 3.6).

No function binding, policy replacement, deletion or write retry is performed.
Run again after an uncertain response: every apply begins by reading resources.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import unquote

PLAN = Path(__file__).resolve().with_name('cos-production-role-policies.json')
PREFIX = 'calorie-agent/production/20261102/'
APPROVED_SHA256 = '138d35f3d9fa975af0acaeeda799a605d5f813aaed5c7ae51887088c19df8db9'
NAMES = {kind: ('calorie-agent-cos-'+kind+'-role', 'calorie-agent-cos-'+kind+'-policy')
         for kind in ('ingress', 'message', 'archive')}
READS = {'GetRole', 'ListPolicies', 'GetPolicy', 'ListAttachedRolePolicies'}
WRITES = {'CreateRole', 'CreatePolicy', 'AttachRolePolicy'}


class RoleSetupError(RuntimeError):
    pass


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return sorted((canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True, separators=(',', ':')))
    return value


def document(value):
    if isinstance(value, dict):
        return value
    # Raw JSON may itself contain meaningful %2F strings in cos:prefix. Decode
    # exactly once only when the outer response is URL-encoded rather than JSON.
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = json.loads(unquote(value))
    if not isinstance(parsed, dict):
        raise RoleSetupError('CAM policy document is not a JSON object')
    return parsed


def approved_plan():
    plan = json.loads(PLAN.read_text(encoding='utf-8'))
    fixed = {'prefix': plan['prefix'], 'roles': plan['roles']}
    digest = hashlib.sha256(json.dumps(canonical(fixed), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    if not plan.get('approved_by_user') or plan['prefix'] != PREFIX or set(plan['roles']) != set(NAMES) or digest != APPROVED_SHA256:
        raise RoleSetupError('approved role plan changed; independent review is required')
    for kind, names in NAMES.items():
        spec = plan['roles'][kind]
        if (spec['role_name'], spec['policy_name']) != names:
            raise RoleSetupError('unapproved role or policy name')
    return plan


class SdkCAM:
    def __init__(self):
        from tencentcloud.cam.v20190116 import cam_client, models
        from tencentcloud.common.credential import Credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        profile = ClientProfile()
        profile.httpProfile.reqTimeout = 20
        # Current SDK defaults to NoopRetryer. Never opt into write retries.
        profile.retryer = None
        credential = Credential(os.environ['TENCENTCLOUD_SECRET_ID'], os.environ['TENCENTCLOUD_SECRET_KEY'],
                                os.environ['TENCENTCLOUD_SECURITY_TOKEN'])
        self.client = cam_client.CamClient(credential, '', profile)
        self.models = models

    def call(self, action, parameters):
        request = getattr(self.models, action+'Request')()
        request.from_json_string(json.dumps(parameters, ensure_ascii=False))
        return json.loads(getattr(self.client, action)(request).to_json_string())


class RoleManager:
    def __init__(self, cam, emit=None):
        self.cam, self.emit = cam, emit
        self.audit = []
        self.plan = approved_plan()

    def call(self, action, **parameters):
        if action not in READS | WRITES:
            raise RoleSetupError('unapproved CAM operation')
        event = {'operation': action, 'target': {key: value for key, value in parameters.items()
            if key in {'RoleName', 'PolicyName', 'AttachRoleName', 'PolicyId', 'Keyword', 'Page'}}}
        try:
            response = self.cam.call(action, parameters)
        except Exception as exc:
            event.update(outcome='failed_or_unknown', error_code=exc.get_code() if hasattr(exc, 'get_code') else type(exc).__name__,
                         request_id=exc.get_request_id() if hasattr(exc, 'get_request_id') else None)
            self.record(event)
            raise
        event.update(outcome='ok', request_id=response.get('RequestId'))
        role_id = response.get('RoleId') or (response.get('RoleInfo') or {}).get('RoleId')
        policy_id = response.get('PolicyId') or parameters.get('PolicyId')
        if role_id:
            event['role_id'] = role_id
        if policy_id:
            event['policy_id'] = policy_id
        self.record(event)
        return response

    def record(self, event):
        self.audit.append(event)
        if self.emit:
            self.emit({'event': 'cam_operation', **event})

    def pages(self, action, **parameters):
        rows = []
        for page in range(1, 201):
            result = self.call(action, Page=page, Rp=100, **parameters)
            batch = result['List']
            rows.extend(batch)
            if len(rows) >= result['TotalNum']:
                return rows
            if not batch:
                break
        raise RoleSetupError('CAM pagination did not return the declared complete list')

    def read(self, kind):
        spec = self.plan['roles'][kind]
        try:
            role = self.call('GetRole', RoleName=spec['role_name'])['RoleInfo']
        except Exception as exc:
            # Official GetRole documents exactly this absence code. Authorization,
            # network and unknown errors are never converted into permission to create.
            if not hasattr(exc, 'get_code') or exc.get_code() != 'InvalidParameter.RoleNotExist':
                raise
            role = None
        else:
            if not isinstance(role, dict) or not role.get('RoleId'):
                raise RoleSetupError('GetRole returned no confirmed role identity')
        if role:
            if role['RoleName'] != spec['role_name'] or canonical(document(role['PolicyDocument'])) != canonical(spec['trust_policy']):
                raise RoleSetupError('existing role trust differs: '+spec['role_name'])
        matches = [row for row in self.pages('ListPolicies', Scope='Local', Keyword=spec['policy_name'])
                   if row['PolicyName'] == spec['policy_name']]
        if len(matches) > 1:
            raise RoleSetupError('custom policy name is not unique: '+spec['policy_name'])
        policy = None
        if matches:
            policy = self.call('GetPolicy', PolicyId=matches[0]['PolicyId'])
            if policy['PolicyName'] != spec['policy_name'] or policy['Type'] != 1 or canonical(document(policy['PolicyDocument'])) != canonical(spec['policy']):
                raise RoleSetupError('existing custom policy differs: '+spec['policy_name'])
            policy['PolicyId'] = matches[0]['PolicyId']
        attached = self.pages('ListAttachedRolePolicies', RoleName=spec['role_name']) if role else []
        if attached and (len(attached) != 1 or not policy or attached[0]['PolicyName'] != spec['policy_name'] or attached[0]['PolicyId'] != policy['PolicyId']):
            raise RoleSetupError('unexpected policy attachment; no changes made to it: '+spec['role_name'])
        return {'role': role, 'policy': policy, 'attached': attached}

    def execute(self, operation):
        if operation not in {'inspect', 'apply'}:
            raise RoleSetupError('expected inspect or apply')
        # Read all three first, so a preexisting mismatch causes no partial apply.
        before = {kind: self.read(kind) for kind in NAMES}
        if operation == 'apply':
            for kind in NAMES:
                spec, state = self.plan['roles'][kind], before[kind]
                if not state['role']:
                    created = self.call('CreateRole', RoleName=spec['role_name'],
                        PolicyDocument=json.dumps(spec['trust_policy'], ensure_ascii=False), ConsoleLogin=0)
                    state = self.read(kind)
                    if not state['role'] or state['role']['RoleId'] != created['RoleId']:
                        raise RoleSetupError('created role could not be confirmed by its original name')
                if not state['policy']:
                    created = self.call('CreatePolicy', PolicyName=spec['policy_name'],
                        PolicyDocument=json.dumps(spec['policy'], ensure_ascii=False))
                    state = self.read(kind)
                    if not state['policy'] or state['policy']['PolicyId'] != created['PolicyId']:
                        raise RoleSetupError('created policy could not be confirmed by its original name')
                if not state['attached']:
                    self.call('AttachRolePolicy', AttachRoleName=spec['role_name'], PolicyId=state['policy']['PolicyId'])
        final = {kind: self.read(kind) for kind in NAMES}
        readback = {}
        for kind, state in final.items():
            if operation == 'apply' and (not state['role'] or not state['policy'] or len(state['attached']) != 1):
                raise RoleSetupError('apply readback incomplete: '+kind)
            readback[kind] = {'role_name': NAMES[kind][0], 'policy_name': NAMES[kind][1],
                'role_id': state['role']['RoleId'] if state['role'] else None,
                'policy_id': state['policy']['PolicyId'] if state['policy'] else None,
                'trust': document(state['role']['PolicyDocument']) if state['role'] else None,
                'policy': document(state['policy']['PolicyDocument']) if state['policy'] else None,
                'attached': [{'policy_id': row['PolicyId'], 'policy_name': row['PolicyName']} for row in state['attached']]}
        return {'operation': operation, 'prefix': PREFIX, 'approved_plan_sha256': APPROVED_SHA256,
                'readback': readback, 'audit': self.audit}


def main(argv=None, cam=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['inspect', 'apply'])
    args = parser.parse_args(argv)
    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    try:
        manager = RoleManager(cam or SdkCAM(), emit=emit)
        emit(manager.execute(args.operation))
    except Exception as exc:
        emit({'event': 'cam_roles_stopped', 'error_type': type(exc).__name__,
              'error_code': exc.get_code() if hasattr(exc, 'get_code') else None,
              'reason': str(exc) if isinstance(exc, RoleSetupError) else 'operation failed; rerun inspect before apply; no write was retried'})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
