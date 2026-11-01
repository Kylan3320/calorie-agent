"""Offline-only checks for the pinned CloudShell CAM release helper."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from urllib.parse import quote
from unittest.mock import patch

from deploy.cloudshell_roles import RoleManager, RoleSetupError, NAMES, WRITES, approved_plan, document


class CAMError(Exception):
    def __init__(self, code): self.code = code
    def get_code(self): return self.code


class FakeCAM:
    def __init__(self):
        self.roles, self.policies, self.attached = {}, {}, {}
        self.calls = []
        self.lose = None
        self.read_error = None
        self.encoded = False

    def call(self, action, params):
        self.calls.append((action, deepcopy(params)))
        response = {'RequestId':'request-'+str(len(self.calls))}
        if action == 'GetRole':
            if self.read_error: raise CAMError(self.read_error)
            if params['RoleName'] not in self.roles:
                raise CAMError('InvalidParameter.RoleNotExist')
            response['RoleInfo'] = deepcopy(self.roles[params['RoleName']])
        elif action == 'ListPolicies':
            assert params['Scope']=='Local' and params['Page']==1 and params['Rp']==100
            rows = [{'PolicyName': name, 'PolicyId': value['PolicyId'], 'Type':1} for name,value in self.policies.items() if params['Keyword'] in name]
            response.update(List=rows, TotalNum=len(rows))
        elif action == 'GetPolicy':
            policy = next(value for value in self.policies.values() if value['PolicyId']==params['PolicyId'])
            response.update(deepcopy(policy))
            if self.encoded:
                response['PolicyDocument'] = quote(response['PolicyDocument'], safe='')
        elif action == 'ListAttachedRolePolicies':
            assert params['Page']==1 and params['Rp']==100 and set(params)=={'RoleName','Page','Rp'}
            rows = deepcopy(self.attached.get(params['RoleName'], []))
            response.update(List=rows, TotalNum=len(rows))
        elif action == 'CreateRole':
            assert params['ConsoleLogin']==0 and params['RoleName'] not in self.roles
            response['RoleId'] = 'role-'+str(len(self.roles)+1)
            self.roles[params['RoleName']] = {**deepcopy(params), 'RoleId':response['RoleId']}
        elif action == 'CreatePolicy':
            assert params['PolicyName'] not in self.policies
            response['PolicyId'] = len(self.policies)+1
            self.policies[params['PolicyName']] = {**deepcopy(params), 'PolicyId':response['PolicyId'], 'Type':1}
        elif action == 'AttachRolePolicy':
            assert set(params)=={'AttachRoleName','PolicyId'}
            name = next(name for name,value in self.policies.items() if value['PolicyId']==params['PolicyId'])
            self.attached[params['AttachRoleName']] = [{'PolicyName':name, 'PolicyId':params['PolicyId']}]
        else:
            raise AssertionError('unexpected CAM operation: '+action)
        if self.lose == action:
            self.lose = None
            raise OSError('response lost after committed CAM operation')
        return response


def role_release_checks():
    """限定3角色的幂等、原始/编码文档、差异拒绝与响应丢失后只读找回。"""
    fake = FakeCAM()
    inspected = RoleManager(fake).execute('inspect')
    assert not any(action in WRITES for action,_ in fake.calls)
    assert all(value['role_id'] is None for value in inspected['readback'].values())
    first = RoleManager(fake).execute('apply')
    assert len(fake.roles)==len(fake.policies)==3
    writes = [action for action,_ in fake.calls if action in WRITES]
    assert writes.count('CreateRole')==writes.count('CreatePolicy')==writes.count('AttachRolePolicy')==3
    fake.calls.clear()
    fake.encoded = True
    second = RoleManager(fake).execute('apply')
    assert not any(action in WRITES for action,_ in fake.calls)
    assert first['readback']==second['readback']
    prefix = approved_plan()['roles']['ingress']['policy']['statement'][1]['condition']['string_equal']['cos:prefix'][0]
    assert '%2F' in prefix and document(json.dumps({'prefix':prefix}))['prefix']==prefix
    assert document(quote(json.dumps({'prefix':prefix}), safe=''))['prefix']==prefix
    assert any(row.get('role_id') for row in first['audit']) and any(row.get('policy_id') for row in first['audit'])

    for mismatch in ('trust', 'policy', 'attachment'):
        altered = deepcopy(fake)
        altered.calls.clear()
        if mismatch == 'trust':
            altered.roles[NAMES['ingress'][0]]['PolicyDocument'] = json.dumps({'version':'2.0','statement':[]})
        elif mismatch == 'policy':
            policy = altered.policies[NAMES['ingress'][1]]
            value = json.loads(policy['PolicyDocument'])
            value['statement'][-1]['action'] = ['name/scf:Invoke*']
            policy['PolicyDocument'] = json.dumps(value)
        else:
            altered.attached[NAMES['ingress'][0]].append({'PolicyName':'unexpected-extra','PolicyId':999})
        try: RoleManager(altered).execute('apply')
        except RoleSetupError: pass
        else: raise AssertionError('existing resource difference was accepted')
        assert not any(action in WRITES for action,_ in altered.calls)

    for lost in ('CreateRole', 'CreatePolicy', 'AttachRolePolicy'):
        interrupted = FakeCAM()
        interrupted.lose = lost
        try: RoleManager(interrupted).execute('apply')
        except OSError: pass
        else: raise AssertionError('unknown write response was not propagated')
        assert sum(action==lost for action,_ in interrupted.calls)==1
        RoleManager(interrupted).execute('apply')
        assert len(interrupted.roles)==len(interrupted.policies)==3
        assert sum(action==lost for action,_ in interrupted.calls)==3
    unavailable = FakeCAM()
    unavailable.read_error = 'AuthFailure.SignatureExpire'
    try: RoleManager(unavailable).execute('apply')
    except CAMError: pass
    else: raise AssertionError('unknown read error was treated as absence')
    assert not any(action in WRITES for action,_ in unavailable.calls)
    changed_plan = approved_plan()
    changed_plan['roles']['ingress']['policy']['statement'][-1]['action'] = ['name/scf:Invoke*']
    with tempfile.TemporaryDirectory(prefix='calorie-role-plan-') as directory:
        altered = Path(directory)/'roles.json'
        altered.write_text(json.dumps(changed_plan), encoding='utf-8')
        blocked = FakeCAM()
        with patch('deploy.cloudshell_roles.PLAN', altered):
            try: RoleManager(blocked).execute('apply')
            except RoleSetupError: pass
            else: raise AssertionError('unreviewed additional grant was accepted')
        assert not blocked.calls


CHECKS = {'CR01':role_release_checks}
