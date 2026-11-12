"""Scoped COS v2 preflight, prepare, resume, verify and retirement entry.

Never deploys code or edits IAM. Use only resources explicitly authorised for
this task; the CLI does not infer authorisation from credentials being present.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent.storage.cos_transport import CosObjects
from agent.storage.cos_journal import Journal,encode
from agent.storage.cos_artifacts import thaw
from agent.storage.generations import Maintenance,DOMAINS,maintenance_tick
from agent.storage.retention import preflight


def verify(maint,task):
    return maint.verify_restart(task)


def denied_role_writes(maint,task,files):
    return maint.denied_role_writes(task,files)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['preflight','prepare','switch','resume','verify','collection-plan','collect'])
    parser.add_argument('--prefix',required=True)
    parser.add_argument('--task',default='candidate-v2')
    parser.add_argument('--old-role-envs',type=Path,help='private JSON mapping ingress/message/sync to credential files; only collect reads it')
    parser.add_argument('--isolated',action='store_true',help='assert this exact prefix is an explicitly authorised isolated data domain')
    args=parser.parse_args()
    if args.stage not in {'preflight','verify','collection-plan'} and not args.isolated:
        parser.error('mutations require the explicitly authorised isolated scope (--isolated); formal migration is outside this task')
    objects=CosObjects();maint=Maintenance(objects,args.prefix)
    if args.stage=='preflight':
        _,source=maint.control.location()
        _,report=preflight({d:Journal(objects,source+d+'/',readonly=True) for d in DOMAINS})
        result={'source':source,'target':maint.root+'generations/'+args.task+'/','retirement_ranges':[source+d+'/' for d in DOMAINS],'report':report}
    elif args.stage=='prepare':result=maint.prepare(args.task,compact=True)
    elif args.stage in {'switch','resume'}:
        from agent.storage.execution_pipeline import Pipeline
        from agent.storage.scf_dispatch import dispatch_event
        ledger=Journal(objects,maint.root+'control/maintenance/')
        active=ledger.get('active')
        if active and active['task']!=args.task:raise ValueError('resume the registered task first')
        ledger.commit({'active':active or {'task':args.task,'stage':'prepare'}})
        result=maintenance_tick(Pipeline(objects,args.prefix),dispatch=dispatch_event)
    elif args.stage=='verify':result=verify(maint,args.task)
    elif args.stage=='collection-plan':result=maint.collection_plan(args.task)
    else:
        if args.old_role_envs is None:parser.error('collect requires --old-role-envs outside the repository')
        verified=verify(maint,args.task)
        denial=denied_role_writes(maint,args.task,json.loads(args.old_role_envs.read_text()))
        result=maint.collect(args.task,restart_verified=bool(verified),denied_roles=denial)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
