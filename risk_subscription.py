"""Private queue bridge for Risk; API0 subscription collection and consumption."""
import argparse
import base64
import contextlib
import copy
import hashlib
import html
import io
import json
import os
import re
import smtplib
import sys
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from risk_queue import Queue, JOB_ID, now, digest
from risk_result_validator import validate, utc_time
from risk_body_quality import body_quality
from risk_delivery import (Delivery, address_key, report_headers, rejection_evidence,
                           retry_targets, checked_delivery_failure)
from risk_partial_close import checked_closure, source_version
from risk_provider import Provider
from risk_outbox import make_packet, validate_packet, store_packet, legacy_packet, assert_report_content
from risk_shards import split_job, hydrate, chunks
from risk_audit import checkpoint, record_error, record_errors, run_audited, observe_pending, persist_unhandled
from risk_continuity import (review_catalog, review_source, freeze_exposure, read_exposure, pending_reviews,
                             prepare_reviews, finish_reviews, delivery_view, delivery_verdicts, read_review,
                             review_age, prune_expired_reviews)

CONFIG = 'risk_subscription_config.json'
POLICY = 'risk_policy.json'
TERMINAL = {'completed', 'verified', 'expired', 'cancelled'}


def notify_best_effort(error, context=''):
    try:
        notify_error(error, context)
    except Exception as error:
        record_error('owner_notification', error, 'owner_notification_failed')
        print('담당자 오류 알림 실패; 수집 계속/API0')


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def policy():
    value = json.loads(Path(POLICY).read_text())
    for key, path in [('filter_rules', 'filter_prompt.txt'), ('action_rules', 'action_prompt.txt')]:
        if value[key]['path'] != path or file_hash(path) != value[key]['sha256']:
            raise RuntimeError('분석 프롬프트와 정책 해시 불일치')
    return value


def import_bot():
    os.environ.update(RISK_ENGINE='subscription', ANTHROPIC_API_KEY='subscription-api-disabled')
    for key in ('EMAIL_SENDER', 'EMAIL_PASSWORD', 'EMAIL_RECEIVER', 'NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET'):
        os.environ.setdefault(key, '' if key.startswith('EMAIL') else 'not-needed-by-consumer')
    import naver_news_monitor as bot
    return bot


def current_config(queue, verification=False, articles=(), document=''):
    raw, _ = queue.source(CONFIG)
    config = json.loads(raw)
    if config.get('engine') not in {'subscription', 'api', 'paused'}:
        raise RuntimeError('운영 엔진 오류')
    if not verification and config['engine'] != 'subscription':
        raise RuntimeError('구독 운영이 아님 — 발송 보류')
    if config.get('policy_version') != 'risk-subscription-v1':
        raise RuntimeError('분석 정책 버전 오류')
    excluded = config.get('excluded_article_ids', [])
    if (not isinstance(excluded, list)
            or any(not isinstance(aid, str) or not JOB_ID.fullmatch(aid) for aid in excluded)
            or len(excluded) != len(set(excluded))):
        raise RuntimeError('담당자 발송 제외 목록 오류')
    if any(a['article_id'] in excluded and (a['url'] in document or html.escape(a['url'], quote=True) in document)
           for a in articles):
        raise RuntimeError('담당자가 제외한 기사 포함 — 발송 금지')
    return config


def expire_and_check(queue, suppressed_versions=None):
    index, sha = queue.get('index.json')
    index = index or {'jobs': []}
    closures = index.get('partial_closures', {})
    if not isinstance(closures, dict):
        raise RuntimeError('부분 종결 색인 형식 오류')
    for jid in dict.fromkeys([*index['jobs'], *closures]):
        job, old_sha = queue.get('jobs/'+jid+'.json')
        if not job:
            raise RuntimeError('색인 회차 읽기 실패')
        if job.get('status') == 'delivery_rejected':
            close_rejected(queue, jid)
            job, old_sha = queue.get('jobs/'+jid+'.json')
        if job.get('status') == 'delivery_failed':
            checked_delivery_failure(job)
            continue
        if jid in closures or job.get('status') == 'closed_partial':
            if job.get('job_id') != jid:
                raise RuntimeError('부분 종결 색인 회차 불일치')
            record = checked_closure(job, index)
            if suppressed_versions is not None:
                suppressed_versions.update(record['versions'])
            continue
        observe_pending(queue, job)
        if job.get('status') in TERMINAL:
            continue
        if job.get('status') == 'pending' and not job.get('receipts') and not job.get('attempted_count', 0) and utc_time(job['expires_at']) <= now():
            values, expected = {}, {}
            if job.get('mode') == 'production':
                if 'input' not in job:
                    raise RuntimeError('만료 회차 원본 누락 — 이월 전 종결 금지')
                full = hydrate(queue, job)
                if digest(full['input']) != job['input_hash'] or digest(full['body_manifest']) != job['body_manifest_hash']:
                    raise RuntimeError('만료 회차 원본 해시 오류 — 이월 전 종결 금지')
                values, expected, _ = prepare_reviews(queue, full,
                    {a['article_id']: None for a in full['input']['items']}, reason='expired_unprocessed')
            job.update(status='expired', expired_at=now().isoformat())
            path = 'jobs/'+jid+'.json'
            if values:
                values[path], expected[path] = job, old_sha
                queue.put_many(values, expected)
            else:
                queue.put(path, job, old_sha)
            print('만료 회차 입력 보존 완료; 만료 반복 알림 생략/API0')
            continue
        raise RuntimeError('미종결 구독 회차 존재 — 새 회차·재전송 금지')
    return index, sha


def close_rejected(queue, jid):
    """Atomically carry every input before releasing a proven zero-delivery run."""
    path = 'jobs/'+jid+'.json'
    small, old_sha = getattr(queue, 'get_fresh', queue.get)(path)
    if not small or small.get('status') != 'delivery_rejected':
        return False
    outcomes = rejection_evidence(small)
    if any(v['state'] == 'accepted' for v in outcomes.values()) or retry_targets(small, ready=False):
        return False
    job = hydrate(queue, small)
    if digest(job['input']) != job['input_hash'] or digest(job['body_manifest']) != job['body_manifest_hash']:
        raise RuntimeError('미접수 실패 원본 해시 오류 — 이월 전 종결 금지')
    packet, _ = queue.get('outbox/'+jid+'.json')
    result, _ = queue.get('results/'+jid+'.json')
    validate_packet(packet, job, result)
    rejection_evidence(small, packet['packet_hash'])
    ids = [a['article_id'] for a in job['input']['items']]
    values, expected, _ = prepare_reviews(queue, job, dict.fromkeys(ids), reason='delivery_failed_unprocessed')
    small.update(status='delivery_failed', delivery_failed_at=now().isoformat(),
                 delivery_failure={'evidence_hash': digest(small['smtp_evidence']),
                    'input_hash': job['input_hash'], 'body_manifest_hash': job['body_manifest_hash'],
                    'carried_ids': ids})
    checked_delivery_failure(small)
    values[path], expected[path] = small, old_sha
    queue.put_many(values, expected)
    return True


def prune_terminal_inputs(queue, retention_hours=12, max_batches=None):
    """Prune terminal payloads after the recovery window; keep receipts and hashes."""
    if type(retention_hours) is not int or retention_hours < 1:
        raise ValueError('입력 보존기간은 1시간 이상 정수')
    cutoff = now()-timedelta(hours=retention_hours)
    directories = ('results', 'outbox', 'previews', 'logs')
    blobs = {directory+'/'+e['path']: e['sha'] for directory in directories
             for e in queue.list_entries(directory) if e['type'] == 'blob'}
    pruned_rounds, pruned_files, batches = 0, 0, 0
    for entry in queue.list_entries('jobs'):
        name = entry['path']
        if entry['type'] != 'blob' or not name.endswith('.json') or not JOB_ID.fullmatch(name[:-5]):
            continue
        jid = name[:-5]
        path = 'jobs/'+jid+'.json'
        job, sha = queue.get(path)
        if not job or job.get('job_id') != jid or job.get('status') not in TERMINAL:
            continue
        if any(v != 'accepted' for v in job.get('receipts', {}).values()):
            continue
        if job['status'] == 'completed' and (not job.get('ack') or not job.get('history_saved')
                or not job.get('receipts') or set(job['receipts']) != set(job.get('expected_receipts', []))):
            continue
        if job['status'] in {'expired', 'cancelled'} and job.get('attempted_count', 0):
            continue
        ended = job.get(job['status']+'_at')
        if not ended:
            continue  # Never infer the age of terminal state from creation time.
        try:
            if utc_time(ended) >= cutoff:
                continue
        except (ValueError, TypeError):
            continue
        if job.get('input_retention_hours'):
            continue
        shards = queue.list_input_entries(jid)
        expected, deletions = {path: sha}, []
        for shard in shards:
            name = shard['path']
            if (shard['type'] != 'blob' or '/' in name or not name.endswith('.json')
                    or not JOB_ID.fullmatch(name.removesuffix('.json'))):
                raise RuntimeError('큐 입력 분할 파일 목록 오류 — 정리 보류')
            part = 'inputs/'+jid+'/'+name
            deletions.append(part)
            expected[part] = shard['sha']
        for directory in directories:
            part = directory+'/'+jid+'.json'
            payload_sha = blobs.get(part)
            if payload_sha:
                deletions.append(part)
                expected[part] = payload_sha
        original = job.get('input') or {}
        if not deletions and not (original.get('items') or original.get('raw_articles') or job.get('body_manifest')):
            continue
        # Shards precede the four payload files. The final batch alone publishes
        # inputs_pruned_at and removes embedded input. Each prior batch keeps a
        # durable counter and every receipt/hash/ACK for safe interrupted resume.
        progress = job.get('inputs_pruning', {'files': 0, 'bytes': 0})
        sizes = {'inputs/'+jid+'/'+s['path']: s.get('size', 0) for s in shards}
        # Keep all non-input payloads in the final transaction. The Queue adapter
        # intentionally permits deleting them only with inputs_pruned_at.
        chunks = [deletions[i:min(i+250, len(shards))] for i in range(0, len(shards), 250)]
        payloads = deletions[len(shards):]
        if chunks and len(chunks[-1])+len(payloads) <= 250:
            chunks[-1].extend(payloads)
        elif payloads or not chunks:
            chunks.append(payloads)
        for position, batch in enumerate(chunks):
            if max_batches is not None and batches >= max_batches:
                return {'pruned_rounds': pruned_rounds, 'pruned_payload_files': pruned_files}
            final = position == len(chunks)-1
            progress = {'files': progress['files']+len(batch),
                        'bytes': progress['bytes']+sum(sizes.get(p, 0) for p in batch)}
            if final:
                original.pop('items', None)
                original.pop('raw_articles', None)
                job.pop('body_manifest', None)
                job.pop('inputs_pruning', None)
                job.update(inputs_pruned_at=now().isoformat(), input_retention_hours=retention_hours,
                           inputs_pruned_files=progress['files'], inputs_pruned_bytes=progress['bytes'])
            else:
                job['inputs_pruning'] = progress
            leases = {path: sha, **{p: expected[p] for p in batch}}
            queue.put_many({path: job}, leases, deletions=batch)
            batches += 1
            pruned_files += len(batch)
            observed, sha = getattr(queue, 'get_fresh', queue.get)(path)
            if observed != job or not sha:
                raise RuntimeError('기사 정리 진행 원장 재확인 실패')
            if final:
                pruned_rounds += 1
    return {'pruned_rounds': pruned_rounds, 'pruned_payload_files': pruned_files}


def prune_articles(queue, max_batches=None):
    """Maintenance only. No expiry, collection, analysis, SMTP, seen write or ACK."""
    index, sha = queue.get('index.json')
    if not index or not isinstance(index.get('jobs'), list) or not isinstance(index.get('partial_closures', {}), dict):
        raise RuntimeError('정리 회차 색인 누락')
    leases = {'index.json': sha}
    for jid in dict.fromkeys([*index['jobs'], *index.get('partial_closures', {})]):
        if not JOB_ID.fullmatch(jid):
            raise RuntimeError('정리 회차 ID 오류')
        job, job_sha = queue.get('jobs/'+jid+'.json')
        if not job or job.get('job_id') != jid or job.get('status') not in TERMINAL | {'closed_partial', 'delivery_failed'}:
            return {'status': 'preserved_active', 'pruned_reviews': 0, 'paid_ai_calls': 0, 'smtp_calls': 0}
        if job['status'] == 'closed_partial':
            checked_closure(job, index)
        elif job['status'] == 'delivery_failed':
            checked_delivery_failure(job)
        leases['jobs/'+jid+'.json'] = job_sha
    count = prune_expired_reviews(queue, leases, max_batches=max_batches)
    return {'status': 'pruned', 'pruned_reviews': count, 'paid_ai_calls': 0, 'smtp_calls': 0,
            **prune_terminal_inputs(queue, max_batches=max_batches)}


def reusable_exclusions(queue, index, job):
    """Optional exact-context answers; never remove IDs or reuse positives/nulls.

    Changed history/exposure/day/policy requires fresh review even for the same
    source. Consumers still validate the complete newly submitted result.
    """
    current = now()
    for jid in reversed(index['jobs']):
        previous, _ = queue.get('jobs/'+jid+'.json')
        if not previous or previous.get('status') != 'completed' or previous.get('mode') != 'production':
            continue
        created = utc_time(previous['created_at'])
        if (previous.get('inputs_pruned_at') or not timedelta(0) <= current-created <= timedelta(hours=12)
                or created.astimezone(timezone(timedelta(hours=9))).date()
                   != current.astimezone(timezone(timedelta(hours=9))).date()
                or previous['policy_hash'] != job['policy_hash']
                or any(previous['input'].get(k) != job['input'].get(k)
                       for k in ('history_hash', 'exposure_hash'))):
            return []
        old = hydrate(queue, previous)
        result, _ = queue.get('results/'+jid+'.json')
        checked = validate(result, old, old['input'], old['body_manifest'])
        sources = {a['article_id']: a for a in old['input']['items']}
        bodies = {b['article_id']: b for b in old['body_manifest']}
        new_sources = {a['article_id']: a for a in job['input']['items']}
        new_bodies = {b['article_id']: b for b in job['body_manifest']}
        return [copy.deepcopy(row) for row in result['classifications']
                if row['relevant'] is False and row['article_id'] not in checked.get('held_articles', {})
                and (old.get('exclusion_review_version') == 2
                     or row.get('exclusion_review', {}).get('reviewed') is True)
                and row['article_id'] in new_sources
                and sources[row['article_id']] == new_sources[row['article_id']]
                and bodies[row['article_id']] == new_bodies[row['article_id']]
                and review_age(new_sources[row['article_id']], current) == 'eligible']
    return []


def collect(config, queue, verification=False, reviews_only=False, review_ids=None):
    collection_started_at = now()
    checkpoint('collect')
    if reviews_only and verification:
        raise RuntimeError("보류 재수집은 운영 회차만 허용")
    queue.preflight()
    if not verification:
        current_config(queue)
    suppressed_versions = set()
    index, sha = expire_and_check(queue, suppressed_versions)
    after_job_id = None
    if reviews_only:
        later_reviews = False
        for jid in reversed(index['jobs']):
            parent, _ = queue.get('jobs/'+jid+'.json')
            if parent.get('mode') != 'production':
                continue
            if parent['input'].get('snapshot_mode') == 'review_replay':
                later_reviews = True
                continue
            expected = parent.get('expected_receipts', [])
            receipts = parent.get('receipts', {})
            if (parent.get('status') != 'completed' or parent.get('ack') is not True
                    or parent.get('history_saved') is not True or not expected
                    or set(receipts) != set(expected)
                    or any(receipts[key] != 'accepted' for key in expected)):
                raise RuntimeError('정규 발송·이력·ACK 완료 전 보류 후속 수집 금지')
            if later_reviews:
                raise RuntimeError('해당 정규 회차의 보류 후속 회차 이미 존재')
            after_job_id = jid
            break
        if after_job_id is None:
            raise RuntimeError('보류 후속 수집의 완료된 정규 회차 없음')
    bot = import_bot()
    for state_file in ('seen_news.json', 'exposure_data.csv'):
        data, _ = queue.source(state_file)
        Path(state_file).write_bytes(data)
    exposure_raw = Path('exposure_data.csv').read_bytes()
    captured_at = now().isoformat()
    review_state = {'pending': [], 'sources': {}} if verification else review_catalog(queue)[0]
    catalog = dict(review_state.get('sources', {}))
    # Legacy holds without a small index are skipped, never read before fresh news.
    for aid in set(review_state['pending'])-catalog.keys():
        catalog[aid] = {'published_at': None, 'item_hash': None, 'body_hash': None}
    excluded = set(current_config(queue, verification).get('excluded_article_ids', []))
    catalog = {aid: source for aid, source in catalog.items() if aid not in excluded}
    ages = {aid: review_age(source, now()) for aid, source in catalog.items()}
    # No automatic evidence retry: default follow-up is first analysis only.
    selected = set(review_ids or [])
    if selected and not reviews_only:
        raise RuntimeError('기사 지정은 보류 후속 수집에만 허용')
    if selected-set(review_state['pending']):
        raise RuntimeError('활성 보류에 없는 기사 지정')
    reviews = []
    if reviews_only:
        selected = selected or {aid for aid, source in catalog.items()
                               if source.get('analysis_state') == 'unprocessed'
                               and aid in review_state['pending']}
        for aid in sorted(selected-excluded):
            if ages.get(aid) == 'expired':
                continue
            record, _ = read_review(queue, aid)
            if review_age(record['item'], now()) != 'eligible':
                continue
            reviews.append(record)
            previous_source = record.get('last_source') or review_source(record['item'], record['body'])
            catalog[aid] = previous_source
        if not reviews:
            print(json.dumps({'job_id': 'pending-review', 'status': 'no_change', 'inputs': 0, 'paid_ai_calls': 0}))
            return
    replay = {r['article_id']: r for r in reviews}
    first_analysis = {aid for aid, record in replay.items() if record.get('analysis_state') == 'unprocessed'}
    seen = bot.load_seen_urls()
    exposure = bot.load_exposure_data()
    keywords = [] if reviews_only else list(dict.fromkeys(bot.KEYWORDS+bot.get_overseas_keywords(exposure)))
    raw, failures, search_errors = [], {}, []
    lock = threading.Lock()
    get = bot.requests.get
    def checked_get(url, **kwargs):
        query = kwargs.get('params', {}).get('query')
        try:
            response = get(url, **kwargs)
            response.raise_for_status()
            response.json()  # A 200 response alone does not prove collection succeeded.
            with lock:
                failures.pop(query, None)
            return response
        except Exception as error:
            with lock:
                failures[query] = True
                search_errors.append((query, error))
            raise
    checkpoint('search')
    with patch.object(bot.requests, 'get', checked_get), ThreadPoolExecutor(max_workers=5) as pool:
        for rows in pool.map(bot.crawl_naver_news, keywords):
            raw.extend(a for a in rows if a.get('url') and a['url'] not in seen)
    for query, error in search_errors:
        record_error('search', error, 'search_request_failed', query=query, recovered=query not in failures)
    if failures:
        raise RuntimeError('일부 검색 수집 실패 — 정상 0건·부분 수집 발송 금지')
    # Exact URL duplicates are the same source; never truncate unique sources.
    unique, urls = [], set()
    for article in raw:
        if article['url'] not in urls:
            urls.add(article['url'])
            unique.append(article)
    instant = now()
    raw = [a for a in unique if verification or review_age(a, instant) != 'expired']
    items = []
    by_url = {s['url']: s for s in catalog.values() if s.get('url')}
    previous_sources = dict(catalog)
    for article in raw:
        article['article_id'] = hashlib.sha256((article['url']+'\0'+article['title']).encode()).hexdigest()[:24]
        article['description'] = article.get('desc', '')
        article['published_at'] = article.get('pubDate', '')
        previous = catalog.get(article['article_id']) or by_url.get(article['url'])
        if previous:
            previous_sources[article['article_id']] = previous
            article['_review_replay'] = True
            article['published_at'] = previous['published_at']
        if (article['article_id'] in excluded or previous and not reviews_only
                and (review_age(previous, now()) != 'eligible'
                     or review_source(article, {})['item_hash'] == previous['item_hash'])):
            continue
        if not bot.is_hard_excluded(article['title'], article.get('desc', ''), article['url'])[0]:
            items.append(article)
    held_unchanged = {a['article_id'] for a in raw if a['article_id'] in previous_sources
                      and (review_age(previous_sources[a['article_id']], now()) != 'eligible' or
                           review_source(a, {})['item_hash'] == previous_sources[a['article_id']]['item_hash'])}
    raw = [a for a in raw if a['article_id'] not in excluded
           and (reviews_only or a['article_id'] not in held_unchanged)]
    item_ids = {a['article_id'] for a in items}
    raw_ids = {a['article_id'] for a in raw}
    for record in reviews:
        aid = record['article_id']
        if aid not in item_ids:
            items.append(copy.deepcopy(record['item']))
        if aid not in raw_ids:
            raw.append(copy.deepcopy(record['item']))
    for article in items:
        if article['article_id'] in replay:
            article['_review_replay'] = True
            article['_review_first_seen_at'] = replay[article['article_id']]['first_seen_at']
    body_errors = []
    def body(article):
        if article['article_id'] in first_analysis:
            return copy.deepcopy(replay[article['article_id']]['body'])
        diagnostics = {}
        try:
            text = bot.fetch_article_body(article['url'], diagnostics) or ''
        except Exception as error:
            text = ''
            diagnostics.update(code='body_fetch_failed', error=error)
        truncated = diagnostics.pop('truncated', False) or len(text) > 12000
        if diagnostics:
            with lock:
                body_errors.append((article['article_id'], diagnostics))
        status, quality_reason = body_quality(text[:12000])
        # Preserve failed extraction text as evidence; never promote menus to verified.
        return {'article_id': article['article_id'], 'body': text[:12000],
                'body_status': status, 'body_quality_reason': quality_reason,
                'source_url': article['url'], 'truncated': truncated}
    checkpoint('body')
    with ThreadPoolExecutor(max_workers=6) as pool:
        manifest = list(pool.map(body, items))
    diagnostics_batch = []
    for aid, diagnostics in body_errors:
        detail = dict(diagnostics)
        error = detail.pop('error', None)
        code = detail.pop('code')
        diagnostics_batch.append({'code': code, 'error': error or RuntimeError(code),
                                  'facts': {'article_id': aid, **detail}})
    record_errors('body', diagnostics_batch)
    # A fetch is not a new analysis: only changed, usable evidence can reopen a hold.
    unchanged = {b['article_id'] for b in manifest if b['article_id'] in previous_sources and b['article_id'] not in first_analysis
                 and (b['body_status'] != 'verified' or
                      review_source({}, b)['body_hash'] == previous_sources[b['article_id']]['body_hash'])}
    items = [a for a in items if a['article_id'] not in unchanged]
    raw = [a for a in raw if a['article_id'] not in unchanged]
    manifest = [b for b in manifest if b['article_id'] not in unchanged]
    if reviews_only and not items:
        print(json.dumps({'job_id': 'pending-review', 'status': 'no_change', 'inputs': 0, 'paid_ai_calls': 0}))
        return
    # The live main can finish a prior run while collection is in progress.
    # Refresh delivery history; keep the exposure used to collect/filter this job.
    data, _ = queue.source('seen_news.json')
    Path('seen_news.json').write_bytes(data)
    current_seen = bot.load_seen_urls()
    suppressed_ids = {b['article_id'] for b in manifest if not verification and suppressed_versions
                      and source_version(b) in suppressed_versions}
    raw = [a for a in raw if a['article_id'] not in suppressed_ids
           and (a['url'] not in current_seen or a['article_id'] in replay)]
    items = [a for a in items if a['article_id'] not in suppressed_ids
             and (a['url'] not in current_seen or a['article_id'] in replay)]
    allowed_ids = {a['article_id'] for a in items}
    manifest = [b for b in manifest if b['article_id'] in allowed_ids]
    # Put readable sources first without dropping IDs or changing their verdicts.
    body_ready = {b['article_id'] for b in manifest if b['body_status'] == 'verified'}
    items.sort(key=lambda a: a['article_id'] not in body_ready)
    manifest.sort(key=lambda b: b['article_id'] not in body_ready)
    created = now()
    jid = ('risk-verification-' if verification else 'risk-production-')+os.environ.get('GITHUB_RUN_ID', created.strftime('%Y%m%dT%H%M%S'))
    if not JOB_ID.fullmatch(jid) or queue.get('jobs/'+jid+'.json')[0]:
        raise RuntimeError('회차 ID·중복 오류')
    inputs = {'items': items, 'raw_articles': raw, 'history_hash': file_hash('seen_news.json'),
              'exposure_hash': file_hash('exposure_data.csv'), 'snapshot_mode': 'review_replay' if reviews_only else 'natural_collection'}
    rules = policy()
    job = {'schema_version': 1, 'bot_id': 'risk_monitor', 'job_id': jid,
           'mode': 'verification' if verification else 'production', 'status': 'pending',
           'created_at': created.isoformat(), 'expires_at': (created+timedelta(minutes=config['job_ttl_minutes'])).isoformat(),
           'input': inputs, 'input_hash': digest(inputs), 'body_manifest': manifest,
           'body_manifest_hash': digest(manifest), 'policy_version': 'risk-subscription-v1',
           'policy_hash': digest(rules), 'receipts': {}, 'paid_ai_calls': 0,
           'body_quality_version': 1}
    job['collection_timing'] = {'started_at': collection_started_at.isoformat(),
                                'input_ready_at': created.isoformat(),
                                'input_ready_elapsed_seconds': (created-collection_started_at).total_seconds()}
    if rules.get('exclusion_review_version') in (1, 2):
        job['exclusion_review_version'] = rules['exclusion_review_version']
    if rules.get('event_grouping_version') == 1:
        job['event_grouping_version'] = 1
    if rules.get('action_quality_version') in (1, 2):
        job['action_quality_version'] = rules['action_quality_version']
    if not verification:
        job['review_dispatch'] = {'version': 1, 'max_publication_age_hours': 12,
                                 'phase': 'post_delivery' if reviews_only else 'regular',
                                 'after_job_id': after_job_id,
                                 'pending_total': len(catalog),
                                 'eligible_count': sum(v == 'eligible' for v in ages.values()),
                                 'expired_count': sum(v == 'expired' for v in ages.values()),
                                 'date_unconfirmed_count': sum(v == 'date_unconfirmed' for v in ages.values())}
        job['review_pass_version'] = rules.get('review_pass_version', 1)
        if rules.get('delivery_focus_version') in (1, 2):
            job['delivery_focus_version'] = rules['delivery_focus_version']
        if rules.get('review_evidence_version') == 1:
            job['review_evidence_version'] = 1
        if rules.get('broker_security_version') == 1:
            job['broker_security_version'] = 1
        if rules.get('caution_care_version') == 1:
            job['caution_care_version'] = 1
        job['report_layout_version'] = 1
        job['smtp_evidence_version'] = 1
        current_config(queue)
    frozen = freeze_exposure(job, exposure_raw, captured_at)
    if job['exposure_snapshot']['sha256'] != job['input']['exposure_hash']:
        raise RuntimeError('수집 중 잔고 변경 — 스냅샷 불일치')
    small, values = split_job(job)
    if not verification and not reviews_only:
        try:
            reused = reusable_exclusions(queue, index, job)
            refs = []
            for i, part in enumerate(chunks(reused)):
                path = 'inputs/'+jid+'/exclusion-reuse-'+str(i).zfill(3)+'.json'
                values[path] = part
                refs.append({'path': path, 'sha256': digest(part), 'count': len(part)})
            small['exclusion_reuse'] = refs
        except Exception as error:
            # Cache absence/failure never blocks fresh analysis or delivery.
            record_error('collect', error, 'exclusion_reuse_unavailable')
    values.update(frozen)
    index['jobs'] = (index['jobs']+[jid])[-60:]
    values['jobs/'+jid+'.json'] = small
    values['index.json'] = index
    queue.put_many(values, {'jobs/'+jid+'.json': None, 'index.json': sha})
    checkpoint('collected')
    print(json.dumps({'job_id': jid, 'status': 'pending', 'inputs': len(items), 'paid_ai_calls': 0}))


def verify_job(queue, jid, self_test=False):
    checkpoint('verify')
    if not JOB_ID.fullmatch(jid):
        raise RuntimeError('회차 ID 오류')
    config = current_config(queue, self_test)
    if not self_test and os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise RuntimeError('운영 SMTP 소비는 main만 허용')
    job, sha = queue.get('jobs/'+jid+'.json')
    if not job or job.get('status') != 'pending' or job.get('receipts') or job.get('attempted_count', 0):
        raise RuntimeError('발송 시작·종결 회차 재처리 금지')
    if job.get('mode') != ('verification' if self_test else 'production') or utc_time(job['expires_at']) <= now():
        raise RuntimeError('회차 모드·만료 오류')
    job = hydrate(queue, job)
    result, _ = queue.get('results/'+jid+'.json')
    if not result:
        raise RuntimeError('구독 분석 결과 없음')
    info = validate(result, job, job['input'], job['body_manifest'])
    if job.get('review_evidence_version') == 1:
        from risk_review_session import verify_ready
        job['_review_leases'] = verify_ready(queue, job, result)
    if 'held_articles' in info:
        job['_validation_holds'] = info['held_articles']
        job['_validation_omissions'] = info['omitted_fields']
        job['_unprocessed_ids'] = info.get('unprocessed_ids', [])
        job['_validation_warnings'] = info.get('warnings', {})
    if job.get('continuity_version') == 1 and policy().get('continuity', {}).get('version') != 1:
        raise RuntimeError('연속 운영 정책 미승인 — 발송 보류')
    if not info['consume_ready'] and (self_test or job.get('continuity_version') != 1):
        raise RuntimeError('본문·귀속 확인 보류 존재 — 정상 발송 금지')
    if digest(job['input']) != job['input_hash'] or digest(job['body_manifest']) != job['body_manifest_hash']:
        raise RuntimeError('원본 입력·본문 manifest 변경')
    if job['policy_hash'] != digest(policy()):
        raise RuntimeError('현재 정책 불일치')
    snapshot = read_exposure(queue, job, config)
    if job.get('continuity_version') == 1 and snapshot is None:
        raise RuntimeError('구독 연속 운영 회차 잔고 스냅샷 누락')
    checks = [('history_hash', 'seen_news.json')]
    if snapshot is None:
        checks.append(('exposure_hash', 'exposure_data.csv'))
    for key, path in checks:
        remote, _ = queue.source(path)
        if hashlib.sha256(remote).hexdigest() != job['input'][key] or file_hash(path) != job['input'][key]:
            raise RuntimeError('운영 이력·잔고 변경 — 기존 자료 발송 금지')
    return config, job, sha, result


def consume(queue, jid, self_test=False):
    if not JOB_ID.fullmatch(jid):
        raise RuntimeError('회차 ID 오류')
    # A merged request file can retrigger an already finished owner verification.
    # Terminal receipts are evidence of completion, never a reason to resend.
    current_config(queue, self_test)
    existing, _ = queue.get('jobs/'+jid+'.json')
    if not self_test and existing and existing.get('status') == 'delivery_rejected':
        return recover(queue, jid)
    if existing and existing.get('status') in {'verified', 'completed'}:
        expected_mode = 'verification' if self_test else 'production'
        if existing.get('mode') != expected_mode:
            raise RuntimeError('종결 회차 모드 불일치')
        if not self_test:
            notify_reviews(queue, existing)
        print(json.dumps({'job_id': jid, 'status': existing['status'], 'noop': True,
                          'smtp_calls': 0, 'paid_ai_calls': 0}))
        return
    if (not self_test and existing and existing.get('status') == 'pending'
            and not existing.get('receipts') and not existing.get('attempted_count', 0)
            and utc_time(existing['expires_at']) > now()
            and queue.get('outbox/'+jid+'.json')[0] is not None):
        return recover(queue, jid)
    config, job, sha, result = verify_job(queue, jid, self_test)
    bot = import_bot()
    if self_test:
        from risk_owner_verification import OWNER
        bot.EMAIL_RECEIVERS, bot.EMAIL_CC = [], []
        bot.NO_RESULT_RECEIVER = OWNER
        bot.FORCE_SELF_ONLY = True
        previous, _ = queue.get('outbox/'+jid+'.json')
        if previous is not None:
            guard = Delivery(queue, jid, smtplib.SMTP_SSL, lambda: current_config(queue, True))
            validate_packet(previous, job, result)
            def restart(j):
                if j.get('status') != 'pending' or j.get('receipts') or j.get('attempted_count', 0):
                    raise RuntimeError('본인 검수 발송 시작 후 재처리 금지')
                j['status'] = 'sending'
            guard.change(restart)
            try:
                return finish_packet(queue, job, result, previous, bot, guard, bot.send_email)
            except Exception as error:
                record_error('smtp_data', error)
                try:
                    guard.change(lambda j: j.update(status='uncertain' if j.get('receipts') else 'pending'))
                except Exception as cleanup:
                    record_error('cleanup', cleanup, 'cleanup_failed')
                raise
    excluded_ids = set(config.get('excluded_article_ids', []))
    view, answers = delivery_view(job, result, excluded_ids)
    provider = Provider(view, answers)
    original_seen = Path('seen_news.json').read_bytes()
    original_exposure = file_hash('exposure_data.csv')
    metadata, current_sha = queue.get('jobs/'+jid+'.json')
    if current_sha != sha:
        raise RuntimeError('소비 검증 중 회차 상태 변경')
    verdicts = delivery_verdicts(job, result, excluded_ids)
    deferred_ids = [aid for aid, value in verdicts.items() if value is None]
    bodies = {b['article_id']: b for b in job['body_manifest']}
    body_deferred_ids = {aid for aid in deferred_ids
                         if bodies[aid]['body_status'] in {'missing', 'unreadable'}
                         or (isinstance(result['analyses'].get(aid), dict) and
                             result['analyses'][aid].get('body_status') in {'missing', 'unreadable'})}
    body_deferred_ids.update(row['article_id'] for row in result['classifications']
                             if isinstance(row.get('recheck'), dict) and row['recheck'].get('blocker') == 'body_unusable')
    if job.get('review_evidence_version') == 1:
        body_deferred_ids.update(row['article_id'] for row in result['classifications']
                                 if row.get('decision_basis', {}).get('kind') == 'unavailable')
    unprocessed_ids = set(job.get('_unprocessed_ids', []))
    alert_deferred_ids = [aid for aid in deferred_ids if aid not in body_deferred_ids | unprocessed_ids]
    reasons = {row['article_id']: row.get('reason') if isinstance(row.get('reason'), str) else '분류 사유 형식 경고'
               for row in result['classifications']}
    reasons.update(job.get('_validation_holds', {}))
    reasons.update(dict.fromkeys(job.get('_unprocessed_ids', []), '최초 분석 미처리: 검증 기사 우선 안내 후 이어서 처리'))
    values, expected, resolutions = ({}, {}, {}) if self_test else prepare_reviews(queue, job, verdicts, reasons=reasons)
    overdue_count = sum(utc_time(values['review/'+aid+'.json']['first_seen_at']) <= now()-timedelta(hours=24)
                        for aid in deferred_ids if 'review/'+aid+'.json' in values)
    alert_overdue_count = sum(utc_time(values['review/'+aid+'.json']['first_seen_at']) <= now()-timedelta(hours=24)
                              for aid in alert_deferred_ids if 'review/'+aid+'.json' in values)
    metadata.update(status='sending', self_test=self_test, sending_at=now().isoformat())
    metadata.update(deferred_ids=deferred_ids, review_resolutions=resolutions,
                    analysis_complete=not deferred_ids, overdue_review_count=overdue_count)
    metadata['body_deferred_ids'] = sorted(body_deferred_ids)
    if job.get('review_pass_version') == 2:
        metadata['validation_holds'] = job['_validation_holds']
        metadata['omitted_fields'] = job['_validation_omissions']
    if job.get('delivery_focus_version') in (1, 2):
        metadata['unprocessed_ids'] = job.get('_unprocessed_ids', [])
        metadata['validation_warnings'] = job.get('_validation_warnings', {})
    metadata['owner_excluded_ids'] = sorted(excluded_ids.intersection(verdicts))
    if job.get('_review_leases'):
        expected.update(job['_review_leases'])
        metadata['review_receipt'] = {'attempt_id': result['review_attempt_id'],
                                      'result_hash': digest(result)}
        metadata['first_pass_complete'] = not unprocessed_ids
        metadata['first_pass_count'] = len(result['classifications'])
    if values or resolutions or job.get('_review_leases'):
        values['jobs/'+jid+'.json'], expected['jobs/'+jid+'.json'] = metadata, sha
        queue.put_many(values, expected)
    else:
        queue.put('jobs/'+jid+'.json', metadata, sha)
    guard = Delivery(queue, jid, smtplib.SMTP_SSL,
                     lambda: current_config(queue, self_test))
    staged = {}
    diagnostics = {}
    native_send, native_empty, native_save = bot.send_email, bot.send_email_no_result, bot.save_seen_urls
    def artifact(path, value):
        if not self_test and job.get('delivery_focus_version') == 2:
            diagnostics[path] = value
            return
        # A zero-SMTP staging failure may have left diagnostics from that attempt.
        # Only diagnostics are replaceable; saved results/outbox remain immutable.
        previous, old_sha = queue.get(path)
        if previous != value:
            queue.put(path, value, old_sha)
    def review_notice(subject, document, empty=False):
        if not alert_deferred_ids and not unprocessed_ids:
            if empty and body_deferred_ids:
                subject = subject.replace('신규 뉴스 없음', '본문 확인된 발송 후보 없음').replace('해당 뉴스 없음', '본문 확인된 발송 후보 없음')
                document = document.replace('AI 리스크 탐지 결과<br>해당하는 뉴스가 없습니다.',
                                            '본문 확인된 기사에서<br>발송 조건에 해당하는 뉴스가 없습니다.')
            return subject, document
        label = '확인 보류 '+str(len(alert_deferred_ids))+'건'
        if unprocessed_ids:
            label += ' · 최초 분석 미처리 '+str(len(unprocessed_ids))+'건'
        if alert_overdue_count:
            label += ' · 24시간 이상 미확인 '+str(alert_overdue_count)+'건'
        banner = '<div data-risk-owner-status="review-hold" style="padding:12px;color:#92400e;background:#fffbeb">'+html.escape(label)+' — 개별 확인 보류는 후속 처리하며, 같은 근거의 반복 분석은 하지 않습니다.</div>'
        document = re.sub(r'(<body\b[^>]*>)', lambda m: m[1]+banner, document, count=1)
        document = document.replace('금일 리스크 뉴스 없음', '검증 완료 발송 후보 없음')
        if empty:
            subject = '❗ [리스크 탐지] '+label+' · 검증 완료 후보 없음'
            document = document.replace('AI 리스크 탐지 결과<br>해당하는 뉴스가 없습니다.',
                                        '확인 보류 기사가 있습니다.<br>검증을 마친 발송 후보는 없습니다.')
        else:
            subject += ' · '+label
        return subject, document
    def send(subject, html, self_only=False):
        if self_only and job.get('delivery_focus_version') == 2:
            subject, html = review_notice(subject, html)
        if deferred_ids:
            html = html.replace("금일 리스크 뉴스 없음 — ", "")
        if self_test:
            targets, self_only = [bot.NO_RESULT_RECEIVER], True
            subject = '[TEST] '+subject
        else:
            targets = [bot.NO_RESULT_RECEIVER or bot.EMAIL_SENDER] if self_only else bot.report_targets()
        if staged:
            raise RuntimeError('동일 회차 추가 메일 금지')
        route = 'configured' if self_only else 'configured_no_sender_copy'
        if not self_only and job.get('report_layout_version') == 1:
            to, cc = report_headers(targets, config.get('report_recipient_keys'))
            targets, route = to + cc, 'executives_to_groups_cc'
        staged.update(subject=subject, html=html, targets=targets, self_only=self_only, recipient_route=route)
        return True  # Stage native post-delivery history, without SMTP or main writes.
    def empty(subject, html):
        if job.get('_unprocessed_ids'):
            raise RuntimeError('적격 발송 기사 없음 — 미처리 분석을 계속해야 함')
        from risk_owner_verification import OWNER, ERROR_EMAIL
        # Operational status only; normal/reference reports retain configured recipients.
        bot.NO_RESULT_RECEIVER = OWNER if self_test else ERROR_EMAIL
        subject, html = review_notice(subject, html, empty=True)
        if staged:
            raise RuntimeError('동일 회차 추가 메일 금지')
        staged.update(subject=('[TEST] ' if self_test else '')+subject, html=html,
                      targets=[bot.NO_RESULT_RECEIVER or bot.EMAIL_SENDER], self_only=True, recipient_route='owner_status')
    def save(*args, **kwargs):
        if args:
            held_urls = {a['url'] for a in job['input']['items'] if a['article_id'] in deferred_ids}
            args = (set(args[0])-held_urls,)+args[1:]
        return native_save(*args, **kwargs)
    def private_stats(*args, **kwargs):
        artifact('metrics/'+jid+'.json', {'stats': copy.deepcopy(bot._RUN_STATS), 'paid_ai_calls': 0,
                  'provider_route_calls': copy.deepcopy(provider.calls),
                  'subscription_usage': {'measurement_source': 'not_exposed',
                      'model_call_count': None, 'input_tokens': None, 'output_tokens': None,
                      'cache_read_input_tokens': None, 'cache_creation_input_tokens': None,
                      'cost': None},
                  'summary_source_ids': list(provider.summary_source_ids)})
    def private_log(*args):
        # Exact final/source objects stay private; the native logger writes no public file.
        artifact('filter-logs/'+jid+'.json', {'raw': args[0], 'hard_excluded': args[1],
                  'ai_selected': args[2], 'final': args[3], 'paid_ai_calls': 0})
    def no_paid_post(*args, **kwargs):
        raise RuntimeError('유료 HTTP inference POST 차단')
    try:
        checkpoint('render')
        provider.install(bot)
        with contextlib.ExitStack() as stack:
            if job['input'].get('snapshot_mode') == 'review_replay':
                # Review only the saved articles; market/notice digests belong to natural runs.
                stack.enter_context(patch.object(bot, 'build_price_alert_section', lambda *a, **k: ''))
                stack.enter_context(patch.object(bot, 'load_competitor_notices', return_value=[]))
            frozen = read_exposure(queue, job, config)
            if frozen is not None:
                directory = stack.enter_context(tempfile.TemporaryDirectory())
                snapshot_path = Path(directory)/'exposure_data.csv'
                snapshot_path.write_bytes(frozen)
                stack.enter_context(patch.object(bot, 'EXPOSURE_FILE', str(snapshot_path)))
            temporary = stack.enter_context(tempfile.TemporaryDirectory())
            seen_path = Path(temporary)/'seen_news.json'
            seen_path.write_bytes(original_seen)
            stack.enter_context(patch.object(bot, 'SEEN_FILE', str(seen_path)))
            stack.enter_context(patch.object(bot, 'send_email', send))
            stack.enter_context(patch.object(bot, 'send_email_no_result', empty))
            stack.enter_context(patch.object(bot, 'save_seen_urls', save))
            stack.enter_context(patch.object(bot, 'save_run_stats', private_stats))
            stack.enter_context(patch.object(bot, 'save_filter_log', private_log))
            stack.enter_context(patch.object(bot.smtplib, 'SMTP_SSL', side_effect=RuntimeError('발송 계획 저장 전 SMTP 금지')))
            stack.enter_context(patch.object(bot.requests, 'post', no_paid_post))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            bot.main()
            prepared_seen = original_seen.decode() if self_test else seen_path.read_text()
        if not staged:
            raise RuntimeError('발송 계획 없음 — 정상 완료 금지')
        packet = make_packet(job, result, staged['subject'], staged['html'], staged['targets'],
                             staged['self_only'], prepared_seen, staged.get('recipient_route', 'configured'))
        checkpoint('outbox')
        store_packet(queue, packet)
        artifact('previews/'+jid+'.json', {'html': staged['html'], 'self_test': self_test, 'paid_ai_calls': 0})
        artifact('logs/'+jid+'.json', {'log': output.getvalue(), 'paid_ai_calls': 0})
        if file_hash('exposure_data.csv') != original_exposure:
            raise RuntimeError('잔고 파일 변경')
        if self_test and Path('seen_news.json').read_bytes() != original_seen:
            raise RuntimeError('본인 검수 중 운영 이력 변경')
        finish_packet(queue, job, result, packet, bot, guard, native_send)
    except Exception as error:
        record_error(None, error)
        # An authenticated connection failure before any DATA is safely retryable.
        def failed(j):
            if j.get('status') not in {'completed', 'verified', 'delivery_complete', 'delivery_rejected', 'delivery_failed'}:
                j['status'] = 'uncertain' if j.get('receipts') or j.get('attempted_count', 0) else 'pending'
        try:
            guard.change(failed)
            if not self_test:
                close_rejected(queue, jid)
        except Exception as cleanup:
            record_error('cleanup', cleanup, 'cleanup_failed')
            # The cleanup PUT can itself conflict. Keep the original exception
            # for the owner notification instead of replacing the first failure.
            print('RISK_DELIVERY_CLEANUP outcome=failed')
        raise
    finally:
        bot.RISK_PROVIDER = None
        if diagnostics:
            from risk_diagnostics import finish
            finish(queue, jid, diagnostics)
        # SMTP guard has been restored: diagnostics must not become delivery receipts.
        primary = sys.exception()
        try:
            bot.flush_owner_warnings()
        except Exception as error:
            record_error('owner_notification', error, 'owner_notification_failed')
            if primary is None:
                raise


def finish_packet(queue, job, result, packet, bot, guard, native_send):
    """SMTP uses only a persisted plan; history is published after all receipts."""
    validate_packet(packet, job, result)
    self_test = packet['self_test']
    grouped = packet.get('recipient_route') == 'executives_to_groups_cc'
    modern_owner = job.get('smtp_evidence_version') == 1 and packet['self_only'] and len(packet['expected_receipts']) == 1
    def confirm_config():
        config = current_config(queue, self_test, job['input']['items'], packet['html'])
        if grouped:
            to, cc = report_headers(bot.report_targets(), config.get('report_recipient_keys'))
            if [address_key(a) for a in to + cc] != packet['expected_receipts']:
                raise RuntimeError('고정본 작성 이후 수신자 역할 변경 — 발송 금지')
            return to, cc
    guard.check_config = confirm_config
    headers = confirm_config()
    before, _ = queue.source('seen_news.json')
    if hashlib.sha256(before).hexdigest() not in {packet['source_history_hash'], packet['seen_after_hash']}:
        raise RuntimeError('발송 계획 이후 main 이력 변경 — 자동 합치기 금지')
    if packet.get('recipient_route') == 'owner_status':
        from risk_owner_verification import OWNER, ERROR_EMAIL
        bot.NO_RESULT_RECEIVER = OWNER if self_test else ERROR_EMAIL
    # Older immutable packets keep their original recipients and receipt contract.
    include_sender_copy = packet.get('recipient_route', 'configured') == 'configured'
    targets = ([bot.NO_RESULT_RECEIVER or bot.EMAIL_SENDER] if packet['self_only']
               else bot.report_targets(include_sender_copy=include_sender_copy))
    if grouped:
        to, cc = headers
        targets = to + cc
    if [address_key(t) for t in targets] != packet['expected_receipts']:
        raise RuntimeError('고정본 작성 이후 수신자 변경 — 발송 금지')
    checkpoint('smtp_prepare')
    guard.prepare(targets)
    current, _ = guard.read()
    if (set(current.get('receipts', {})) != set(packet['expected_receipts'])
            or any(v != 'accepted' for v in current.get('receipts', {}).values())):
        assert_report_content(packet)
        if grouped or modern_owner:
            message = bot._build_msg(packet['subject'], packet['html'], targets[0])
            if grouped:
                message.replace_header('To', ', '.join(to))
                message['Cc'] = ', '.join(cc)
            guard.send_report(bot.EMAIL_SENDER, bot.EMAIL_PASSWORD, message, packet['packet_hash'])
        else:
            with contextlib.redirect_stdout(io.StringIO()), patch.object(bot.smtplib, 'SMTP_SSL', guard.factory):
                native_send(packet['subject'], packet['html'], self_only=packet['self_only'],
                            include_sender_copy=include_sender_copy)
    guard.complete()
    if not self_test:
        checkpoint('history_local')
        Path('seen_news.json').write_text(packet['seen_after_snapshot'])
    def finish(j):
        expected = set(packet['expected_receipts'])
        if (j.get('status') != 'sending' or set(j.get('receipts', {})) != expected
                or any(v != 'accepted' for v in j['receipts'].values())):
            raise RuntimeError('전원 접수 상태 변경 — 이력 완료 금지')
        j.update(status='verified' if self_test else 'delivery_complete',
                 verified_at=now().isoformat() if self_test else None,
                 delivered_at=now().isoformat(), paid_ai_calls=0, self_test=self_test,
                 seen_after_hash=packet['seen_after_hash'], seen_after_snapshot=packet['seen_after_snapshot'],
                 outbox_hash=packet['packet_hash'], production_history_writes=0 if self_test else None)
    checkpoint('delivery_state')
    guard.change(finish)
    final = guard.complete()
    print(json.dumps({'job_id': job['job_id'], 'status': final['status'], 'paid_ai_calls': 0,
                      'self_test': self_test, 'analysis_complete': final.get('analysis_complete', True),
                      'deferred_count': len(final.get('deferred_ids', [])),
                      'body_deferred_count': len(final.get('body_deferred_ids', [])),
                      'smtp_accepted': len(final['receipts']), 'smtp_calls': guard.smtp_calls}))


def recover(queue, jid):
    """Explicit operator recovery; uncertain SMTP recipients remain blocked."""
    if not JOB_ID.fullmatch(jid) or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise RuntimeError('운영 복구는 유효한 회차·main만 허용')
    config = current_config(queue)
    small, original_sha = getattr(queue, 'get_fresh', queue.get)('jobs/'+jid+'.json')
    rejected = bool(small and small.get('status') == 'delivery_rejected')
    if rejected:
        if close_rejected(queue, jid):
            raise RuntimeError('SMTP 미접수 실패 종결 — 원문 이월 완료, 재발송 없음')
        if not retry_targets(small):
            raise RuntimeError('SMTP 거부 재시도 대기·상한·만료 또는 영구 거부')
    if small and small.get('status') == 'completed':
        notify_reviews(queue, small)
        print(json.dumps({'job_id': jid, 'status': 'completed', 'noop': True, 'smtp_calls': 0, 'paid_ai_calls': 0}))
        return
    if (not small or small.get('mode') != 'production'
            or small.get('status') not in {'pending', 'sending', 'uncertain', 'delivery_complete', 'delivery_rejected'}
            or (not small.get('expected_receipts') and small.get('status') != 'pending')
            or len(set(small.get('expected_receipts', []))) != len(small.get('expected_receipts', []))
            or not set(small.get('receipts', {})) <= set(small.get('expected_receipts', []))
            or not rejected and any(v != 'accepted' for v in small.get('receipts', {}).values())
            or small.get('attempted_count', 0) != len(small.get('receipts', {}))):
        raise RuntimeError('SMTP 시도·접수 불확실 또는 복구 대상 아님 — 재발송 금지')
    if now()-utc_time(small['created_at']) > timedelta(hours=24):
        raise RuntimeError('24시간 초과 운영 복구 보류 — 기사 적시성 재검토 필요')
    if not small.get('receipts') and utc_time(small['expires_at']) <= now():
        raise RuntimeError('미발송 만료 회차는 복구 발송 금지 — 원문 이월 필요')
    job = hydrate(queue, small)
    result, _ = queue.get('results/'+jid+'.json')
    if job.get('review_evidence_version') == 1 and job.get('review_receipt') != {
            'attempt_id': result.get('review_attempt_id'), 'result_hash': digest(result)}:
        raise RuntimeError('복구 분석 소유권 확정 증거 불일치')
    if digest(job['input']) != job['input_hash'] or digest(job['body_manifest']) != job['body_manifest_hash']:
        raise RuntimeError('복구 원본 입력·본문 해시 변경')
    info = validate(result, job, job['input'], job['body_manifest'])
    if 'held_articles' in info:
        job['_validation_holds'] = info['held_articles']
        job['_validation_omissions'] = info['omitted_fields']
        job['_unprocessed_ids'] = info.get('unprocessed_ids', [])
        job['_validation_warnings'] = info.get('warnings', {})
    if (not info['consume_ready'] and job.get('continuity_version') != 1
            or job.get('continuity_version') == 1 and policy().get('continuity', {}).get('version') != 1):
        raise RuntimeError('복구 연속 처리 정책 미승인')
    if digest(policy()) != job['policy_hash']:
        raise RuntimeError('복구 정책 변경 — 재분석 없이 발송 금지')
    frozen = read_exposure(queue, job, config)
    if job.get('continuity_version') == 1 and frozen is None:
        raise RuntimeError('복구 고정 잔고 없음')
    bot = import_bot()
    bot.RISK_PROVIDER = None
    targets = list(dict.fromkeys([bot.EMAIL_SENDER]+bot.EMAIL_RECEIVERS+bot.EMAIL_CC))
    packet, _ = queue.get('outbox/'+jid+'.json')
    if packet is None:
        packet = legacy_packet(queue, job, result, bot, targets)
        store_packet(queue, packet)
    validate_packet(packet, job, result)
    if rejected:
        if (packet.get('recipient_route') != 'executives_to_groups_cc'
                and not (job.get('smtp_evidence_version') == 1 and packet['self_only']
                         and len(packet['expected_receipts']) == 1)):
            raise RuntimeError('구형 SMTP 경로 거부 자동 복구 금지')
        rejection_evidence(small, packet['packet_hash'])
    before, _ = queue.source('seen_news.json')
    if hashlib.sha256(before).hexdigest() not in {packet['source_history_hash'], packet['seen_after_hash']}:
        raise RuntimeError('복구 전 main 이력 변경 — 자동 합치기 금지')
    latest, latest_sha = getattr(queue, 'get_fresh', queue.get)('jobs/'+jid+'.json')
    if latest_sha != original_sha or latest != small:
        raise RuntimeError('복구 검증 중 회차 변경 — 발송 금지')
    latest.update(status='sending', recovered_at=now().isoformat(), recovery_count=latest.get('recovery_count', 0)+1)
    latest.setdefault('expected_receipts', packet['expected_receipts'])
    queue.put('jobs/'+jid+'.json', latest, latest_sha)
    guard = Delivery(queue, jid, smtplib.SMTP_SSL, lambda: current_config(queue), resume=True)
    try:
        finish_packet(queue, job, result, packet, bot, guard, bot.send_email)
    except Exception as error:
        record_error(None, error)
        try:
            guard.change(lambda j: j.update(status='uncertain') if j.get('status') == 'sending' else None)
            close_rejected(queue, jid)
        except Exception as cleanup:
            record_error('cleanup', cleanup, 'cleanup_failed')
            print('RISK_DELIVERY_CLEANUP outcome=failed')
        raise


def acknowledge(queue, jid):
    checkpoint('ack_verify')
    job, sha = queue.get('jobs/'+jid+'.json')
    if not job or job.get('status') != 'delivery_complete':
        raise RuntimeError('전송 접수 전 ACK 금지')
    remote, _ = queue.source('seen_news.json')
    if hashlib.sha256(remote).hexdigest() != job['seen_after_hash']:
        raise RuntimeError('main 처리 이력 저장 확인 전 완료 금지')
    if set(job.get('receipts', {})) != set(job.get('expected_receipts', [])) or not job.get('receipts') or any(v != 'accepted' for v in job['receipts'].values()):
        raise RuntimeError('수신자 전원 accepted 없음')
    job.update(status='completed', completed_at=now().isoformat(), history_saved=True, ack=True)
    values, expected = finish_reviews(queue, job.get('review_resolutions', {}), jid)
    checkpoint('ack_commit')
    if values:
        values['jobs/'+jid+'.json'], expected['jobs/'+jid+'.json'] = job, sha
        queue.put_many(values, expected)
    else:
        queue.put('jobs/'+jid+'.json', job, sha)
    checkpoint('ack_complete')
    notify_reviews(queue, job)
    # Cleanup follows durable ACK and the notice that reads the review records.
    if job.get('mode') == 'production' and not job.get('self_test'):
        try:
            retention = prune_articles(queue, max_batches=1)
            print('RISK_ARTICLE_RETENTION '+json.dumps(retention))
        except Exception as error:
            record_error('cleanup', error, 'input_prune_failed')
    print(json.dumps({'job_id': jid, 'status': 'completed', 'paid_ai_calls': 0, 'history_saved': True,
                      'ack': True, 'analysis_complete': job.get('analysis_complete', True),
                      'deferred_count': len(job.get('deferred_ids', [])),
                      'body_deferred_count': len(job.get('body_deferred_ids', []))}))


def review_notice_rows(records, grouping):
    """Presentation only: explicit whole-incident reviews, bound to frozen sources.

    Never infer equivalence from headlines, dates, entities or shared markets.
    Missing/stale/ambiguous annotations fall back to individual article rows.
    Article reservations, pending records and all production state stay separate.
    """
    groups = grouping.get('groups', []) if isinstance(grouping, dict) and grouping.get('schema_version') == 1 else []
    if not isinstance(groups, list):
        groups = []
    memberships = {}
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get('members'), list):
            for member in group['members']:
                if isinstance(member, dict) and isinstance(member.get('article_id'), str):
                    aid = member['article_id']
                    memberships[aid] = memberships.get(aid, 0)+1
    assignment = {}
    for index, group in enumerate(groups):
        if (not isinstance(group, dict) or group.get('review_scope') != 'complete_risk_event_set'
                or not isinstance(group.get('label'), str) or not group['label'].strip()
                or not isinstance(group.get('event_id'), str) or not JOB_ID.fullmatch(group['event_id'])
                or not isinstance(group.get('members'), list) or len(group['members']) < 2):
            continue
        members = group['members']
        if any(not isinstance(m, dict) or not isinstance(m.get('article_id'), str)
               or memberships.get(m['article_id']) != 1 for m in members):
            continue  # Overlapping annotations cannot bridge independent incidents.
        for member in members:
            aid = member['article_id']
            if aid not in records:
                continue
            record, phase = records[aid]
            body = record['body']
            quotes = member.get('evidence_quotes')
            if (member.get('source_hash') != record['source_hash']
                    or body.get('body_status') != 'verified' or body.get('truncated')
                    or body.get('source_url') != record['item']['url']
                    or not isinstance(quotes, list) or not quotes
                    or any(not isinstance(q, str) or not q.strip() or q not in body['body'] for q in quotes)):
                continue
            assignment[aid] = (index, phase)
    buckets = {}
    for aid, (record, phase) in records.items():
        key = assignment.get(aid, ('article', aid))
        buckets.setdefault(key, []).append((record, phase))
    rows = []
    for key, members in buckets.items():
        lines = []
        if len(members) > 1:
            lines.append('[동일 사건 검토] '+groups[key[0]]['label']+' · 기사 '+str(len(members))+'건')
            record, phase = members[0]
            first_seen = min((r['first_seen_at'] for r, _ in members), key=utc_time)
            lines.extend(['대표 기사: '+record['item']['title'], record['item']['url'],
                          '관련 원문:\n'+'\n'.join(r['item']['url'] for r, _ in members[1:]),
                          '묶음 최초 확인: '+first_seen+
                          (' · 24시간 이상 미확인' if phase == 'overdue' else ' · 신규 확인 보류'),
                          '개별 제목·최초 확인 시각·보류 사유는 원본 보류 기록에 보존했습니다.'])
        else:
            record, phase = members[0]
            item = record['item']
            lines.append(item['title']+'\n'+item['url']+'\n최초 확인: '+record['first_seen_at']+
                         (' · 24시간 이상 미확인' if phase == 'overdue' else ' · 신규 확인 보류'))
            if record.get('last_review_reason'):
                lines.append('재확인 사유: ' + record['last_review_reason'])
        rows.append('\n'.join(lines))
    return rows


def notify_reviews(queue, job):
    """One owner-only notice per unresolved article/stage, after delivery ACK.

    Separate CAS reservations keep an uncertain diagnostic SMTP from being resent;
    they never alter production receipts, seen URLs, or review resolution hashes.
    """
    if job.get('status') != 'completed' or not job.get('ack') or not job.get('history_saved'):
        return
    ids = set(job.get('deferred_ids', []))-set(job.get('body_deferred_ids', []))-set(job.get('unprocessed_ids', []))
    values, expected, records = {}, {}, {}
    instant = now()
    for aid in sorted(ids):
        record, _ = queue.get('review/'+aid+'.json')
        if record and record.get('status') == 'resolved':
            continue
        record, _ = read_review(queue, aid)
        if record.get('last_body_status', record['body']['body_status']) in {'missing', 'unreadable'}:
            continue
        overdue = instant-utc_time(record['first_seen_at']) >= timedelta(hours=24)
        phase = 'overdue' if overdue else 'initial'
        key = digest({'article_id': aid, 'source_hash': record['source_hash']})
        path = 'notifications/review-'+key+'-'+phase+'.json'
        previous, sha = getattr(queue, 'get_fresh', queue.get)(path)
        if previous is not None:
            if previous.get('status') != 'accepted':
                print('운영 알림 접수 미확인 선기록 보존; 자동 재발송 금지/API0')
            continue
        values[path] = {'schema_version': 1, 'article_id': aid, 'phase': phase,
                        'job_id': job['job_id'], 'status': 'sending',
                        'attempt_id': uuid.uuid4().hex, 'attempted_at': instant.isoformat()}
        expected[path] = sha
        records[aid] = (record, phase)
    if not values:
        return
    grouping, _ = queue.get('review/notification-groups.json')
    rows = review_notice_rows(records, grouping)
    checkpoint('owner_notification')  # Production ACK is already durable.
    if not os.environ.get('EMAIL_PASSWORD'):
        raise RuntimeError('운영 알림 SMTP 설정 누락 — 발송 완료 상태 보존; Actions 확인 필요')
    commit = None
    try:
        commit = queue.put_many(values, expected)
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise RuntimeError('운영 알림 선기록 commit 미확인 — SMTP 금지')
    except Exception as error:
        record_error('owner_notification', error, 'review_reservation_unconfirmed',
                     reservation_commit=commit if isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit) else None)
        raise  # An ambiguous write never authorizes a retry or SMTP.
    for path, value in values.items():
        raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()
        wanted_sha = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        sha = None
        try:
            # Read the commit returned by the confirmed CAS, not a cached moving
            # main ref. Queue.get also verifies the returned blob's exact bytes.
            current, sha = queue.get(path, ref=commit)
            if current != value or sha != wanted_sha:
                raise RuntimeError('운영 알림 선기록 불일치 — SMTP 금지')
        except Exception as error:
            record_error('owner_notification', error, 'review_reservation_readback_failed',
                         reservation_commit=commit, read_ref=commit,
                         expected_blob_sha=wanted_sha, observed_blob_sha=sha,
                         notification_path=path)
            raise
        expected[path] = sha
    checkpoint('owner_notification', reservation_commit=commit, read_ref=commit,
               verified_blob_shas=dict(expected), notification_count=len(values))
    bot = import_bot()
    run = os.environ.get('GITHUB_RUN_ID', '')
    details = ('회차: '+job['job_id']+'\n발송·이력 저장·ACK 완료. 전체 기사 분석은 미완료입니다.\n'
               '근거가 부족한 기사는 보존했습니다. 발행 후 12시간 안에 새 본문·공식 근거가 확보될 때만 다시 확인합니다.\n\n'+
               '\n\n'.join(rows))
    if run.isdigit():
        details += '\nActions: https://github.com/jinhoo-choi/risk-news-crolling/actions/runs/'+run
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            summary = '기사 확인 필요 '+str(len(records))+'건'
            if len(rows) < len(records):
                summary += ' · 검토 묶음 '+str(len(rows))+'개'
            accepted = bot.send_email_error(summary, details, operational=True)
    except Exception:
        accepted = False
    for value in values.values():
        value.update(status='accepted' if accepted else 'uncertain', finished_at=now().isoformat())
    queue.put_many(values, expected)
    if not accepted:
        raise RuntimeError('운영 알림 SMTP 접수 미확인 — 원본 발송 완료 상태 보존; 알림 자동 재발송 금지')
    print('담당자 운영 알림 SMTP accepted 1건; 운영 영수증 변경0/API0')


def notify_error(error, context=''):
    """Owner error notification only; never change delivery receipts or history."""
    if not os.environ.get('EMAIL_PASSWORD'):
        print('담당자 오류 알림 생략 — SMTP 설정 없음; Actions 확인 필요')
        return False
    try:
        from risk_owner_verification import OWNER
        bot=import_bot()
        bot.NO_RESULT_RECEIVER=OWNER
        run=os.environ.get('GITHUB_RUN_ID','')
        if run.isdigit():
            context += '\nActions: https://github.com/jinhoo-choi/risk-news-crolling/actions/runs/'+run
        with contextlib.redirect_stdout(io.StringIO()):
            accepted=bot.send_email_error('리스크 구독 경로 보류: '+str(error),context)
        print('담당자 오류 알림 SMTP accepted 1건; 운영 영수증 변경0/API0' if accepted
              else '담당자 오류 알림 접수 미확인; Actions 확인 필요')
        return bool(accepted)
    except Exception as error:
        if not record_error('owner_notification', error, 'owner_notification_failed'):
            persist_unhandled(error, 'owner_notification', 'notification')
        print('담당자 오류 알림 실패; Actions 확인 필요')
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['scheduled', 'collect-test', 'collect-review', 'index-reviews', 'consume', 'recover', 'ack', 'connection', 'rollback-check', 'notify-error'])
    parser.add_argument('--job-id')
    parser.add_argument('--article-id', action='append', help='새 근거가 확인된 보류 기사만 명시적으로 지정')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    config = json.loads(Path(CONFIG).read_text())
    if args.operation == 'notify-error':
        notify_error(RuntimeError('발송 후 처리 이력 저장 또는 회차 ACK 실패'),
                     '발송 영수증은 보존했습니다. 접수 메일을 재발송하지 말고 private 회차와 main 이력을 확인하세요.')
        return
    if args.operation == 'scheduled' and config['engine'] == 'paused':
        print('리스크 구독 일시중지; 수집0/발송0/API0')
        return
    if args.operation == 'scheduled' and config['engine'] == 'api':
        raise RuntimeError('API 복귀는 별도 명시 경로로만 허용 — 자동 폴백 없음')
    queue = Queue()
    return run_audited(queue, args.operation, args.job_id or '', lambda: dispatch(args, config, queue))


def dispatch(args, config, queue):
    if args.operation in {'scheduled', 'collect-test', 'collect-review'}:
        collect(config, queue, args.operation == 'collect-test', args.operation == 'collect-review', getattr(args, 'article_id', None))
    elif args.operation == 'index-reviews':
        queue.preflight()
        current_config(queue)
        expire_and_check(queue)
        index, _ = review_catalog(queue, migrate=True)
        print(json.dumps({'active_reviews': len(index['pending']), 'paid_ai_calls': 0, 'smtp_calls': 0}))
    elif args.operation == 'consume':
        consume(queue, args.job_id or '', args.self_test)
    elif args.operation == 'recover':
        recover(queue, args.job_id or '')
    elif args.operation == 'ack':
        acknowledge(queue, args.job_id or '')
    elif args.operation == 'rollback-check':
        expire_and_check(queue)
        print('미확정 발송 없음; 자동 API 복귀 없음')
    else:
        from risk_queue import connection
        connection(queue)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        persist_unhandled(error)
        # Native logs/results/addresses are private. No raw error payload in Actions.
        print('리스크 구독 경로 보류:', type(error).__name__)
        import traceback
        if notify_error(error, traceback.format_exc()) and os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
                stream.write('error_notified=true\n')
        raise SystemExit(1)
