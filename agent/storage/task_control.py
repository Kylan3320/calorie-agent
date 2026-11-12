"""Shared delegation controls and version fencing (diet and general work)."""


def authorized(message, quote):
    return bool(quote and quote.strip() and quote in message['text'])


def stop(run):
    if run.get('status') in {'queued', 'running'}:
        run['status'] = 'terminated' if run['status'] == 'queued' else 'stop_requested'


def binding(task, cycle):
    return {'task_id':task['id'], 'revision':task['revision'], 'cycle_id':cycle['id'],
            'redaction_revision':cycle['redaction_revision']}
