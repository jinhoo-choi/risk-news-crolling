"""Private frozen exposure and durable unresolved articles. No SMTP or AI calls."""
import base64
import copy
import csv
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from risk_queue import JOB_ID, digest, now

REVIEW_INDEX = 'review/index.json'


def exposure_date(raw):
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    dates = {row.get('기준일', '') for row in rows}
    if len(dates) != 1:
        raise RuntimeError('잔고 기준일 누락·혼합 — 발송 보류')
    try:
        return date.fromisoformat(dates.pop())
    except (ValueError, TypeError):
        raise RuntimeError('잔고 기준일 형식 오류') from None


def freeze_exposure(job, raw, captured_at):
    ref_date = exposure_date(raw).isoformat()
    path = 'inputs/'+job['job_id']+'/exposure.json'
    job['exposure_snapshot'] = {'path': path, 'sha256': hashlib.sha256(raw).hexdigest(),
                                'ref_date': ref_date, 'captured_at': captured_at}
    job['continuity_version'] = 1
    return {path: {'schema_version': 1, 'encoding': 'base64',
                   'content': base64.b64encode(raw).decode()}}


def read_exposure(queue, job, config):
    ref = job.get('exposure_snapshot')
    if not ref:
        return None  # Legacy jobs retain their strict live-file hash guard.
    if ref['path'] != 'inputs/'+job['job_id']+'/exposure.json':
        raise RuntimeError('다른 회차 잔고 스냅샷 경로')
    value, _ = queue.get(ref['path'])
    if not value or value.get('schema_version') != 1 or value.get('encoding') != 'base64':
        raise RuntimeError('잔고 스냅샷 누락·형식 오류')
    try:
        raw = base64.b64decode(value['content'], validate=True)
    except (ValueError, TypeError):
        raise RuntimeError('잔고 스냅샷 인코딩 오류') from None
    actual = hashlib.sha256(raw).hexdigest()
    if actual != ref['sha256'] or actual != job['input']['exposure_hash']:
        raise RuntimeError('잔고 스냅샷 원본 해시 불일치')
    ref_date = exposure_date(raw)
    maximum = config.get('exposure_max_age_days', 7)
    if type(maximum) is not int or maximum < 1:
        raise RuntimeError('잔고 허용 기준일 설정 오류')
    age = (now().astimezone(timezone(timedelta(hours=9))).date()-ref_date).days
    if ref_date.isoformat() != ref['ref_date'] or not 0 <= age <= maximum:
        raise RuntimeError('잔고 스냅샷 기준일 불일치·오래된 잔고 — 발송 보류')
    return raw


def review_index(queue):
    index, sha = queue.get(REVIEW_INDEX)
    if index is None:
        index = {'schema_version': 1, 'pending': []}
    if not isinstance(index, dict):
        raise RuntimeError('영속 보류 색인 형식 오류')
    ids = index.get('pending')
    if (index.get('schema_version') != 1 or not isinstance(ids, list)
            or any(not isinstance(i, str) or not JOB_ID.fullmatch(i) for i in ids)
            or len(ids) != len(set(ids))):
        raise RuntimeError('영속 보류 색인 오류')
    return index, sha


def _validated_review(article_id, snapshot):
    record, sha = snapshot
    if (not record or record.get('article_id') != article_id or record.get('status') != 'pending'
            or record['item']['article_id'] != article_id or record['body']['article_id'] != article_id
            or record.get('source_hash') != digest({'item': record['item'], 'body': record['body']})):
        raise RuntimeError('영속 보류 원본 누락·변경')
    return record, sha


def read_review(queue, article_id):
    return _validated_review(article_id, queue.get('review/'+article_id+'.json'))


def _review_snapshots(queue, article_ids):
    """Bound independent reads; retain order and each original CAS SHA."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        snapshots = list(pool.map(lambda aid: queue.get('review/'+aid+'.json'), article_ids))
    return dict(zip(article_ids, snapshots))


def review_source(item, body):
    """Small source identity; timestamps/attempts are not new evidence."""
    return {'url': item.get('url', ''),
            'published_at': item.get('published_at') or item.get('pubDate'),
            'item_hash': digest({k: item.get(k, '') for k in ('url', 'title', 'description')}),
            'body_hash': digest({'body': body.get('body', ''),
                                 'body_status': body.get('body_status')}),
            'body_status': body.get('body_status')}


def review_catalog(queue, migrate=False):
    """Normal collection reads metadata only; explicit maintenance migrates sources."""
    index, sha = review_index(queue)
    before = copy.deepcopy(index)
    catalog = index.setdefault('sources', {})
    if not isinstance(catalog, dict):
        raise RuntimeError('보류 원문 색인 형식 오류')
    missing = [aid for aid in index['pending'] if aid not in catalog] if migrate else []
    for aid, snapshot in _review_snapshots(queue, missing).items():
        record, _ = _validated_review(aid, snapshot)
        catalog[aid] = record.get('last_source') or review_source(record['item'], record['body'])
    for aid, source in catalog.items():
        if (not JOB_ID.fullmatch(aid) or not isinstance(source, dict)
                or any(not isinstance(source.get(k), str) or len(source[k]) != 64
                       for k in ('item_hash', 'body_hash'))
                or source.get('body_status') not in {'verified', 'missing', 'unreadable', 'unconfirmed', None}):
            raise RuntimeError('보류 원문 색인 형식 오류')
    index['pending'] = [aid for aid in index['pending']
                        if aid not in catalog or review_age(catalog[aid], now()) == 'eligible']
    if index != before:
        queue.put(REVIEW_INDEX, index, sha)
        index, sha = review_index(queue)
    return index, sha


def pending_reviews(queue):
    index, _ = review_index(queue)
    snapshots = _review_snapshots(queue, index['pending'])
    return [_validated_review(aid, snapshots[aid])[0] for aid in index['pending']]


def review_age(item, current):
    """Publication age, never collection/first-seen time or a guessed 'now'."""
    value = item.get('published_at') or item.get('pubDate')
    try:
        if not isinstance(value, str):
            return 'date_unconfirmed'
        try:
            published = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            published = parsedate_to_datetime(value)
        if published.tzinfo is None or published > current:
            return 'date_unconfirmed'
        return 'eligible' if current-published <= timedelta(hours=12) else 'expired'
    except (ValueError, TypeError, OverflowError):
        return 'date_unconfirmed'


def prepare_reviews(queue, job, verdicts, reason='analysis_pending', reasons=None):
    """Return one atomic batch; unresolved copies must persist BEFORE any SMTP."""
    index, index_sha = review_index(queue)
    pending = set(index['pending'])
    values, expected, resolutions = {}, {}, {}
    items = {a['article_id']: a for a in job['input']['items']}
    bodies = {b['article_id']: b for b in job['body_manifest']}
    if (len(items) != len(job['input']['items']) or len(bodies) != len(job['body_manifest'])
            or set(items) != set(bodies)):
        raise RuntimeError('영속 보류 입력·본문 ID 중복·누락')
    if any(aid not in items or aid not in bodies or not JOB_ID.fullmatch(aid) for aid in verdicts):
        raise RuntimeError('보류 ID·본문 계약 오류')
    snapshots = _review_snapshots(queue, [aid for aid, verdict in verdicts.items()
                                         if verdict is None or aid in pending])
    instant = now().isoformat()
    catalog = index.setdefault('sources', {})
    for aid, verdict in verdicts.items():
        path = 'review/'+aid+'.json'
        if aid in pending:
            record, sha = _validated_review(aid, snapshots[aid])
        if verdict is not None:
            if aid in pending:
                resolutions[aid] = {'sha': sha, 'relevant': verdict}
                expected[path] = sha
            continue
        if aid not in pending:
            record, sha = snapshots[aid]
            if (record is not None and record.get('status') != 'resolved'
                    and not (aid in catalog and record.get('status') == 'pending')):
                raise RuntimeError('보류 원본과 색인 불일치')
        if record and record.get('status') == 'resolved' and record.get('resolution_kind') == 'owner_excluded':
            continue  # Expiry/replay must not reopen an explicit owner no-send decision.
        previous = record or {}
        origins = list(dict.fromkeys(previous.get('origin_jobs', [])+[job['job_id']]))
        same = previous.get('last_job_id') == job['job_id'] and previous.get('status') == 'pending'
        source = previous if previous.get('status') == 'pending' else {
            'item': items[aid], 'body': bodies[aid]}
        record = {'schema_version': 1, 'article_id': aid, 'status': 'pending',
                  'first_seen_at': previous.get('first_seen_at', instant), 'last_seen_at': instant,
                  'attempts': previous.get('attempts', 0)+(0 if same else 1),
                  'origin_jobs': origins, 'last_job_id': job['job_id'], 'reason': reason,
                  'last_review_reason': (reasons or {}).get(aid, reason),
                  'last_body_status': bodies[aid].get('body_status', 'unconfirmed'),
                  'last_body_quality_reason': bodies[aid].get('body_quality_reason'),
                  'item': copy.deepcopy(source['item']), 'body': copy.deepcopy(source['body'])}
        record['source_hash'] = digest({'item': record['item'], 'body': record['body']})
        record['last_source'] = review_source(items[aid], bodies[aid])
        # Original publication age cannot be reset by a later crawl.
        record['last_source']['published_at'] = record['item'].get('published_at') or record['item'].get('pubDate')
        record['analysis_state'] = 'unprocessed' if aid in job.get('_unprocessed_ids', []) else 'review'
        record['last_source']['analysis_state'] = record['analysis_state']
        catalog[aid] = record['last_source']
        values[path], expected[path] = record, sha
        if job.get('review_pass_version') == 2 and review_age(catalog[aid], now()) != 'eligible':
            index['pending'] = [i for i in index['pending'] if i != aid]
            pending.discard(aid)
        if aid not in pending and (job.get('review_pass_version') != 2
                                   or review_age(catalog[aid], now()) == 'eligible'):
            index['pending'].append(aid)
            pending.add(aid)
    if values or resolutions:
        expected[REVIEW_INDEX] = index_sha
    if values:
        values[REVIEW_INDEX] = index
    return values, expected, resolutions


def finish_reviews(queue, resolutions, job_id):
    """Resolve reviewed IDs only in the same commit as delivery/history ACK."""
    if not resolutions:
        return {}, {}
    index, index_sha = review_index(queue)
    values, expected = {}, {REVIEW_INDEX: index_sha}
    if any(aid not in index['pending'] or type(resolution['relevant']) is not bool
           for aid, resolution in resolutions.items()):
        raise RuntimeError('보류 해결 색인·판정 오류')
    snapshots = _review_snapshots(queue, list(resolutions))
    for aid, resolution in resolutions.items():
        record, sha = _validated_review(aid, snapshots[aid])
        if sha != resolution['sha']:
            raise RuntimeError('보류 원본 변경 — ACK 전 해결 금지')
        record.update(status='resolved', resolved_at=now().isoformat(),
                      resolved_by_job=job_id, relevant=resolution['relevant'])
        path = 'review/'+aid+'.json'
        values[path], expected[path] = record, sha
    index['pending'] = [aid for aid in index['pending'] if aid not in resolutions]
    for aid in resolutions:
        index.get('sources', {}).pop(aid, None)
    values[REVIEW_INDEX] = index
    return values, expected


def delivery_verdicts(job, result, excluded_ids=()):
    """Continuity jobs hold missing-body positives until native body checks can pass."""
    verdicts = {r['article_id']: r['relevant'] for r in result['classifications']}
    verdicts.update(dict.fromkeys(result.get('unprocessed_ids', [])))
    if job.get('continuity_version') == 1:
        for aid, analysis in result['analyses'].items():
            if aid in job.get('_validation_holds', {}):
                continue
            if analysis['body_status'] != 'verified':
                verdicts[aid] = None
        for article in job['input']['items']:
            aid = article['article_id']
            if (article.get('_review_replay') and verdicts[aid] is True
                    and review_age(article, now()) != 'eligible'):
                verdicts[aid] = None  # Aging during analysis must not reopen eligibility.
    for aid in job.get('_validation_holds', {}):
        verdicts[aid] = None
    for aid in set(excluded_ids).intersection(verdicts):
        verdicts[aid] = False  # Owner delivery decision; saved analysis stays unchanged.
    return verdicts


def delivery_view(job, result, excluded_ids=()):
    """Exclude deferred rows from native processing, keeping saved answers immutable."""
    deferred = {aid for aid, value in delivery_verdicts(job, result, excluded_ids).items() if value is None} | set(excluded_ids)
    view, answers = copy.deepcopy(job), copy.deepcopy(result)
    view['input']['raw_articles'] = [a for a in view['input']['raw_articles'] if a['article_id'] not in deferred]
    view['input']['items'] = [a for a in view['input']['items'] if a['article_id'] not in deferred]
    view['body_manifest'] = [b for b in view['body_manifest'] if b['article_id'] not in deferred]
    if 'unprocessed_ids' in answers:
        answers['unprocessed_ids'] = []
    answers['classifications'] = [r for r in answers['classifications'] if r['article_id'] not in deferred]
    answers['analyses'] = {aid: a for aid, a in answers['analyses'].items() if aid not in deferred}
    answers['event_groups'] = [dict(g, article_ids=[aid for aid in g['article_ids'] if aid not in deferred])
                              for g in answers.get('event_groups', [])
                              if len(set(g['article_ids'])-deferred) >= 2]
    for aid, fields in job.get('_validation_omissions', {}).items():
        if aid in answers['analyses']:
            for field in fields:
                answers['analyses'][aid][field] = None if field == 'customer_notice' else ''
    return view, answers
