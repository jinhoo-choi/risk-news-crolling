"""Native scenario replay and explicit Gmail-only delivery; no production writes."""
import contextlib
import base64
import copy
import csv
import hashlib
import html
import io
import json
import os
import re
import smtplib
import shutil
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from risk_owner_verification import OwnerSMTP, ERROR_EMAIL, snapshot
from risk_queue import Queue, JOB_ID, now, digest

CASES = [
    ('S01','당사 직접 사건','fixture',1),
    ('S02','같은 사건 최고점 기사만','grouped_fixture',1),
    ('S03','서로 다른 집계일의 같은 통계','market_statistics_fixture',1),
    ('S04','부분 인용과 검토된 사건 그룹','partial_market_statistics_fixture',1),
    ('S05','같은 회사의 서로 다른 사건','independent_same_company_fixture',2),
    ('S06','주식·채권과 뱅키스·영업점','exposure',1),
    ('S07','적격 기사 없음·본인 한정','empty',0),
    ('S08','경쟁사 공지는 오늘 등록분만','notices',1),
]
CONTROLS = [
    ('입력 ID 누락 차단','test_risk_subscription.Integration.test_incomplete_result_blocks_smtp'),
    ('잘못된 중복 그룹 차단','test_risk_subscription.Integration.test_invalid_or_missing_event_comparison_blocks_before_smtp'),
    ('본문 미확보 발송 차단','test_risk_subscription.Integration.test_body_missing_holds_delivery_and_seen_history'),
    ('만료·불확실 상태 보존','test_risk_subscription.Integration.test_expiration_cleanup_never_unlocks_uncertain'),
    ('SMTP 불확실 자동 재전송 금지','test_risk_subscription.Integration.test_uncertain_data_never_retries_or_saves_history'),
    ('일부 접수 시 ACK 금지','test_risk_subscription.Integration.test_partial_delivery_never_acks_or_writes_history'),
    ('전원 접수 후 이력만 복구','test_risk_delivery_recovery.Recovery.test_all_accepted_recovery_restores_history_without_any_smtp'),
    ('고정본 저장 검증 전 SMTP 차단','test_risk_delivery_recovery.Recovery.test_put_saved_but_read_missing_blocks_then_recovers_exactly_once'),
    ('유료 AI 폴백 차단','test_risk_subscription.Integration.test_paid_fallback_blocked_without_provider'),
    ('손상 이력으로 운영 금지','test_analysis_guards.HistoryTests.test_main_stops_before_collection_on_corrupt_history'),
    ('회생 신청과 개시 결정 구분','test_event_history.DatedEventGateTests.test_application_is_not_opening_and_real_order_reopens_old_bad_history'),
    ('미래·불명·타사 사건 직접 귀속 금지','test_analysis_guards.DirectIncidentTests.test_unknown_future_negated_and_mention_only_cases'),
    ('유사 회사명 익스포저 혼합 금지','test_risk_exposure.Exposure.test_short_names_and_substrings_never_absorb_other_issuers'),
    ('직접 복수 주체와 배경 계열사 구분','test_risk_exposure.Exposure.test_reviewed_joint_subjects_survive_while_background_affiliate_is_hidden'),
    ('CSV 변경 시 고정 잔고 사용','test_risk_continuity.NativeContinuity.test_current_balance_change_uses_snapshot_and_preserves_main_csv'),
    ('확인 보류를 정상 0건으로 표시 금지','test_risk_continuity.NativeContinuity.test_all_null_sends_owner_status_without_claiming_no_risk'),
    ('재검토의 임원 잔고메일 반복 방지','test_risk_continuity.NativeContinuity.test_review_no_positive_cannot_send_market_digest_to_groups'),
    ('Gmail 외 수신자·헤더 차단','test_risk_owner_verification.IsolationTests.test_gmail_suite_rejects_company_group_and_header_drift'),
    ('오너 제외 기사 이력 불변','test_risk_continuity.NativeContinuity.test_owner_exclusion_removes_only_requested_article_without_rewriting_result'),
    ('구독 결과 종결 회차 재소비 금지','test_risk_subscription.Integration.test_terminal_request_is_noop_without_smtp_or_history_write'),
]


def rehash(queue):
    job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
    job['input']['raw_articles'] = copy.deepcopy(job['input']['items'])
    job['input_hash'] = result['input_hash'] = digest(job['input'])
    job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])


def simulate(spec):
    from test_risk_subscription import Integration, SMTP
    key, title, fixture, count = spec
    output = io.StringIO()
    with patch.dict(os.environ, dict(os.environ), clear=True), contextlib.redirect_stdout(output):
        case = Integration()
        case.setUp()
        try:
            queue = getattr(case, fixture if hasattr(case, fixture) else 'fixture')()
            job, answer = queue.data['jobs/test.json'], queue.data['results/test.json']
            if key == 'S04':
                answer['analyses']['two']['confidence'] = 0.99
            if fixture == 'exposure':
                date = now().astimezone(case.bot.timezone(case.bot.timedelta(hours=9))).strftime('%Y년 %m월 %d일')
                body = date+' 가상검수전자의 공장에서 화재가 발생해 생산이 중단됐다.'
                item = job['input']['items'][0]
                item.update(title='가상검수전자 공장 화재 발생·생산 중단',description=body,desc=body)
                job['body_manifest'][0]['body'] = body
                a = answer['analyses']['one']
                a.update(entity='가상검수전자',entities=['가상검수전자'],event_type='기타리스크',
                         fact_summary=body, action='가상검수전자 공장 가동 중단 범위와 보유 고객 영향을 확인한다.',
                         judgment={'핵심사건':'공장 화재','손실주체':'회사','당사연관':'직접','확정여부':'확정'},
                         evidence=[{'source_segment':'body','source_hash':hashlib.sha256(body.encode()).hexdigest(),'quote':body}])
                fields=['기준일','종목명','종목코드','종목유형','뱅잔고','영잔고','뱅고객수','영고객수']
                with Path('exposure_data.csv').open('w',newline='') as f:
                    writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
                    for code,kind,bank,branch in [('990001','주식','120','30'),('990002','채권','40','10')]:
                        writer.writerow(dict(zip(fields,[date[:4]+'-'+date[6:8]+'-'+date[10:12],
                            '가상검수전자',code,kind,bank,branch,'2','1'])))
                job['input']['exposure_hash']=hashlib.sha256(Path('exposure_data.csv').read_bytes()).hexdigest()
            elif fixture == 'empty':
                answer['classifications'][0].update(relevant=False,reason='가상 비해당 시나리오.')
                answer['analyses'] = {}
            elif fixture == 'notices':
                case.notice.stop()
                today=now().astimezone(case.bot.timezone(case.bot.timedelta(hours=9))).date()
                Path('data').mkdir()
                with Path('data/fixture.csv').open('w',newline='') as f:
                    writer=csv.DictWriter(f,fieldnames=['published_date','title','company','url']);writer.writeheader()
                    for offset,label in [(0,'금일공지'),(-1,'전일공지'),(1,'미래공지')]:
                        writer.writerow({'published_date':(today+timedelta(days=offset)).isoformat(),
                            'title':'신용대출 가능종목 변경 '+label,'company':'가상검수증권',
                            'url':'https://example.invalid/notice/'+label})
            rehash(queue)
            smtp=SMTP()
            case.run_consume(queue,smtp)
            final=queue.data.get('filter-logs/test.json',{}).get('final',[])
            document=queue.data['previews/test.json']['html']
            assert len(final)==count, (key,'card count',len(final),count)
            if key=='S02': assert final[0]['article_id']=='two'
            if key=='S03': assert final[0]['article_id']=='three'
            if key=='S04': assert final[0]['article_id']=='two'
            if key=='S05': assert {a['article_id'] for a in final}=={'one','two'}
            if key=='S06':
                for word in ['뱅키스','영업점','주식잔고','채권잔고','120억','30억','40억','10억']:
                    assert word in document, (key,word)
            if key=='S07': assert len(smtp.calls)==1 and '해당하는 뉴스가 없습니다' in document
            if key=='S08':
                assert '금일공지' in document and '전일공지' not in document and '미래공지' not in document
            # The production path above uses only in-memory SMTP and temporary
            # holdings/history. Its output is the exact native HTML sent below.
            return {'scenario':key,'title':title,'expected_cards':count,'selected':len(final),
                    'native_html':True,'simulation':True,'html':document,
                    'simulated_routes':[x[0] for x in smtp.calls],
                    'input':copy.deepcopy(job['input']), 'analysis':copy.deepcopy(answer),
                    'final':copy.deepcopy(final),'log':output.getvalue()}
        finally:
            case.tearDown()


def prepare():
    prepared=[simulate(spec) for spec in CASES]
    controls=[]
    for title,name in CONTROLS:
        log=io.StringIO()
        with patch.dict(os.environ,dict(os.environ),clear=True),contextlib.redirect_stdout(log):
            result=unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromName(name).run(result)
        if not result.wasSuccessful():
            raise AssertionError(name+': '+str(result.errors+result.failures))
        controls.append({'title':title,'test':name,'passed':True,'tests':result.testsRun})
    rows=''.join('<tr><td style="padding:8px;border-bottom:1px solid #ddd">'+html.escape(c['title'])+
                 '</td><td style="padding:8px;color:#166534">통과</td></tr>' for c in controls)
    document='<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="font-family:sans-serif;margin:0;padding:20px"><h2>차단·경계·복구 시나리오</h2><p>가상 환경 검사입니다. 차단 대상의 실제 메일 발송은 없으며, 이 메일은 검수 결과표입니다.</p><table style="border-collapse:collapse;width:100%;max-width:640px">'+rows+'</table></body></html>'
    prepared.append({'scenario':'S09','title':'차단·경계·복구 20개 시나리오','html':document,
                     'native_html':False,'simulation':True,'controls':controls,'selected':0})
    return prepared


def capture_previews(prepared,queue,batch):
    chrome=shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        return {'status':'unavailable','reason':'chrome_not_installed'}
    images=[]
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        for entry in prepared:
            if entry['scenario'] not in {'S05','S06','S08'}: continue
            for width in (390,900):
                name=entry['scenario']+'-'+str(width)
                page,image=root/(name+'.html'),root/(name+'.png')
                probe='<script>document.title="AUDIT_METRICS:"+JSON.stringify({viewport:innerWidth,scroll:document.documentElement.scrollWidth});</script>'
                page.write_text(entry['html'].replace('</body>',probe+'</body>'))
                command=[chrome,'--headless','--no-sandbox','--disable-dev-shm-usage',
                         '--hide-scrollbars','--window-size='+str(width)+',2400',
                         '--screenshot='+str(image),'--dump-dom',page.as_uri()]
                try:
                    rendered=subprocess.run(command,capture_output=True,text=True,timeout=30)
                    match=re.search(r'<title>AUDIT_METRICS:(.*?)</title>',rendered.stdout)
                    metrics=json.loads(html.unescape(match[1])) if match else {}
                    if rendered.returncode or not image.exists():
                        raise RuntimeError('render_failed')
                    path='previews/'+batch+'-'+name+'-image.json'
                    queue.put(path,{'mime_type':'image/png','encoding':'base64',
                                   'content':base64.b64encode(image.read_bytes()).decode(),
                                   'scenario':entry['scenario'],'requested_width':width,'metrics':metrics})
                    images.append({'path':path,'scenario':entry['scenario'],'width':width,'metrics':metrics})
                except (OSError,subprocess.TimeoutExpired,ValueError,RuntimeError):
                    return {'status':'partial','images':images,'failed':name}
    return {'status':'rendered','images':images}


def run(request):
    if (set(request)!={'operation','self_test','request_id','send'}
            or request['operation']!='scenario-suite' or request['self_test'] is not True
            or type(request['send']) is not bool or not JOB_ID.fullmatch(request['request_id'])
            or len(request['request_id'])>55):
        raise RuntimeError('시나리오 검수 요청 오류')
    if os.environ.get('GITHUB_RUN_ATTEMPT','1')!='1':
        raise RuntimeError('검수 재실행 금지')
    if os.environ.get('EMAIL_RECEIVER') or os.environ.get('EMAIL_CC') or os.environ.get('ANTHROPIC_API_KEY'):
        raise RuntimeError('그룹 수신자·유료 AI 키가 있는 검수 환경 금지')
    before=snapshot()
    sender,password=os.environ.get('EMAIL_SENDER',''),os.environ.get('EMAIL_PASSWORD','')
    real_smtp=smtplib.SMTP_SSL
    prepared=prepare()
    if snapshot()!=before:
        raise RuntimeError('가상 검수 중 운영 파일 변경')
    if not request['send']:
        path=Path('/tmp')/request['request_id']
        path.mkdir(exist_ok=True)
        for entry in prepared:
            (path/(entry['scenario']+'.html')).write_text(entry['html'])
        (path/'results.json').write_text(json.dumps(prepared,ensure_ascii=False,indent=2))
        print(json.dumps({'scenarios':28,'native_previews':8,'control_checks':20,'smtp_calls':0,'paid_ai_calls':0,'production_history_writes':0}))
        return
    if not sender or not password:
        raise RuntimeError('검수 SMTP 설정 없음')
    queue=Queue();queue.preflight()
    visual=capture_previews(prepared,queue,request['request_id'])
    import naver_news_monitor as bot
    bot.EMAIL_SENDER,bot.EMAIL_PASSWORD=sender,password
    bot.EMAIL_RECEIVERS,bot.EMAIL_CC=[],[]
    bot.NO_RESULT_RECEIVER=ERROR_EMAIL
    accepted=0
    for entry in prepared:
        jid=request['request_id']+'-'+entry['scenario']
        path='jobs/'+jid+'.json'
        existing,_=queue.get(path)
        if existing:
            if (existing.get('status')=='verified' and existing.get('mode')=='verification'
                    and existing.get('request_id')==request['request_id']
                    and existing.get('receipts')=={hashlib.sha256(ERROR_EMAIL.encode()).hexdigest():'accepted'}):
                continue
            raise RuntimeError('기존 검수 상태 확인 필요 — 자동 재발송 금지')
        banner='<div style="padding:16px;background:#fef3c7;color:#78350f;font:14px sans-serif"><b>[TEST] 가상 시나리오 · 실제 사건이 아닙니다.</b><br>'+html.escape(jid+' | '+entry['title'])+'<br>운영 수신자 발송 0 · 운영 이력 변경 0 · 유료 AI 호출 0</div>'
        document=re.sub(r'(<body\b[^>]*>)',lambda m:m[1]+banner,entry['html'],count=1)
        subject='[TEST]['+request['request_id']+'] '+entry['scenario']+' '+entry['title']
        payload=dict(entry,html=document,subject=subject,html_sha256=hashlib.sha256(document.encode()).hexdigest())
        job={'schema_version':1,'bot_id':'risk_monitor','job_id':jid,'mode':'verification',
             'test_kind':'scenario_suite','status':'pending','receipts':{},'self_test':True,
             'request_id':request['request_id'],'created_at':now().isoformat(),
             'expires_at':(now()+timedelta(hours=3)).isoformat(),
             'code_commit':os.environ.get('GITHUB_SHA','unconfirmed'),'scenario':entry['scenario']}
        queue.put_many({path:job,'results/'+jid+'.json':payload,
                        'previews/'+jid+'.json':{'html':document,'subject':subject,
                        'html_sha256':payload['html_sha256'],'native_html':entry['native_html']}},
                       {path:None,'results/'+jid+'.json':None,'previews/'+jid+'.json':None})
        if snapshot()!=before: raise RuntimeError('SMTP 전 운영 파일 변경')
        guard=OwnerSMTP(real_smtp,queue,path,recipient=ERROR_EMAIL)
        with patch.object(bot.smtplib,'SMTP_SSL',guard.factory),contextlib.redirect_stdout(io.StringIO()):
            bot.send_email(subject,document,self_only=True)
        current,sha=queue.get(path)
        if guard.accepted!=1 or snapshot()!=before: raise RuntimeError('검수 접수·운영 격리 실패')
        current.update(status='verified',verified_at=now().isoformat(),smtp_accepted=1,
                       paid_ai_calls=0,production_smtp_calls=0,production_history_writes=0,
                       before_hashes=before,after_hashes=snapshot(),native_html=entry['native_html'])
        queue.put(path,current,sha);accepted+=1
    queue.put('checks/'+request['request_id']+'.json',{'request_id':request['request_id'],
              'scenario_count':28,'native_scenarios':8,'control_checks':20,'mail_count':9,
              'smtp_accepted_this_run':accepted,'paid_ai_calls':0,'production_smtp_calls':0,
              'production_history_writes':0,'before_hashes':before,'after_hashes':snapshot(),
              'visual':visual,
              'code_commit':os.environ.get('GITHUB_SHA','unconfirmed')})
    print(json.dumps({'scenario_count':28,'smtp_accepted':accepted,'paid_ai_calls':0,
                      'production_smtp_calls':0,'production_history_writes':0}))


if __name__=='__main__':
    run({'operation':'scenario-suite','self_test':True,'request_id':'risk-audit-20261017-local','send':False})
