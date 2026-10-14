"""Small connector-readable input files, committed atomically with job metadata."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor

from risk_queue import digest

MAX_BYTES = 48000


def chunks(rows):
    result, current = [], []
    for row in rows:
        trial = current+[row]
        if len(json.dumps(trial, ensure_ascii=False).encode()) > MAX_BYTES:
            if not current or len(json.dumps([row], ensure_ascii=False).encode()) > MAX_BYTES:
                raise RuntimeError('단일 원문이 연결 도구 한도 초과 — 누락 없이 보류')
            result.append(current)
            current = [row]
        else:
            current = trial
    if current:
        result.append(current)
    return result


def split_job(job):
    full = copy.deepcopy(job)
    small = copy.deepcopy(job)
    values = {}
    descriptors = {}
    for field, rows in [('items', full['input']['items']),
                        ('raw_articles', full['input']['raw_articles']),
                        ('body_manifest', full['body_manifest'])]:
        refs = []
        for i, part in enumerate(chunks(rows)):
            path = 'inputs/'+job['job_id']+'/'+field+'-'+str(i).zfill(3)+'.json'
            values[path] = part
            refs.append({'path': path, 'sha256': digest(part), 'count': len(part)})
        descriptors[field] = refs
    small['input'].pop('items')
    small['input'].pop('raw_articles')
    small.pop('body_manifest')
    small['input_parts'] = descriptors
    small['input_count'] = len(full['input']['items'])
    small['raw_count'] = len(full['input']['raw_articles'])
    return small, values


def hydrate(queue, job):
    value = copy.deepcopy(job)
    if 'input_parts' not in job:
        return value  # Small legacy owner fixtures remain readable.
    fields = {}
    jid = job['job_id']
    for field in ('items','raw_articles','body_manifest'):
        refs = job['input_parts'].get(field)
        if not isinstance(refs, list):
            raise RuntimeError('큐 분할 색인 누락')
        paths = [r['path'] for r in refs]
        if len(set(paths)) != len(paths) or any(not p.startswith('inputs/'+jid+'/'+field+'-') or '..' in p for p in paths):
            raise RuntimeError('다른 회차·중복 분할 경로')
        with ThreadPoolExecutor(max_workers=6) as pool:
            parts = list(pool.map(lambda r: queue.get(r['path'])[0], refs))
        rows = []
        for ref, part in zip(refs, parts):
            if not isinstance(part, list) or digest(part) != ref['sha256'] or len(part) != ref['count']:
                raise RuntimeError('분할 입력 누락·해시 오류')
            rows.extend(part)
        fields[field] = rows
    value['input'].update(items=fields['items'], raw_articles=fields['raw_articles'])
    value['body_manifest'] = fields['body_manifest']
    if digest(value['input']) != job['input_hash'] or digest(value['body_manifest']) != job['body_manifest_hash']:
        raise RuntimeError('재조립 입력·본문 해시 불일치')
    if len(fields['items']) != job['input_count'] or len(fields['raw_articles']) != job['raw_count']:
        raise RuntimeError('재조립 입력 건수 불일치')
    return value
