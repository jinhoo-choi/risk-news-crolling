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
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from risk_queue import Queue, JOB_ID, now, digest
from risk_result_validator import validate, utc_time
from risk_delivery import Delivery
from risk_provider import Provider
from risk_outbox import make_packet, validate_packet, store_packet, legacy_packet
from risk_shards import split_job, hydrate
from risk_audit import checkpoint, record_error, run_audited, observe_pending, persist_unhandled
from risk_continuity import (freeze_exposure, read_exposure, pending_reviews,
                             prepare_reviews, finish_reviews, delivery_view, delivery_verdicts, read_review)

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


def expire_and_check(queue):
    index, sha = queue.get('index.json')
    index = index or {'jobs': []}
    for jid in index['jobs']:
        job, old_sha = queue.get('jobs/'+jid+'.json')
        if not job:
            raise RuntimeError('색인 회차 읽기 실패')
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


def prune_terminal_inputs(queue, retention_days=7):
    """Remove only old terminal input shards; keep metadata/receipts and Git history."""
    if type(retention_days) is not int or retention_days < 1:
        raise ValueError('입력 보존기간은 1일 이상 정수')
    cutoff = now()-timedelta(days=retention_days)
    for entry in queue.list_input_entries():
        jid = entry['path']
        if entry['type'] != 'tree' or not JOB_ID.fullmatch(jid):
            raise RuntimeError('큐 입력 회차 디렉터리 오류')
        path = 'jobs/'+jid+'.json'
        job, sha = queue.get(path)
        if not job or job.get('job_id') != jid or job.get('status') not in TERMINAL:
            continue
        if any(v != 'accepted' for v in job.get('receipts', {}).values()):
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
        if not deletions:
            continue
        job.update(inputs_pruned_at=now().isoformat(), input_retention_days=retention_days,
                   inputs_pruned_files=len(deletions), inputs_pruned_bytes=sum(s.get('size', 0) for s in shards))
        queue.put_many({path: job}, expected, deletions=deletions)


def collect(config, queue, verification=False, reviews_only=False):
    checkpoint('collect')
    if reviews_only and verification:
        raise RuntimeError("보류 재수집은 운영 회차만 허용")
    queue.preflight()
    if not verification:
        current_config(queue)
    index, sha = expire_and_check(queue)
    if not verification:
        current_config(queue)
        try:
            prune_terminal_inputs(queue, config.get('input_retention_days', 7))
        except Exception as error:
            record_error('collect', error, 'input_prune_failed')
            notify_best_effort(RuntimeError('종결 회차 입력 정리 실패 — 자동 재시도 없이 수집 계속'),
                               '미종결 회차·원본 메타데이터·영수증은 정리 대상에서 제외합니다.')
    bot = import_bot()
    for state_file in ('seen_news.json', 'exposure_data.csv'):
        data, _ = queue.source(state_file)
        Path(state_file).write_bytes(data)
    exposure_raw = Path('exposure_data.csv').read_bytes()
    captured_at = now().isoformat()
    reviews = [] if verification else pending_reviews(queue)
    excluded = set(current_config(queue, verification).get('excluded_article_ids', []))
    reviews = [r for r in reviews if r['article_id'] not in excluded]
    if reviews_only and not reviews:
        raise RuntimeError('재검토할 영속 보류 없음')
    replay = {r['article_id']: r for r in reviews}
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
    raw = unique
    items = []
    for article in raw:
        article['article_id'] = hashlib.sha256((article['url']+'\0'+article['title']).encode()).hexdigest()[:24]
        article['description'] = article.get('desc', '')
        article['published_at'] = article.get('pubDate', '')
        if article['article_id'] in excluded:
            continue
        if not bot.is_hard_excluded(article['title'], article.get('desc', ''), article['url'])[0]:
            items.append(article)
    raw = [a for a in raw if a['article_id'] not in excluded]
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
        diagnostics = {}
        text = bot.fetch_article_body(article['url'], diagnostics) or ''
        if diagnostics:
            with lock:
                body_errors.append((article['article_id'], diagnostics))
        if not any(c.isalnum() for c in text) and article['article_id'] in replay:
            return copy.deepcopy(replay[article['article_id']]['body'])
        # Preserve bounded paragraphs, including entity context; no menus-as-body.
        return {'article_id': article['article_id'], 'body': text[:12000],
                'body_status': 'verified' if any(c.isalnum() for c in text) else 'missing',
                'source_url': article['url'], 'truncated': len(text) > 12000}
    checkpoint('body')
    with ThreadPoolExecutor(max_workers=6) as pool:
        manifest = list(pool.map(body, items))
    for aid, diagnostics in body_errors:
        detail = dict(diagnostics)
        error = detail.pop('error', None)
        code = detail.pop('code')
        record_error('body', error or RuntimeError(code), code, article_id=aid, **detail)
    # The live main can finish a prior run while collection is in progress.
    # Refresh delivery history; keep the exposure used to collect/filter this job.
    data, _ = queue.source('seen_news.json')
    Path('seen_news.json').write_bytes(data)
    current_seen = bot.load_seen_urls()
    raw = [a for a in raw if a['url'] not in current_seen or a['article_id'] in replay]
    items = [a for a in items if a['url'] not in current_seen or a['article_id'] in replay]
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
           'policy_hash': digest(rules), 'receipts': {}, 'paid_ai_calls': 0}
    if rules.get('event_grouping_version') == 1:
        job['event_grouping_version'] = 1
    if not verification:
        current_config(queue)
    frozen = freeze_exposure(job, exposure_raw, captured_at)
    if job['exposure_snapshot']['sha256'] != job['input']['exposure_hash']:
        raise RuntimeError('수집 중 잔고 변경 — 스냅샷 불일치')
    small, values = split_job(job)
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
                         or result['analyses'].get(aid, {}).get('body_status') in {'missing', 'unreadable'}}
    alert_deferred_ids = [aid for aid in deferred_ids if aid not in body_deferred_ids]
    values, expected, resolutions = ({}, {}, {}) if self_test else prepare_reviews(queue, job, verdicts)
    overdue_count = sum(utc_time(values['review/'+aid+'.json']['first_seen_at']) <= now()-timedelta(hours=24)
                        for aid in deferred_ids)
    alert_overdue_count = sum(utc_time(values['review/'+aid+'.json']['first_seen_at']) <= now()-timedelta(hours=24)
                              for aid in alert_deferred_ids)
    metadata.update(status='sending', self_test=self_test, sending_at=now().isoformat())
    metadata.update(deferred_ids=deferred_ids, review_resolutions=resolutions,
                    analysis_complete=not deferred_ids, overdue_review_count=overdue_count)
    metadata['body_deferred_ids'] = sorted(body_deferred_ids)
    metadata['owner_excluded_ids'] = sorted(excluded_ids.intersection(verdicts))
    if values or resolutions:
        values['jobs/'+jid+'.json'], expected['jobs/'+jid+'.json'] = metadata, sha
        queue.put_many(values, expected)
    else:
        queue.put('jobs/'+jid+'.json', metadata, sha)
    guard = Delivery(queue, jid, smtplib.SMTP_SSL,
                     lambda: current_config(queue, self_test))
    staged = {}
    native_send, native_empty, native_save = bot.send_email, bot.send_email_no_result, bot.save_seen_urls
    def artifact(path, value):
        # A zero-SMTP staging failure may have left diagnostics from that attempt.
        # Only diagnostics are replaceable; saved results/outbox remain immutable.
        previous, old_sha = queue.get(path)
        if previous != value:
            queue.put(path, value, old_sha)
    def review_notice(subject, document, empty=False):
        if not alert_deferred_ids:
            if empty and body_deferred_ids:
                subject = subject.replace('신규 뉴스 없음', '본문 확인된 발송 후보 없음').replace('해당 뉴스 없음', '본문 확인된 발송 후보 없음')
                document = document.replace('AI 리스크 탐지 결과<br>해당하는 뉴스가 없습니다.',
                                            '본문 확인된 기사에서<br>발송 조건에 해당하는 뉴스가 없습니다.')
            return subject, document
        label = '확인 보류 '+str(len(alert_deferred_ids))+'건'
        if alert_overdue_count:
            label += ' · 24시간 이상 미확인 '+str(alert_overdue_count)+'건'
        banner = '<div style="padding:12px;color:#92400e;background:#fffbeb">'+html.escape(label)+' — 판정 미완료 기사는 발송·정상 처리에서 제외하고 다음 회차에 다시 확인합니다.</div>'
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
        if deferred_ids:
            html = html.replace("금일 리스크 뉴스 없음 — ", "")
        targets = [bot.NO_RESULT_RECEIVER or bot.EMAIL_SENDER] if self_only else list(dict.fromkeys([bot.EMAIL_SENDER]+bot.EMAIL_RECEIVERS+bot.EMAIL_CC))
        if self_test:
            targets, self_only = [bot.NO_RESULT_RECEIVER], True
            subject = '[TEST] '+subject
        if staged:
            raise RuntimeError('동일 회차 추가 메일 금지')
        staged.update(subject=subject, html=html, targets=targets, self_only=self_only)
        return True  # Stage native post-delivery history, without SMTP or main writes.
    def empty(subject, html):
        from risk_owner_verification import OWNER
        bot.NO_RESULT_RECEIVER = OWNER
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
            if j.get('status') not in {'completed', 'verified', 'delivery_complete'}:
                j['status'] = 'uncertain' if j.get('receipts') or j.get('attempted_count', 0) else 'pending'
        try:
            guard.change(failed)
        except Exception as cleanup:
            record_error('cleanup', cleanup, 'cleanup_failed')
            # The cleanup PUT can itself conflict. Keep the original exception
            # for the owner notification instead of replacing the first failure.
            print('RISK_DELIVERY_CLEANUP outcome=failed')
        raise
    finally:
        bot.RISK_PROVIDER = None
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
    guard.check_config = lambda: current_config(queue, self_test, job['input']['items'], packet['html'])
    guard.check_config()
    before, _ = queue.source('seen_news.json')
    if hashlib.sha256(before).hexdigest() not in {packet['source_history_hash'], packet['seen_after_hash']}:
        raise RuntimeError('발송 계획 이후 main 이력 변경 — 자동 합치기 금지')
    if packet.get('recipient_route') == 'owner_status':
        from risk_owner_verification import OWNER
        bot.NO_RESULT_RECEIVER = OWNER
    targets = ([bot.NO_RESULT_RECEIVER or bot.EMAIL_SENDER] if packet['self_only']
               else list(dict.fromkeys([bot.EMAIL_SENDER]+bot.EMAIL_RECEIVERS+bot.EMAIL_CC)))
    from risk_delivery import address_key
    if [address_key(t) for t in targets] != packet['expected_receipts']:
        raise RuntimeError('고정본 작성 이후 수신자 변경 — 발송 금지')
    checkpoint('smtp_prepare')
    guard.prepare(targets)
    current, _ = guard.read()
    if set(current.get('receipts', {})) != set(packet['expected_receipts']):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(bot.smtplib, 'SMTP_SSL', guard.factory):
            native_send(packet['subject'], packet['html'], self_only=packet['self_only'])
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
    if small and small.get('status') == 'completed':
        notify_reviews(queue, small)
        print(json.dumps({'job_id': jid, 'status': 'completed', 'noop': True, 'smtp_calls': 0, 'paid_ai_calls': 0}))
        return
    if (not small or small.get('mode') != 'production'
            or small.get('status') not in {'pending', 'sending', 'uncertain', 'delivery_complete'}
            or (not small.get('expected_receipts') and small.get('status') != 'pending')
            or len(set(small.get('expected_receipts', []))) != len(small.get('expected_receipts', []))
            or not set(small.get('receipts', {})) <= set(small.get('expected_receipts', []))
            or any(v != 'accepted' for v in small.get('receipts', {}).values())
            or small.get('attempted_count', 0) != len(small.get('receipts', {}))):
        raise RuntimeError('SMTP 시도·접수 불확실 또는 복구 대상 아님 — 재발송 금지')
    if now()-utc_time(small['created_at']) > timedelta(hours=24):
        raise RuntimeError('24시간 초과 운영 복구 보류 — 기사 적시성 재검토 필요')
    if not small.get('receipts') and utc_time(small['expires_at']) <= now():
        raise RuntimeError('미발송 만료 회차는 복구 발송 금지 — 원문 이월 필요')
    job = hydrate(queue, small)
    result, _ = queue.get('results/'+jid+'.json')
    if digest(job['input']) != job['input_hash'] or digest(job['body_manifest']) != job['body_manifest_hash']:
        raise RuntimeError('복구 원본 입력·본문 해시 변경')
    info = validate(result, job, job['input'], job['body_manifest'])
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
    print(json.dumps({'job_id': jid, 'status': 'completed', 'paid_ai_calls': 0, 'history_saved': True,
                      'ack': True, 'analysis_complete': job.get('analysis_complete', True),
                      'deferred_count': len(job.get('deferred_ids', [])),
                      'body_deferred_count': len(job.get('body_deferred_ids', []))}))


def notify_reviews(queue, job):
    """One owner-only notice per unresolved article/stage, after delivery ACK.

    Separate CAS reservations keep an uncertain diagnostic SMTP from being resent;
    they never alter production receipts, seen URLs, or review resolution hashes.
    """
    if job.get('status') != 'completed' or not job.get('ack') or not job.get('history_saved'):
        return
    ids = set(job.get('deferred_ids', []))-set(job.get('body_deferred_ids', []))
    values, expected, rows = {}, {}, []
    instant = now()
    for aid in sorted(ids):
        record, _ = queue.get('review/'+aid+'.json')
        if record and record.get('status') == 'resolved':
            continue
        record, _ = read_review(queue, aid)
        if record['body']['body_status'] in {'missing', 'unreadable'}:
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
        item = record['item']
        rows.append(item['title']+'\n'+item['url']+'\n최초 확인: '+record['first_seen_at']+
                    (' · 24시간 이상 미확인' if overdue else ' · 신규 확인 보류'))
    if not values:
        return
    if not os.environ.get('EMAIL_PASSWORD'):
        raise RuntimeError('운영 알림 SMTP 설정 누락 — 발송 완료 상태 보존; Actions 확인 필요')
    queue.put_many(values, expected)  # No SMTP unless the unique reservation is confirmed.
    for path, value in values.items():
        current, sha = getattr(queue, 'get_fresh', queue.get)(path)
        if current != value or not sha:
            raise RuntimeError('운영 알림 선기록 불일치 — SMTP 금지')
        expected[path] = sha
    bot = import_bot()
    run = os.environ.get('GITHUB_RUN_ID', '')
    details = ('회차: '+job['job_id']+'\n발송·이력 저장·ACK 완료. 전체 기사 분석은 미완료입니다.\n'
               '아래 기사는 발송·seen에서 제외한 채 보존했습니다. 원문·공시를 확인하고 판정을 제출해 주세요.\n\n'+
               '\n\n'.join(rows))
    if run.isdigit():
        details += '\nActions: https://github.com/jinhoo-choi/risk-news-crolling/actions/runs/'+run
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            accepted = bot.send_email_error('기사 확인 필요 '+str(len(rows))+'건', details, operational=True)
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
    parser.add_argument('operation', choices=['scheduled', 'collect-test', 'collect-review', 'consume', 'recover', 'ack', 'connection', 'rollback-check', 'notify-error'])
    parser.add_argument('--job-id')
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
        collect(config, queue, args.operation == 'collect-test', args.operation == 'collect-review')
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
