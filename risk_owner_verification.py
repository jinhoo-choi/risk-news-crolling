"""Private, owner-only historical fixture verification using the native HTML.

Never runs the production crawler/main, paid inference, or production history writers.
SMTP reserves its attempt in the private queue before DATA and never retries DATA.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import smtplib
from datetime import datetime, timezone
from email import message_from_string
from email.utils import getaddresses
from pathlib import Path
from unittest.mock import patch

from risk_queue import Queue, now
from risk_result_validator import validate, utc_time

OWNER = '111715@koreainvestment.com'  # Same owner as existing test_send_self.py.
ERROR_EMAIL = 'myboy3743@gmail.com'  # Runtime/operational alerts only; verification stays unchanged.


def failure_mail(request):
    """One real owner-only diagnostic mail from saved evidence, never production consume."""
    from datetime import timedelta
    from risk_queue import JOB_ID, digest
    from risk_audit import redact
    if (set(request) != {'operation', 'source_job_id', 'self_test', 'request_id'}
            or request['operation'] != 'failure-mail' or request['self_test'] is not True
            or not JOB_ID.fullmatch(request['source_job_id'])
            or not JOB_ID.fullmatch(request['request_id'])):
        raise RuntimeError('실패 알림 검수 요청 오류')
    if (os.environ.get('GITHUB_RUN_ATTEMPT', '1') != '1'
            or os.environ.get('EMAIL_RECEIVER') or os.environ.get('EMAIL_CC')
            or os.environ.get('ANTHROPIC_API_KEY')):
        raise RuntimeError('재실행·그룹·유료 AI 경로 검수 금지')
    if os.environ.get('EMAIL_SENDER') != ERROR_EMAIL or not os.environ.get('EMAIL_PASSWORD'):
        raise RuntimeError('기존 본인 발신 SMTP 설정 확인 실패')
    before = snapshot()
    queue = Queue(); queue.preflight()
    source_path = 'jobs/'+request['source_job_id']+'.json'
    source, _ = queue.get_fresh(source_path)
    attempt, _ = queue.get_fresh('analysis-attempts/'+request['source_job_id']+'.json')
    if (not source or source.get('mode') != 'production' or source.get('status') != 'pending'
            or source.get('receipts') or not attempt or attempt.get('state') not in {'blocked', 'failed'}
            or not attempt.get('errors') or attempt.get('input_hash') != source.get('input_hash')
            or attempt.get('policy_hash') != source.get('policy_hash')):
        raise RuntimeError('고정 회차·실제 차단 근거 불일치 — 검수 발송 금지')
    jid = request['request_id']
    path = 'jobs/'+jid+'.json'
    previous, _ = queue.get_fresh(path)
    if previous is not None:
        if (previous.get('test_kind') == 'failure_mail' and previous.get('status') == 'verified'
                and previous.get('receipts') == {hashlib.sha256(ERROR_EMAIL.encode()).hexdigest(): 'accepted'}):
            print(json.dumps({'status':'verified','smtp_calls':0,'noop':True}))
            return
        raise RuntimeError('기존 테스트 시도 보존 — 자동 재전송 금지')
    job = {'schema_version':1, 'mode':'verification', 'test_kind':'failure_mail', 'job_id':jid,
           'status':'pending', 'receipts':{}, 'created_at':now().isoformat(),
           'expires_at':(now()+timedelta(hours=1)).isoformat(), 'self_test':True,
           'source_job_id':request['source_job_id'], 'source_hash':digest(source)}
    queue.put(path, job)
    # Use the same native failure/operational HTML, MIME builder and SMTP as production.
    os.environ.update(EMAIL_RECEIVER='', EMAIL_CC='', NO_RESULT_RECEIVER=ERROR_EMAIL,
                      RISK_ENGINE='subscription')
    os.environ.setdefault('ANTHROPIC_API_KEY', 'subscription-api-disabled')
    os.environ.setdefault('NAVER_CLIENT_ID', 'verification-not-used')
    os.environ.setdefault('NAVER_CLIENT_SECRET', 'verification-not-used')
    import naver_news_monitor as bot
    bot.EMAIL_SENDER, bot.EMAIL_PASSWORD = os.environ['EMAIL_SENDER'], os.environ['EMAIL_PASSWORD']
    bot.EMAIL_RECEIVERS, bot.EMAIL_CC = [], []
    real_build, real_smtp = bot._build_msg, smtplib.SMTP_SSL
    prepared = {}
    def build(subject, document, recipient):
        if recipient != ERROR_EMAIL:
            raise RuntimeError('검수 수신자 변경 금지')
        subject = '[TEST]['+jid+'] '+subject
        prepared.update(subject=subject, html=document, html_sha256=hashlib.sha256(document.encode()).hexdigest())
        queue.put('previews/'+jid+'.json', prepared)
        return real_build(subject, document, recipient)
    reasons = '\n\n'.join(redact(json.dumps(e, ensure_ascii=False, indent=2)) for e in attempt['errors'])
    details = ('[TEST] 실환경 미발송 사유 재현입니다. 새 장애 발생이나 정기 보고가 아닙니다.\n'
               '대상: '+request['source_job_id']+'\n현재 상태: '+source['status']+
               '\n분석 입력: '+str(source.get('input_count'))+'건 · 완료: '+str(attempt.get('processed_count'))+
               '건\n정기 보고 SMTP 시도: '+str(source.get('attempted_count', 0))+
               '건\n차단 단계: '+attempt['stage']+'\n\n실제 기록된 사유:\n'+reasons+
               '\n\n21:35 실행의 내부 중단 사유는 기록 부족으로 별도 미확정입니다.'
               '\n이 메일은 수신자 본인 한정 테스트 1건입니다. 운영 회차·이력·ACK는 변경하지 않습니다.')
    guard = OwnerSMTP(real_smtp, queue, path, recipient=ERROR_EMAIL)
    output = io.StringIO()
    with patch.object(bot, '_build_msg', build), patch.object(bot.smtplib, 'SMTP_SSL', guard.factory), \
            contextlib.redirect_stdout(output):
        accepted = bot.send_email_error('[TEST] 구독 분석 차단으로 21시 정기 보고 미발송', details, operational=True)
    try:
        queue.put('logs/'+jid+'.json', {'log':redact(output.getvalue()), 'smtp_attempts':guard.attempts,
                                      'smtp_accepted':guard.accepted, 'paid_ai_calls':0})
    except Exception as error:
        error.stdout = redact(output.getvalue())
        raise
    unchanged, _ = queue.get_fresh(source_path)
    if accepted is not True or guard.accepted != 1 or snapshot() != before or digest(unchanged) != digest(source):
        raise RuntimeError('테스트 접수·원본 격리 확인 실패 — 재전송 금지')
    current, sha = queue.get_fresh(path)
    current.update(status='verified', verified_at=now().isoformat(), smtp_accepted=1,
                   html_sha256=prepared['html_sha256'], production_smtp_calls=0,
                   production_history_writes=0, before_hashes=before, after_hashes=snapshot())
    queue.put(path, current, sha)
    print(json.dumps({'job_id':jid,'status':'verified','smtp_accepted':1,'smtp_calls':1,
                      'production_smtp_calls':0,'production_history_writes':0,'paid_ai_calls':0}))


def snapshot():
    return {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
            for p in ('seen_news.json', 'exposure_data.csv', 'run_stats.jsonl')}


def require_pending(job):
    if not job or job.get('mode') != 'verification' or job.get('status') != 'pending':
        raise RuntimeError('미발송 본인 검수 회차만 허용')
    if job.get('receipts') or utc_time(job['expires_at']) <= now():
        raise RuntimeError('만료·발송 시도 회차 재처리 금지')


def render(bot, job, result):
    articles = []
    bodies = {b['article_id']: b for b in job['body_manifest']}
    reasons = {r['article_id']: r['reason'] for r in result['classifications']}
    for item in job['input']['items']:
        analysis = result['analyses'].get(item['article_id'])
        if not analysis:
            continue
        body = bodies[item['article_id']]['body']
        articles.append(dict(copy.deepcopy(item), desc=item['description'],
            pub_str=item['published_at'][:16].replace('T', ' ') + ' (과거 기사 검수)',
            keyword='실제 기사 검수', grade=analysis['base_grade'],
            entity=analysis['entity'], entities=analysis['entities'],
            event_type=analysis['event_type'], event_key=analysis['entity']+'_'+analysis['event_type'],
            reason=reasons[item['article_id']], action=analysis['action'],
            customer_notice=analysis['customer_notice'], _ai_confidence=analysis['confidence'],
            body=body, _body_failed=False, _body_source_url=item['url'],
            _body_source_hash=hashlib.sha256(body.encode()).hexdigest(),
            _body_risk_confirmed=True, _judgment=analysis['judgment']))
    exposure = bot.load_exposure_data()
    articles = bot.regrade_by_score(articles, exposure)
    for article in articles:
        rows = bot.find_exposure(article.get('entity', ''), exposure)
        action, _ = bot.strip_unsupported_action_clauses(article.get('action', ''), rows)
        tainted, _ = bot.sanitize_action_numbers(action, rows, article['title']+' '+article['body'])
        if tainted:
            raise RuntimeError('검수 대응방안 수치 오염')
        article['action'] = action
        if bot.notice_forbidden_reason(article):
            article['customer_notice'] = None
    bot._LAST_ANALYSIS_ENGINE = 'chatgpt_subscription'
    bot._LAST_VERIFY_MODEL = result['model_used']
    ref = next(iter(exposure.values()))[0].get('기준일', '') if exposure else ''
    html = bot.build_email_html(articles, total_count=len(job['input']['items']),
        ai_summary='[TEST] 과거 실제 기사·현재 잔고 검수. 오늘 신규 경보가 아닙니다.',
        exposure_data=exposure, ref_date=ref,
        today_str=datetime.now(bot.timezone(bot.timedelta(hours=9))).strftime('%Y-%m-%d'))
    return html, articles


class OwnerSMTP:
    """Wrap the native sender while prohibiting recipient drift and DATA retry."""
    def __init__(self, real, queue, path, recipient=OWNER):
        if recipient not in {OWNER, ERROR_EMAIL}:
            raise RuntimeError('허용되지 않은 검수 수신자')
        self.real, self.queue, self.path = real, queue, path
        self.recipient = recipient
        self.attempts = 0
        self.accepted = 0

    def factory(self, host, port, **kwargs):
        if (host, port) != ('smtp.gmail.com', 465) or self.attempts:
            raise RuntimeError('허용되지 않은 SMTP 연결 또는 DATA 재시도')
        server = self.real(host, port, timeout=30)
        outer = self

        class Session:
            def __enter__(self):
                return self

            def __exit__(self, typ, value, trace):
                try:
                    server.quit()
                except Exception:
                    server.close()

            def ehlo(self):
                return server.ehlo()

            def login(self, sender, password):
                return server.login(sender, password)

            def sendmail(self, sender, recipients, raw):
                message = message_from_string(raw)
                to = [addr for _, addr in getaddresses(message.get_all('To', []))]
                if recipients != [outer.recipient] or to != [outer.recipient] or message.get('Cc') or message.get('Bcc'):
                    raise RuntimeError('본인 수신자·헤더 격리 실패')
                job, sha = getattr(outer.queue,'get_fresh',outer.queue.get)(outer.path)
                require_pending(job)
                outer.attempts += 1
                key = hashlib.sha256(outer.recipient.encode()).hexdigest()
                job.update(status='sending', attempted_count=outer.attempts, self_test=True)
                job['receipts'] = {key: 'sending'}
                outer.queue.put(outer.path, job, sha)
                try:
                    refused = server.sendmail(sender, recipients, raw)
                except Exception:
                    job, sha = getattr(outer.queue,'get_fresh',outer.queue.get)(outer.path)
                    job.update(status='uncertain')
                    job['receipts'][key] = 'uncertain'
                    outer.queue.put(outer.path, job, sha)
                    raise RuntimeError('SMTP 접수 미확인 — 자동 재전송 금지') from None
                job, sha = getattr(outer.queue,'get_fresh',outer.queue.get)(outer.path)
                job['receipts'][key] = 'rejected' if refused else 'accepted'
                if refused:
                    job.update(status='uncertain')
                else:
                    outer.accepted += 1
                    job.update(delivered_at=now().isoformat())
                outer.queue.put(outer.path, job, sha)
                return refused

        return Session()


def main():
    request = json.loads(Path('risk_verification_request.json').read_text())
    if request.get('operation') == 'failure-mail':
        from risk_audit import run_audited
        return run_audited(Queue(), 'failure-mail', request.get('request_id', ''), lambda: failure_mail(request))
    if request.get('operation') == 'scenario-suite':
        from risk_scenario_verification import run
        return run(request)
    if set(request) != {'operation', 'job_id', 'self_test', 'request_id'} or request['operation'] != 'consume' or request['self_test'] is not True:
        raise RuntimeError('본인 검수 메타데이터 오류')
    from risk_queue import JOB_ID
    if not JOB_ID.fullmatch(request['job_id']):
        raise RuntimeError('회차 ID 오류')
    if os.environ.get('GITHUB_RUN_ATTEMPT', '1') != '1':
        raise RuntimeError('검수 워크플로 재실행 금지 — SMTP 미확인 재전송 방지')
    # Group secrets are intentionally not supplied by the workflow.
    if os.environ.get('EMAIL_RECEIVER') or os.environ.get('EMAIL_CC') or os.environ.get('ANTHROPIC_API_KEY'):
        raise RuntimeError('그룹·유료 AI 키가 있는 검수 환경 금지')
    before = snapshot()
    queue = Queue()
    queue.preflight()
    path = 'jobs/'+request['job_id']+'.json'
    job, _ = queue.get(path)
    if job and job.get('mode') == 'verification' and job.get('status') in {'verified', 'completed'}:
        print('이미 종결된 본인 검수 회차; 분석0/SMTP0/쓰기0')
        return
    require_pending(job)
    result, _ = queue.get('results/'+request['job_id']+'.json')
    contract = validate(result, job, job['input'], job['body_manifest'])
    if not contract['consume_ready'] or before['seen_news.json'] != job['input']['history_hash'] or before['exposure_data.csv'] != job['input']['exposure_hash']:
        raise RuntimeError('분석·현재 이력·잔고 검증 실패')
    os.environ.update(EMAIL_RECEIVER='', EMAIL_CC='', NO_RESULT_RECEIVER=OWNER,
                      ANTHROPIC_API_KEY='subscription-api-disabled',
                      NAVER_CLIENT_ID='verification-not-used', NAVER_CLIENT_SECRET='verification-not-used')
    import naver_news_monitor as bot
    bot.NO_RESULT_RECEIVER = OWNER
    bot.EMAIL_RECEIVERS, bot.EMAIL_CC = [], []
    def deny_post(*args, **kwargs):
        raise RuntimeError('유료 AI·HTTP POST 차단')
    with patch.object(bot.requests, 'post', side_effect=deny_post), contextlib.redirect_stdout(io.StringIO()):
        html, articles = render(bot, job, result)
    queue.put('previews/'+request['job_id']+'.json',
              {'html': html, 'selected': len(articles), 'paid_ai_calls': 0,
               'simulation': True, 'native_html': True})
    require_pending(queue.get(path)[0])
    if before != snapshot():
        raise RuntimeError('렌더 중 운영 파일 변경')
    guard = OwnerSMTP(smtplib.SMTP_SSL, queue, path)
    subject = '[TEST] 리스크봇 구독 분석 검수 — 과거 실제 기사·본인 전용'
    with patch.object(bot.requests, 'post', side_effect=deny_post), patch.object(bot.smtplib, 'SMTP_SSL', guard.factory), contextlib.redirect_stdout(io.StringIO()):
        bot.send_email(subject, html, self_only=True)
    current, sha = queue.get(path)
    if guard.accepted != 1 or any(r != 'accepted' for r in current['receipts'].values()) or before != snapshot():
        raise RuntimeError('본인 SMTP 접수·운영 이력 격리 검증 실패')
    current.update(status='verified', verified_at=now().isoformat(), paid_ai_calls=0,
                   production_smtp_calls=0, production_history_writes=0,
                   before_hashes=before, after_hashes=snapshot(), native_html=True,
                   selected_count=len(articles), self_test=True)
    queue.put(path, current, sha)
    print(json.dumps({'job_id': request['job_id'], 'status': 'verified',
                      'self_test': True, 'smtp_accepted': 1, 'paid_ai_calls': 0,
                      'production_smtp_calls': 0, 'production_history_writes': 0,
                      'native_html': True}))


if __name__ == '__main__':
    main()
