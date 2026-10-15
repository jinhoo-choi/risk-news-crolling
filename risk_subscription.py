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
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from risk_queue import Queue, JOB_ID, now, digest
from risk_result_validator import validate, utc_time
from risk_delivery import Delivery
from risk_provider import Provider
from risk_shards import split_job, hydrate
from risk_continuity import (freeze_exposure, read_exposure, pending_reviews,
                             prepare_reviews, finish_reviews, delivery_view)

CONFIG = 'risk_subscription_config.json'
POLICY = 'risk_policy.json'
TERMINAL = {'completed', 'verified', 'expired', 'cancelled'}


def notify_best_effort(error, context=''):
    try:
        notify_error(error, context)
    except Exception:
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


def current_config(queue, verification=False):
    raw, _ = queue.source(CONFIG)
    config = json.loads(raw)
    if config.get('engine') not in {'subscription', 'api', 'paused'}:
        raise RuntimeError('운영 엔진 오류')
    if not verification and config['engine'] != 'subscription':
        raise RuntimeError('구독 운영이 아님 — 발송 보류')
    if config.get('policy_version') != 'risk-subscription-v1':
        raise RuntimeError('분석 정책 버전 오류')
    return config


def expire_and_check(queue):
    index, sha = queue.get('index.json')
    index = index or {'jobs': []}
    for jid in index['jobs']:
        job, old_sha = queue.get('jobs/'+jid+'.json')
        if not job:
            raise RuntimeError('색인 회차 읽기 실패')
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
            if job.get('mode') == 'production':
                notify_best_effort(RuntimeError('운영 구독 회차 만료 — 기사 누락 가능; 수집 범위 확인 필요'),
                                   'job_id='+jid+'\ncreated_at='+str(job.get('created_at', 'unconfirmed')))
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


def collect(config, queue, verification=False):
    queue.preflight()
    if not verification:
        current_config(queue)
    index, sha = expire_and_check(queue)
    if not verification:
        current_config(queue)
        try:
            prune_terminal_inputs(queue, config.get('input_retention_days', 7))
        except Exception:
            notify_best_effort(RuntimeError('종결 회차 입력 정리 실패 — 자동 재시도 없이 수집 계속'),
                               '미종결 회차·원본 메타데이터·영수증은 정리 대상에서 제외합니다.')
    bot = import_bot()
    for state_file in ('seen_news.json', 'exposure_data.csv'):
        data, _ = queue.source(state_file)
        Path(state_file).write_bytes(data)
    exposure_raw = Path('exposure_data.csv').read_bytes()
    captured_at = now().isoformat()
    reviews = [] if verification else pending_reviews(queue)
    replay = {r['article_id']: r for r in reviews}
    seen = bot.load_seen_urls()
    exposure = bot.load_exposure_data()
    keywords = list(dict.fromkeys(bot.KEYWORDS+bot.get_overseas_keywords(exposure)))
    raw, failures = [], {}
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
        except Exception:
            with lock:
                failures[query] = True
            raise
    with patch.object(bot.requests, 'get', checked_get), ThreadPoolExecutor(max_workers=5) as pool:
        for rows in pool.map(bot.crawl_naver_news, keywords):
            raw.extend(a for a in rows if a.get('url') and a['url'] not in seen)
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
        if not bot.is_hard_excluded(article['title'], article.get('desc', ''), article['url'])[0]:
            items.append(article)
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
    def body(article):
        text = bot.fetch_article_body(article['url']) or ''
        if not any(c.isalnum() for c in text) and article['article_id'] in replay:
            return copy.deepcopy(replay[article['article_id']]['body'])
        # Preserve bounded paragraphs, including entity context; no menus-as-body.
        return {'article_id': article['article_id'], 'body': text[:12000],
                'body_status': 'verified' if any(c.isalnum() for c in text) else 'missing',
                'source_url': article['url'], 'truncated': len(text) > 12000}
    with ThreadPoolExecutor(max_workers=6) as pool:
        manifest = list(pool.map(body, items))
    # The live main can finish a prior run while collection is in progress.
    # Refresh delivery history; keep the exposure used to collect/filter this job.
    data, _ = queue.source('seen_news.json')
    Path('seen_news.json').write_bytes(data)
    current_seen = bot.load_seen_urls()
    raw = [a for a in raw if a['url'] not in current_seen or a['article_id'] in replay]
    items = [a for a in items if a['url'] not in current_seen or a['article_id'] in replay]
    allowed_ids = {a['article_id'] for a in items}
    manifest = [b for b in manifest if b['article_id'] in allowed_ids]
    created = now()
    jid = ('risk-verification-' if verification else 'risk-production-')+os.environ.get('GITHUB_RUN_ID', created.strftime('%Y%m%dT%H%M%S'))
    if not JOB_ID.fullmatch(jid) or queue.get('jobs/'+jid+'.json')[0]:
        raise RuntimeError('회차 ID·중복 오류')
    inputs = {'items': items, 'raw_articles': raw, 'history_hash': file_hash('seen_news.json'),
              'exposure_hash': file_hash('exposure_data.csv'), 'snapshot_mode': 'natural_collection'}
    rules = policy()
    job = {'schema_version': 1, 'bot_id': 'risk_monitor', 'job_id': jid,
           'mode': 'verification' if verification else 'production', 'status': 'pending',
           'created_at': created.isoformat(), 'expires_at': (created+timedelta(minutes=config['job_ttl_minutes'])).isoformat(),
           'input': inputs, 'input_hash': digest(inputs), 'body_manifest': manifest,
           'body_manifest_hash': digest(manifest), 'policy_version': 'risk-subscription-v1',
           'policy_hash': digest(rules), 'receipts': {}, 'paid_ai_calls': 0}
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
    print(json.dumps({'job_id': jid, 'status': 'pending', 'inputs': len(items), 'paid_ai_calls': 0}))


def verify_job(queue, jid, self_test=False):
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
        print(json.dumps({'job_id': jid, 'status': existing['status'], 'noop': True,
                          'smtp_calls': 0, 'paid_ai_calls': 0}))
        return
    config, job, sha, result = verify_job(queue, jid, self_test)
    bot = import_bot()
    if self_test:
        from risk_owner_verification import OWNER
        bot.EMAIL_RECEIVERS, bot.EMAIL_CC = [], []
        bot.NO_RESULT_RECEIVER = OWNER
        bot.FORCE_SELF_ONLY = True
    view, answers = delivery_view(job, result)
    provider = Provider(view, answers)
    original_seen = Path('seen_news.json').read_bytes()
    original_exposure = file_hash('exposure_data.csv')
    metadata, current_sha = queue.get('jobs/'+jid+'.json')
    if current_sha != sha:
        raise RuntimeError('소비 검증 중 회차 상태 변경')
    verdicts = {r['article_id']: r['relevant'] for r in result['classifications']}
    deferred_ids = [aid for aid, value in verdicts.items() if value is None]
    values, expected, resolutions = ({}, {}, {}) if self_test else prepare_reviews(queue, job, verdicts)
    overdue_count = sum(utc_time(values['review/'+aid+'.json']['first_seen_at']) <= now()-timedelta(hours=24)
                        for aid in deferred_ids)
    metadata.update(status='sending', self_test=self_test, sending_at=now().isoformat())
    metadata.update(deferred_ids=deferred_ids, review_resolutions=resolutions,
                    analysis_complete=not deferred_ids, overdue_review_count=overdue_count)
    if values or resolutions:
        values['jobs/'+jid+'.json'], expected['jobs/'+jid+'.json'] = metadata, sha
        queue.put_many(values, expected)
    else:
        queue.put('jobs/'+jid+'.json', metadata, sha)
    guard = Delivery(queue, jid, smtplib.SMTP_SSL,
                     lambda: current_config(queue, self_test))
    native_send, native_empty, native_save = bot.send_email, bot.send_email_no_result, bot.save_seen_urls
    def review_notice(subject, document, empty=False):
        if not deferred_ids:
            return subject, document
        label = '확인 보류 '+str(len(deferred_ids))+'건'
        if overdue_count:
            label += ' · 24시간 이상 미확인 '+str(overdue_count)+'건'
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
        subject, html = review_notice(subject, html)
        targets = [bot.NO_RESULT_RECEIVER or bot.EMAIL_SENDER] if self_only else list(dict.fromkeys([bot.EMAIL_SENDER]+bot.EMAIL_RECEIVERS+bot.EMAIL_CC))
        if self_test:
            targets, self_only = [bot.NO_RESULT_RECEIVER], True
            subject = '[TEST] '+subject
        guard.prepare(targets)
        queue.put('previews/'+jid+'.json', {'html': html, 'self_test': self_test, 'paid_ai_calls': 0})
        value = native_send(subject, html, self_only=self_only)
        guard.complete()
        return value
    def empty(subject, html):
        subject, html = review_notice(subject, html, empty=True)
        guard.prepare([bot.NO_RESULT_RECEIVER or bot.EMAIL_SENDER])
        queue.put('previews/'+jid+'.json', {'html': html, 'self_test': self_test, 'paid_ai_calls': 0})
        native_empty(('[TEST] ' if self_test else '')+subject, html)
        guard.complete()
    def save(*args, **kwargs):
        guard.complete()
        if args:
            held_urls = {a['url'] for a in job['input']['items'] if a['article_id'] in deferred_ids}
            args = (set(args[0])-held_urls,)+args[1:]
        return native_save(*args, **kwargs)
    def private_stats(*args, **kwargs):
        queue.put('metrics/'+jid+'.json', {'stats': copy.deepcopy(bot._RUN_STATS), 'paid_ai_calls': 0})
    def private_log(*args):
        # Exact final/source objects stay private; the native logger writes no public file.
        queue.put('filter-logs/'+jid+'.json', {'raw': args[0], 'hard_excluded': args[1],
                  'ai_selected': args[2], 'final': args[3], 'paid_ai_calls': 0})
    def no_paid_post(*args, **kwargs):
        raise RuntimeError('유료 HTTP inference POST 차단')
    try:
        provider.install(bot)
        with contextlib.ExitStack() as stack:
            frozen = read_exposure(queue, job, config)
            if frozen is not None:
                directory = stack.enter_context(tempfile.TemporaryDirectory())
                snapshot_path = Path(directory)/'exposure_data.csv'
                snapshot_path.write_bytes(frozen)
                stack.enter_context(patch.object(bot, 'EXPOSURE_FILE', str(snapshot_path)))
            if self_test:
                temporary = stack.enter_context(tempfile.TemporaryDirectory())
                seen_path = Path(temporary)/'seen_news.json'
                seen_path.write_bytes(original_seen)
                stack.enter_context(patch.object(bot, 'SEEN_FILE', str(seen_path)))
            stack.enter_context(patch.object(bot, 'send_email', send))
            stack.enter_context(patch.object(bot, 'send_email_no_result', empty))
            stack.enter_context(patch.object(bot, 'save_seen_urls', save))
            stack.enter_context(patch.object(bot, 'save_run_stats', private_stats))
            stack.enter_context(patch.object(bot, 'save_filter_log', private_log))
            stack.enter_context(patch.object(bot.smtplib, 'SMTP_SSL', guard.factory))
            stack.enter_context(patch.object(bot.requests, 'post', no_paid_post))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            bot.main()
        guard.complete()
        queue.put('logs/'+jid+'.json', {'log': output.getvalue(), 'paid_ai_calls': 0})
        if file_hash('exposure_data.csv') != original_exposure:
            raise RuntimeError('잔고 파일 변경')
        if self_test and Path('seen_news.json').read_bytes() != original_seen:
            raise RuntimeError('본인 검수 중 운영 이력 변경')
        snapshot = Path('seen_news.json').read_text()
        guard.change(lambda j: j.update(status='verified' if self_test else 'delivery_complete',
            verified_at=now().isoformat() if self_test else None,
            delivered_at=now().isoformat(), paid_ai_calls=0, self_test=self_test,
            seen_after_hash=file_hash('seen_news.json'), seen_after_snapshot=snapshot,
            production_history_writes=0 if self_test else None))
        print(json.dumps({'job_id': jid, 'status': 'verified' if self_test else 'delivery_complete',
                          'paid_ai_calls': 0, 'self_test': self_test,
                          'analysis_complete': not deferred_ids, 'deferred_count': len(deferred_ids),
                          'smtp_accepted': len(guard.complete()['receipts'])}))
    except Exception:
        # An authenticated connection failure before any DATA is safely retryable.
        def failed(j):
            j['status'] = 'uncertain' if j.get('receipts') or j.get('attempted_count', 0) else 'pending'
        guard.change(failed)
        raise
    finally:
        bot.RISK_PROVIDER = None


def acknowledge(queue, jid):
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
    if values:
        values['jobs/'+jid+'.json'], expected['jobs/'+jid+'.json'] = job, sha
        queue.put_many(values, expected)
    else:
        queue.put('jobs/'+jid+'.json', job, sha)
    print(json.dumps({'job_id': jid, 'status': 'completed', 'paid_ai_calls': 0, 'history_saved': True,
                      'ack': True, 'analysis_complete': job.get('analysis_complete', True),
                      'deferred_count': len(job.get('deferred_ids', []))}))


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
    except Exception:
        print('담당자 오류 알림 실패; Actions 확인 필요')
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['scheduled', 'collect-test', 'consume', 'ack', 'connection', 'rollback-check', 'notify-error'])
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
    queue.preflight()
    if args.operation in {'scheduled', 'collect-test'}:
        collect(config, queue, args.operation == 'collect-test')
    elif args.operation == 'consume':
        consume(queue, args.job_id or '', args.self_test)
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
        # Native logs/results/addresses are private. No raw error payload in Actions.
        print('리스크 구독 경로 보류:', type(error).__name__)
        import traceback
        notify_error(error, traceback.format_exc())
        raise SystemExit(1)
