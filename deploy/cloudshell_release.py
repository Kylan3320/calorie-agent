"""Bounded CloudShell release operations; production packages never import this.

The operator runs one explicit stage at a time. Candidate writes are pinned to
one prefix and three existing functions. Production cutover is a separate gate.
Compatible with CloudShell's management Python 3.6.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
CANDIDATES = {'ingress':'calorie-cos-ingress-probe','message':'calorie-cos-msg-probe','sync':'calorie-cos-sync-probe'}
HANDLERS = {'ingress':'app.ingress_handler','message':'app.message_handler','sync':'app.maintenance_handler'}
PREFIX='cos-probe/production-candidate/20261102/recheck-v2/'
BUCKET='calorie-cos-probe-20261101-1440448581'
REGION='ap-shanghai'
EVIDENCE=ROOT/'cloud-evidence'


def api(action, payload):
    sid=os.environ.get('TENCENTCLOUD_SECRET_ID') or os.environ['TENCENTCLOUD_SECRETID']
    secret=os.environ.get('TENCENTCLOUD_SECRET_KEY') or os.environ['TENCENTCLOUD_SECRETKEY']
    token=os.environ.get('TENCENTCLOUD_SECURITY_TOKEN') or os.environ.get('TENCENTCLOUD_TOKEN') or os.environ.get('TENCENTCLOUD_SESSION_TOKEN') or os.environ.get('TENCENTCLOUD_SESSIONTOKEN')
    body=json.dumps(payload,separators=(',',':'),ensure_ascii=False).encode()
    stamp=int(time.time());day=datetime.fromtimestamp(stamp,timezone.utc).strftime('%Y-%m-%d')
    canonical='POST\n/\n\ncontent-type:application/json; charset=utf-8\nhost:scf.tencentcloudapi.com\nx-tc-action:'+action.lower()+'\n\ncontent-type;host;x-tc-action\n'+hashlib.sha256(body).hexdigest()
    scope=day+'/scf/tc3_request'
    to_sign='TC3-HMAC-SHA256\n'+str(stamp)+'\n'+scope+'\n'+hashlib.sha256(canonical.encode()).hexdigest()
    def sign(key,value):return hmac.new(key,value.encode(),hashlib.sha256).digest()
    key=sign(sign(sign(('TC3'+secret).encode(),day),'scf'),'tc3_request')
    sig=hmac.new(key,to_sign.encode(),hashlib.sha256).hexdigest()
    headers={'Content-Type':'application/json; charset=utf-8','Host':'scf.tencentcloudapi.com','X-TC-Action':action,'X-TC-Version':'2018-04-16','X-TC-Region':REGION,'X-TC-Timestamp':str(stamp),'Authorization':'TC3-HMAC-SHA256 Credential='+sid+'/'+scope+', SignedHeaders=content-type;host;x-tc-action, Signature='+sig}
    if token:headers['X-TC-Token']=token
    request=urllib.request.Request('https://scf.tencentcloudapi.com/',data=body,headers=headers)
    with urllib.request.urlopen(request,timeout=90) as response: result=json.load(response)['Response']
    if 'Error' in result:raise RuntimeError(action+':'+result['Error'].get('Code','unknown')+':'+result['Error'].get('Message','')[:180])
    return result


def identity(name):return {'FunctionName':name,'Namespace':'default'}


def record(name,data,private=False):
    folder=EVIDENCE/('private' if private else 'public');folder.mkdir(parents=True,exist_ok=True)
    path=folder/(name+'.json')
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    if private:os.chmod(str(path),0o600)


def wait_ready(name):
    until=time.time()+90
    while time.time()<until:
        result=api('GetFunction',identity(name))
        if result.get('Status')=='Active':return result
        if result.get('Status') in ('CreateFailed','UpdateFailed'):raise RuntimeError(name+':'+result['Status'])
        time.sleep(1)
    raise TimeoutError(name+' not active')


def inventory():
    rows=[]
    for name in list(CANDIDATES.values())+['calorie-agent-api-v3-test','calorie-agent-reminder']:
        config=api('GetFunction',identity(name));record(name+'-before',config,True)
        triggers=api('ListTriggers',identity(name));record(name+'-triggers-before',triggers)
        reserved=api('GetReservedConcurrencyConfig',identity(name))
        rows.append({'function':name,'status':config.get('Status'),'handler':config.get('Handler'),'runtime':config.get('Runtime'),'memory':config.get('MemorySize'),'role':config.get('Role'),'layers':config.get('Layers'),'env_keys':[v['Key'] for v in config.get('Environment',{}).get('Variables',[])],'triggers':triggers.get('Triggers',[]),'reserved':reserved})
    record('inventory',rows);print(json.dumps(rows,ensure_ascii=False))


def candidate_deploy():
    package=ROOT/'calorie-agent-v4.zip'
    manifest=json.loads((ROOT/'frozen-build.json').read_text())
    assert hashlib.sha256(package.read_bytes()).hexdigest()==manifest['sha256']
    secrets=json.loads((ROOT/'candidate-private.json').read_text())
    selected=json.loads((ROOT/'candidate-layer.json').read_text())
    actual_layer=wait_layer(selected)
    assert 'Python3.10' in actual_layer['CompatibleRuntimes']
    layers=[{'LayerName':'calorie-rag','LayerVersion':selected['LayerVersion']}]
    log_config=api('GetFunction',identity('calorie-agent-api-v3-test'))
    assert log_config['ClsLogsetId'] and log_config['ClsTopicId']
    zipped=base64.b64encode(package.read_bytes()).decode()
    for kind,name in CANDIDATES.items():
        previous=api('GetFunction',identity(name));record(name+'-before-deploy',previous,True)
        triggers=api('ListTriggers',identity(name)).get('Triggers',[])
        for trigger in triggers:
            if trigger['Type']=='cos' and trigger.get('Enable')==1:
                api('UpdateTriggerStatus',dict(identity(name),TriggerName=trigger['TriggerName'],Type='cos',Enable='CLOSE',Qualifier=trigger.get('Qualifier','$LATEST'),TriggerDesc=trigger['TriggerDesc']))
        code=api('UpdateFunctionCode',dict(identity(name),Handler=HANDLERS[kind],ZipFile=zipped))
        wait_ready(name)
        common={'PRESET':'live','COS_BUCKET':BUCKET,'COS_PREFIX':PREFIX,'COS_REGION':REGION,
                'CALORIE_MESSAGE_FUNCTION':CANDIDATES['message'],'TRACE_ENABLED':'true'}
        if kind in ('ingress','message'):
            common['CALORIE_SERVICE_TOKEN']=secrets['CALORIE_SERVICE_TOKEN']
        if kind=='ingress':common['FEISHU_VERIFICATION_TOKEN']=secrets['FEISHU_VERIFICATION_TOKEN']
        if kind=='message':
            for key in ('DEEPSEEK_API_KEY','FEISHU_APP_ID','FEISHU_APP_SECRET','SILICONFLOW_API_KEY'):
                common[key]=secrets[key]
            common.update(KNOWLEDGE_RETRIEVER='lightrag',RAG_INDEX_DIR='/opt/rag_index',TIKTOKEN_CACHE_DIR='/opt/tiktoken_cache',
                          CALORIE_SERVICE_AUTH='cam',CALORIE_CLOUD_UIN='100049554055',CALORIE_SERVICE_URL=secrets['CALORIE_SERVICE_URL'])
        if kind=='sync':
            for key in ('FEISHU_APP_ID','FEISHU_APP_SECRET','BITABLE_APP_TOKEN'):common[key]=secrets[key]
            common['BITABLE_INTAKE_TABLE_ID']='tblZ2uvw8XFOUNjm'
            # Unconfigured food/settings archives fail closed rather than touching production tables.
        env={v['Key']:v['Value'] for v in previous.get('Environment',{}).get('Variables',[])}
        for key in list(env):
            if key.startswith('BITABLE_') or key.startswith('COS_PROBE_') or key.startswith('CALORIE_'):
                env.pop(key)
        env.update(common)
        params=dict(identity(name),Timeout=120,MemorySize=512,Role='calorie-cos-probe-role',
                    Environment={'Variables':[{'Key':key,'Value':value} for key,value in sorted(env.items())]},
                    ClsLogsetId=log_config['ClsLogsetId'],ClsTopicId=log_config['ClsTopicId'],IgnoreSysLog=False)
        if kind=='message':params['Layers']=layers
        config=api('UpdateFunctionConfiguration',params);wait_ready(name)
        api('PutReservedConcurrencyConfig',dict(identity(name),ReservedConcurrencyMem=512))
        actual=api('GetFunction',identity(name))
        assert actual['Handler']==HANDLERS[kind] and actual['MemorySize']==512 and actual['Timeout']==120
        observed={v['Key']:v['Value'] for v in actual.get('Environment',{}).get('Variables',[])}
        assert all(observed.get(k)==v for k,v in common.items())
        public={'function':name,'handler':actual['Handler'],'build_id':manifest['build_id'],'code_request_id':code['RequestId'],'config_request_id':config['RequestId'],'env_keys':sorted(observed),'layers':actual.get('Layers'),'reserved':api('GetReservedConcurrencyConfig',identity(name)),'cos_triggers_disabled':[t['TriggerName'] for t in triggers if t['Type']=='cos']}
        record(name+'-deployed',public);print(json.dumps(public,ensure_ascii=False),flush=True)


def seed():
    from qcloud_cos import CosConfig,CosS3Client
    from qcloud_cos.cos_exception import CosServiceError
    token=os.environ.get('TENCENTCLOUD_SECURITY_TOKEN') or os.environ.get('TENCENTCLOUD_TOKEN') or os.environ.get('TENCENTCLOUD_SESSION_TOKEN')
    client=CosS3Client(CosConfig(Region=REGION,SecretId=os.environ['TENCENTCLOUD_SECRET_ID'],SecretKey=os.environ['TENCENTCLOUD_SECRET_KEY'],Token=token,Timeout=8),retry=0)
    assert client.get_bucket_versioning(Bucket=BUCKET).get('Status') not in ('Enabled','Suspended')
    archive=zipfile.ZipFile(str(ROOT/'seed-objects.zip'))
    names=sorted(archive.namelist(),key=lambda n:('/commits/' in n,n))
    out=[]
    for key in names:
        if key.endswith('/'):continue
        assert key.startswith(PREFIX+'message/') and ('/documents/' in key or '/commits/' in key)
        body=archive.read(key)
        try:client.put_object(Bucket=BUCKET,Key=key,Body=body,Metadata={'x-cos-forbid-overwrite':'true'},ContentType='application/json',EnableMD5=True)
        except CosServiceError as exc:
            if exc.get_status_code()!=409:raise
        response=client.get_object(Bucket=BUCKET,Key=key)
        stream=response['Body'].get_raw_stream()
        try:actual=stream.read()
        finally:stream.close()
        assert actual==body,'candidate seed identity mismatch'
        out.append({'key':key,'sha256':hashlib.sha256(body).hexdigest()})
    record('seed-readback',{'bucket':BUCKET,'prefix':PREFIX,'objects':out})
    print(json.dumps({'seed_objects_verified':len(out),'prefix':PREFIX}),flush=True)


def wait_layer(selected):
    until=time.time()+120
    while time.time()<until:
        actual=api('GetLayerVersion',{'LayerName':'calorie-rag','LayerVersion':selected['LayerVersion']})
        if actual.get('Status')=='Active':
            location=actual.get('Location','')
            if not location.startswith('https://'):raise RuntimeError('active layer HTTPS location unavailable')
            with urllib.request.urlopen(location,timeout=90) as response:body=response.read()
            digest=hashlib.sha256(body).hexdigest()
            assert digest==selected['CodeSha256'],'actual layer content differs from frozen artifact'
            import io
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                index=archive.read('rag_index/INDEX_VERSION').decode().strip()
            assert index==selected['index_version']
            record('candidate-layer-content-readback',{'LayerName':'calorie-rag','LayerVersion':selected['LayerVersion'],
                'status':actual['Status'],'download_sha256':digest,'bytes':len(body),'index_version':index,
                'actual_api_fields':sorted(k for k in actual if k!='Location'),'get_request_id':actual['RequestId']})
            return actual
        if actual.get('Status') in ('PublishFailed','Failed'):
            raise RuntimeError('candidate layer publication failed')
        time.sleep(2)
    raise TimeoutError('candidate layer not active')


def publish_candidate_layer():
    from qcloud_cos import CosConfig,CosS3Client
    from qcloud_cos.cos_exception import CosServiceError
    manifest=json.loads((ROOT/'layer-artifact.json').read_text())
    body=(ROOT/'rag-layer-sourcefix.zip').read_bytes()
    assert hashlib.sha256(body).hexdigest()==manifest['sha256']
    selected=ROOT/'candidate-layer.json'
    if selected.exists():
        saved=json.loads(selected.read_text())
        assert saved['CodeSha256']==manifest['sha256']
        wait_layer(saved)
        print(json.dumps(saved));return
    token=os.environ.get('TENCENTCLOUD_SECURITY_TOKEN') or os.environ.get('TENCENTCLOUD_SESSION_TOKEN')
    client=CosS3Client(CosConfig(Region=REGION,SecretId=os.environ['TENCENTCLOUD_SECRET_ID'],SecretKey=os.environ['TENCENTCLOUD_SECRET_KEY'],Token=token))
    key=PREFIX+'deploy/rag-layer-sourcefix.zip'
    try:client.put_object(Bucket=BUCKET,Key=key,Body=body,Metadata={'x-cos-forbid-overwrite':'true'},ContentType='application/zip',EnableMD5=True)
    except CosServiceError as exc:
        if exc.get_status_code()!=409:raise
        stream=client.get_object(Bucket=BUCKET,Key=key)['Body'].get_raw_stream()
        try:assert hashlib.sha256(stream.read()).hexdigest()==manifest['sha256']
        finally:stream.close()
    result=api('PublishLayerVersion',{'LayerName':'calorie-rag','CompatibleRuntimes':['Python3.10'],
        'Description':'Calorie COS sourcefix '+manifest['index_version'],
        'Content':{'CosBucketName':BUCKET.rsplit('-',1)[0],'CosObjectName':'/'+key,'CosBucketRegion':REGION}})
    saved={'LayerName':'calorie-rag','LayerVersion':result['LayerVersion'],'CodeSha256':manifest['sha256'],
           'index_version':manifest['index_version'],'publish_request_id':result['RequestId']}
    selected.write_text(json.dumps(saved,indent=2));record('candidate-layer-published',saved)
    wait_layer(saved)
    print(json.dumps(saved),flush=True)


def bootstrap():
    destination=ROOT/'workspace';destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(str(ROOT/'calorie-agent-v4.zip')) as package:package.extractall(str(destination))
    print(json.dumps({'workspace':str(destination),'build_id':(destination/'BUILD_ID').read_text().strip()}))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['inventory','candidate-deploy','publish-candidate-layer','seed','bootstrap'])
    args=parser.parse_args();globals()[args.operation.replace('-','_')]()

if __name__=='__main__':main()
