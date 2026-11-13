"""General public evidence and a bounded calculator, separate from food facts."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, DecimalException
import hashlib
import re
import time
from ..storage import deepseek_web

NUMBER = re.compile(r'(?<![0-9A-Za-z_.])[-+]?\d+(?:\.\d+)?')


def source(text, *, title='', url='', origin='web'):
    fetched = datetime.now(timezone.utc).isoformat()
    identity = hashlib.sha256((url+'\n'+text).encode()).hexdigest()[:24]
    return {'ref':'source:'+identity, 'text':text, 'title':title, 'url':url, 'origin':origin, 'fetched_at':fetched}


def sources(rt, p):
    saved = rt.session.setdefault('general_sources', {})
    operation = p['operation']
    if operation == 'read':
        item = saved.get(p.get('ref'))
        return {'status':'ok', 'data':{'source':deepcopy(item)}} if item else {'status':'pending','data':{'reason':'source_not_visible'}}
    if operation == 'text':
        quote = p.get('text','')
        if not quote or quote not in rt.message['text']:
            return {'status':'pending','data':{'reason':'current_user_quote_required'}}
        item = source(quote, title='用户提供资料', origin='user')
        saved[item['ref']] = item
        from ..storage.tasks import retain_acquired_source
        retain_acquired_source(rt,item)
        return {'status':'ok','data':{'source':item}}
    def reserve():
        used = rt.journal.get('general_web_requests', 0)
        maximum = getattr(rt.goal_run, 'web_limit', 3) if rt.goal_run else 3
        from ..storage.tasks import web_usage
        if web_usage(rt) >= maximum:
            raise deepseek_web.WebLookupError('web_budget_exhausted')
        rt.check_deadline()
        if rt.goal_run:
            rt.goal_run.check()
        rt.journal['general_web_requests'] = used+1
        rt.persist()
    try:
        if operation == 'search':
            hits = deepseek_web.search(p.get('query',''), deadline=rt.deadline, reserve=reserve)
            return {'status':'ok','data':{'candidates':hits,'fact_scope':'locator_only'}}
        url = deepseek_web.public_url(p.get('url'))
        if not url:
            return {'status':'pending','data':{'reason':'public_url_required'}}
        known = next((item for item in saved.values() if item.get('origin') == 'web' and item.get('url') == url), None)
        if known:
            return {'status':'ok','data':{'source':deepcopy(known)}}
        page = deepseek_web.fetch(url, deadline=rt.deadline, reserve=reserve)
        if not page:
            return {'status':'pending','data':{'reason':'source_unreadable','url':url}}
        item = source(page['text'], title=page['title'], url=url)
        item['truncated'] = page.get('truncated',False)
        saved[item['ref']] = item
        from ..storage.tasks import retain_acquired_source
        retain_acquired_source(rt,item)
        return {'status':'ok','data':{'source':item}}
    except deepseek_web.WebLookupError as exc:
        return {'status':'error','error':exc.code,'data':{'reason':exc.code}}


def calculate(rt,p):
    values, refs = p['values'], p['source_refs']
    known = rt.session.get('general_sources', {})
    if len(values) != len(refs):
        return {'status':'pending','data':{'reason':'operand_sources_required'}}
    for value, ref in zip(values,refs):
        text = rt.message['text'] if ref == 'current_user' else known.get(ref,{}).get('text','')
        numeric = NUMBER.findall(text)
        exact = value in text if p['operation']=='days' else any(Decimal(value)==Decimal(n) for n in numeric) if re.fullmatch(r'[-+]?\d+(?:\.\d+)?',value) else False
        if not text or not exact:
            return {'status':'pending','data':{'reason':'operand_not_in_visible_source','ref':ref}}
    try:
        if p['operation'] == 'days':
            if len(values) != 2: raise ValueError()
            result = str((date.fromisoformat(values[1])-date.fromisoformat(values[0])).days)
        else:
            nums = [Decimal(v) for v in values]
            if any(not n.is_finite() or abs(n)>Decimal('1e15') for n in nums): raise ValueError()
            result = nums[0]
            for num in nums[1:]:
                if p['operation'] == 'add': result += num
                elif p['operation'] == 'subtract': result -= num
                elif p['operation'] == 'multiply': result *= num
                elif p['operation'] == 'divide': result /= num
            if not result.is_finite() or abs(result)>Decimal('1e18'): raise ValueError()
            result = format(result.quantize(Decimal('0.000001')).normalize(),'f')
    except (ValueError, DecimalException, ZeroDivisionError):
        return {'status':'pending','data':{'reason':'invalid_calculation'}}
    item = source(f"{p['operation']}({', '.join(values)}) = {result}", title=p['basis'],origin='calculation')
    item.update(inputs=values, input_refs=refs, result=result, basis=p['basis'])
    rt.session.setdefault('general_sources',{})[item['ref']] = item
    return {'status':'ok','data':{'calculation':item}}


def validate_claims(candidate, sources):
    if not isinstance(candidate,dict) or candidate.get('outcome') not in {'complete','partial','waiting','failed'}:
        raise ValueError('invalid candidate outcome')
    if len(candidate.get('items',[])) > 30 or len(candidate.get('unresolved',[])) > 20:
        raise ValueError('candidate too large')
    for item in candidate.get('items',[]):
        quotes = item.get('evidence',[])
        if not quotes:
            raise ValueError('conclusion requires actual evidence')
        original = []
        for evidence in quotes:
            text = sources.get(evidence['ref'],{}).get('text','')
            quote = evidence['quote']
            if not quote.strip() or quote not in text:
                raise ValueError('quote not in actual source')
            original.append(quote)
        if not set(NUMBER.findall(item['text'])).issubset(set(NUMBER.findall(' '.join(original)))):
            raise ValueError('numeric claim lacks quoted or calculated evidence')
    if candidate['outcome'] == 'complete' and (not candidate.get('items') or candidate.get('unresolved')):
        raise ValueError('completion has unresolved coverage')
    return candidate
