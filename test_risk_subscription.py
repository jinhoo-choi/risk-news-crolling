"""Native pipeline/SMTP/history integration using synthetic local fixtures only."""
import copy
import hashlib
import importlib
import json
import os
import smtplib
import tempfile
import unittest
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

from risk_queue import now, digest
from risk_subscription import consume, acknowledge, expire_and_check


ROOT = Path(__file__).resolve().parent


class Queue:
    def __init__(self, job, result):
        self.data = {'jobs/test.json': job, 'results/test.json': result, 'index.json': {'jobs': ['test']}}
        self.remote_seen = Path('seen_news.json').read_bytes()
        self.engine = 'subscription'
    def get(self, path):
        return copy.deepcopy(self.data.get(path)), digest(self.data[path]) if path in self.data else None
    def put(self, path, value, sha=None):
        if sha is not None and self.get(path)[1] != sha:
            raise RuntimeError('queue changed')
        self.data[path] = copy.deepcopy(value)
    def put_many(self, values, expected, deletions=()):
        if any(self.get(path)[1] != sha for path, sha in expected.items()):
            raise RuntimeError('queue changed')
        self.data.update(copy.deepcopy(values))
    def source(self, path):
        if path == 'risk_subscription_config.json':
            return json.dumps({'engine': self.engine, 'policy_version': 'risk-subscription-v1'}).encode(), 'sha'
        return (self.remote_seen if path == 'seen_news.json' else Path(path).read_bytes()), 'sha'


class SMTP:
    def __init__(self, fail_at=0, login_fail=False):
        self.calls = []
        self.fail_at, self.login_fail = fail_at, login_fail
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def ehlo(self):
        pass
    def login(self, *args):
        if self.login_fail:
            raise smtplib.SMTPAuthenticationError(535, b'failed')
    def sendmail(self, sender, recipients, raw):
        self.calls.append((recipients, raw))
        if len(self.calls) == self.fail_at:
            raise ConnectionError('uncertain DATA response')
        return {}
    def quit(self):
        pass
    def close(self):
        pass


class Integration(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.temporary = tempfile.TemporaryDirectory()
        os.chdir(self.temporary.name)
        Path('seen_news.json').write_text('{}')
        Path('exposure_data.csv').write_text('기준일,종목명,종목코드,종목유형,잔고(억),고객수,리스크종목,리스크고객수,리스크잔고(억)\n2026-10-14,테스트주식,000000,주식,150,1,아니오,0,0\n')
        Path('risk_policy.json').write_bytes((ROOT/'risk_policy.json').read_bytes())
        for name in ('filter_prompt.txt','action_prompt.txt'):
            Path(name).write_bytes((ROOT/name).read_bytes())
        for key in ('EMAIL_SENDER','EMAIL_PASSWORD','EMAIL_RECEIVER','ANTHROPIC_API_KEY','NAVER_CLIENT_ID','NAVER_CLIENT_SECRET'):
            os.environ[key] = 'fake'
        os.environ['EMAIL_SENDER'] = 'sender@example.invalid'
        os.environ['EMAIL_RECEIVER'] = 'group@example.invalid'
        os.environ['EMAIL_CC'] = 'cc@example.invalid'
        os.environ['NO_RESULT_RECEIVER'] = 'owner@example.invalid'
        os.environ['GITHUB_REF'] = 'refs/heads/main'
        import naver_news_monitor
        self.bot = importlib.reload(naver_news_monitor)
        self.post = patch.object(self.bot.requests, 'post', side_effect=AssertionError('paid inference attempted'))
        self.post.start()
        self.price = patch.object(self.bot, 'build_price_alert_section', lambda *a, **k: '')
        self.price.start()
        self.notice = patch.object(self.bot, 'load_competitor_notices', return_value=[])
        self.notice.start()
        self.before = Path('seen_news.json').read_bytes()
    def tearDown(self):
        self.post.stop(); self.price.stop(); self.notice.stop()
        os.chdir(self.previous)
        self.temporary.cleanup()
    def fixture(self, mode='production'):
        title = '한국투자증권 일부 MTS 잔고 조회 장애 발생'
        body = now().astimezone(self.bot.timezone(self.bot.timedelta(hours=9))).strftime('%Y년 %m월 %d일')+' 한국투자증권 일부 MTS 잔고 조회에서 장애가 발생했다.'
        item = {'article_id':'one','title':title,'description':'한국투자증권 일부 잔고 조회 장애 보도.',
                'desc':'한국투자증권 일부 잔고 조회 장애 보도.','url':'https://example.invalid/test/one',
                'published_at':now().isoformat(),'pubDate':format_datetime(now()),'keyword':'MTS 장애'}
        inputs = {'items':[item],'raw_articles':[item], 'history_hash':hashlib.sha256(self.before).hexdigest(),
                  'exposure_hash':hashlib.sha256(Path('exposure_data.csv').read_bytes()).hexdigest()}
        manifest = [{'article_id':'one','body':body,'body_status':'verified','source_url':item['url']}]
        ph = digest(json.loads(Path('risk_policy.json').read_text()))
        job = {'schema_version':1,'job_id':'test','mode':mode,'status':'pending','receipts':{},
               'expires_at':(now()+timedelta(minutes=30)).isoformat(), 'input':inputs,'input_hash':digest(inputs),
               'body_manifest':manifest,'body_manifest_hash':digest(manifest),'policy_hash':ph}
        analysis = {'entity':'한국투자증권','entities':['한국투자증권'],'related_stocks':[],
                    'event_type':'시스템장애','base_grade':'긴급','confidence':0.95,
                    'judgment':{'핵심사건':'일부 잔고 조회 장애','손실주체':'없음','당사연관':'당사이슈','확정여부':'확정'},
                    'fact_summary':'한국투자증권 일부 MTS 잔고 조회에 장애가 생겼다.',
                    'action':'한국투자증권 잔고 조회 범위와 복구 공지 확인.', 'customer_notice':None,
                    'evidence':[{'source_segment':'body','source_hash':hashlib.sha256(body.encode()).hexdigest(),
                                 'quote':'한국투자증권 일부 MTS 잔고 조회에서 장애가 발생했다'}],
                    'body_status':'verified','reviewed':True}
        result = {'schema_version':1,'job_id':'test','input_hash':digest(inputs),'policy_version':'risk-subscription-v1',
                  'policy_hash':ph,'body_manifest_hash':digest(manifest),'analysis_engine':'chatgpt_subscription',
                  'model_used':'unconfirmed','classifications':[{'article_id':'one','relevant':True,'reason':'본문에서 당사 일부 잔고 조회 장애 확인.'}],
                  'analyses':{'one':analysis}}
        return Queue(job, result)
    def run_consume(self, queue, smtp, self_test=False):
        with patch.object(smtplib, 'SMTP_SSL', lambda *a, **k: smtp):
            consume(queue, 'test', self_test)
    def test_production_native_recipients_and_ack_after_main_history(self):
        queue, smtp = self.fixture(), SMTP()
        self.run_consume(queue, smtp)
        self.assertEqual([c[0] for c in smtp.calls], [['sender@example.invalid'],['group@example.invalid'],['cc@example.invalid']])
        self.assertEqual(queue.data['jobs/test.json']['status'], 'delivery_complete')
        self.assertNotEqual(self.before, Path('seen_news.json').read_bytes())
        self.assertNotIn('모델 미확인', queue.data['previews/test.json']['html'])
        with self.assertRaises(RuntimeError):
            acknowledge(queue, 'test')
        queue.remote_seen = Path('seen_news.json').read_bytes()
        acknowledge(queue, 'test')
        self.assertEqual(queue.data['jobs/test.json']['status'], 'completed')
        self.assertTrue(queue.data['jobs/test.json']['ack'])
        self.assertEqual(queue.data['jobs/test.json']['paid_ai_calls'], 0)

    def grouped_fixture(self):
        queue = self.fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        item = copy.deepcopy(job['input']['items'][0])
        item.update(article_id='two', url='https://example.invalid/test/two',
                    title='한국투자증권 HTS 잔고 조회 장애 발생')
        job['input']['items'].append(item)
        job['input']['raw_articles'] = copy.deepcopy(job['input']['items'])
        body = copy.deepcopy(job['body_manifest'][0])
        body.update(article_id='two', source_url=item['url'])
        job['body_manifest'].append(body)
        job['event_grouping_version'] = 1
        result['analyses']['one']['confidence'] = 0.90
        result['analyses']['two'] = copy.deepcopy(result['analyses']['one'])
        result['analyses']['two']['confidence'] = 0.99
        result['classifications'].append({'article_id':'two','relevant':True,'reason':'같은 당일 조회 장애 보도.'})
        result['event_groups'] = [{'article_ids':['one','two'], 'reason':'동일 거래일 같은 잔고 조회 장애 반복 보도.', 'reviewed':True}]
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        return queue

    def test_grouped_native_mail_keeps_later_highest_score_and_delivered_history_only(self):
        queue, smtp = self.grouped_fixture(), SMTP()
        self.run_consume(queue, smtp)
        document = queue.data['previews/test.json']['html']
        self.assertIn('https://example.invalid/test/two', document)
        self.assertNotIn('https://example.invalid/test/one', document)
        final = queue.data['filter-logs/test.json']['final']
        self.assertEqual([a['article_id'] for a in final], ['two'])
        history = json.loads(Path('seen_news.json').read_text())
        self.assertNotIn(queue.data['jobs/test.json']['input']['items'][0]['title'],
                         json.dumps(history.get('_event_history', {}), ensure_ascii=False))
        self.assertEqual(len(smtp.calls), 3)

    def independent_same_company_fixture(self):
        queue = self.grouped_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        row, body = job['input']['items'][1], job['body_manifest'][1]
        date = now().astimezone(self.bot.timezone(self.bot.timedelta(hours=9))).strftime('%Y년 %m월 %d일')
        text = date+' 금융당국이 한국투자증권에 과태료 10억원을 부과했다.'
        row.update(title='한국투자증권, 금융당국 과태료 10억원 부과',description=text,desc=text)
        body.update(body=text)
        answer = result['analyses']['two']
        answer.update(event_type='금감원제재',fact_summary=text,
                      action='한국투자증권 과태료 부과 범위와 공시를 확인한다.',
                      judgment={'핵심사건':'과태료 부과','손실주체':'회사','당사연관':'당사이슈','확정여부':'확정'},
                      evidence=[{'source_segment':'body','source_hash':hashlib.sha256(text.encode()).hexdigest(),
                                 'quote':text}])
        result['event_groups'] = []  # Reviewed as separate incidents, despite the same company.
        job['input']['raw_articles'] = copy.deepcopy(job['input']['items'])
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        return queue

    def test_reviewed_independent_incidents_at_same_company_both_survive_native_mail(self):
        queue, smtp = self.independent_same_company_fixture(), SMTP()
        self.run_consume(queue,smtp)
        final = queue.data['filter-logs/test.json']['final']
        self.assertEqual({a['article_id'] for a in final},{'one','two'})
        self.assertIn('2건 선별',queue.data['previews/test.json']['html'])

    def test_summary_can_use_verified_body_fact_absent_from_headline(self):
        queue, smtp = self.fixture(), SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        job['input']['items'][0]['title'] = '한국투자증권 고객 불편, 어떤 일이 있었나'
        job['input']['raw_articles'] = copy.deepcopy(job['input']['items'])
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        self.run_consume(queue, smtp)
        final = queue.data['filter-logs/test.json']['final']
        core = result['analyses']['one']['judgment']['핵심사건']
        self.assertNotIn(core, final[0]['title'])
        self.assertIn('한국투자증권 일부 MTS 잔고 조회에서 장애가 발생했다', final[0]['body'])
        self.assertIn('💡 한국투자증권 '+core, queue.data['previews/test.json']['html'])
        self.assertEqual(queue.data['metrics/test.json']['summary_source_ids'], ['one'])

    def test_summary_excludes_same_event_loser_after_final_score_selection(self):
        queue, smtp = self.grouped_fixture(), SMTP()
        result = queue.data['results/test.json']
        result['analyses']['one']['judgment']['핵심사건'] = '일부 잔고 조회 장애'
        result['analyses']['two']['judgment']['핵심사건'] = '잔고 조회 장애'
        self.run_consume(queue, smtp)
        document = queue.data['previews/test.json']['html']
        self.assertEqual([a['article_id'] for a in queue.data['filter-logs/test.json']['final']], ['two'])
        self.assertIn('💡 한국투자증권 잔고 조회 장애', document)
        self.assertNotIn('💡 한국투자증권 일부 잔고 조회 장애', document)
        self.assertEqual(queue.data['metrics/test.json']['summary_source_ids'], ['two'])

    def test_summary_excludes_rejected_analysis_candidate(self):
        queue, smtp = self.independent_same_company_fixture(), SMTP()
        result = queue.data['results/test.json']
        result['classifications'][1].update(relevant=False, reason='검수용 비해당 후보.')
        del result['analyses']['two']
        self.run_consume(queue, smtp)
        document = queue.data['previews/test.json']['html']
        self.assertIn('💡 한국투자증권 일부 잔고 조회 장애', document)
        self.assertNotIn('과태료 부과', document)
        self.assertEqual([a['article_id'] for a in queue.data['filter-logs/test.json']['final']], ['one'])
        self.assertEqual(queue.data['metrics/test.json']['summary_source_ids'], ['one'])

    def test_summary_excludes_positive_article_removed_by_owner(self):
        queue, smtp = self.independent_same_company_fixture(), SMTP()
        source = queue.source
        def exclude_second(path):
            raw, sha = source(path)
            if path == 'risk_subscription_config.json':
                config = json.loads(raw)
                config['excluded_article_ids'] = ['two']
                raw = json.dumps(config).encode()
            return raw, sha
        queue.source = exclude_second
        self.run_consume(queue, smtp)
        document = queue.data['previews/test.json']['html']
        self.assertIn('💡 한국투자증권 일부 잔고 조회 장애', document)
        self.assertNotIn('과태료 부과', document)
        self.assertTrue(queue.data['results/test.json']['classifications'][1]['relevant'])
        self.assertEqual(queue.data['metrics/test.json']['summary_source_ids'], ['one'])

    def test_summary_does_not_keep_previous_selection_or_overflow_40_chars(self):
        from risk_provider import Provider
        queue = self.independent_same_company_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        result['analyses']['one']['judgment']['핵심사건'] = '일부 MTS 잔고 조회 장애 발생 및 복구 점검 진행'
        provider = Provider(job, result)
        first, second = job['input']['items']
        rows = [dict(first, entity='한국투자증권'), dict(second, entity='한국투자증권')]
        headline = provider.response('summary', articles=rows).text
        self.assertLessEqual(len(headline), 40)
        self.assertIn('복구 점검 진행', headline)
        self.assertNotIn('과태료', headline)
        self.assertEqual(provider.summary_source_ids, ['one'])
        self.assertEqual(provider.response('summary', articles=rows[1:]).text, '한국투자증권 과태료 부과')
        self.assertEqual(provider.summary_source_ids, ['two'])
        self.assertEqual(provider.response('summary', articles=[]).text, '')
        self.assertEqual(provider.summary_source_ids, [])

    def test_summary_failure_omits_header_without_stale_fallback(self):
        from risk_provider import Provider
        queue, smtp = self.fixture(), SMTP()
        native = Provider.response
        def fail_summary(provider, route, *args, **kwargs):
            if route == 'summary':
                raise RuntimeError('검수용 요약 실패')
            return native(provider, route, *args, **kwargs)
        with patch.object(Provider, 'response', fail_summary):
            self.run_consume(queue, smtp)
        document = queue.data['previews/test.json']['html']
        self.assertNotIn('💡', document)
        self.assertEqual(queue.data['metrics/test.json']['summary_source_ids'], [])
        self.assertIn('https://example.invalid/test/one', document)
        self.assertEqual(len(smtp.calls), 3)

    def test_invalid_or_missing_event_comparison_blocks_before_smtp(self):
        cases = ['missing','unknown','overlap','mixed','unverified']
        for case in cases:
            with self.subTest(case=case):
                queue, smtp = self.grouped_fixture(), SMTP()
                result = queue.data['results/test.json']
                if case == 'missing':
                    del result['event_groups']
                elif case == 'unknown':
                    result['event_groups'][0]['article_ids'][1] = 'unknown'
                elif case == 'overlap':
                    result['event_groups'] *= 2
                elif case == 'mixed':
                    result['analyses']['two']['event_type'] = '횡령배임'
                else:
                    result['analyses']['two']['body_status'] = 'missing'
                with self.assertRaises(ValueError):
                    self.run_consume(queue, smtp)
                self.assertEqual(smtp.calls, [])
                self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_event_score_selection_crosses_entities_and_preserves_independent_risks(self):
        a = {'entity':'네비우스','grade':'주의','_risk_score':4.0,'url':'https://example.invalid/a', '_subscription_event_group':'0'}
        b = dict(a, entity='샌디스크', _risk_score=6.0, url='https://example.invalid/b')
        independent = dict(a, entity='', grade='긴급', event_type='반대매매', _risk_score=8.0)
        independent.pop('_subscription_event_group')
        separate = dict(b, entity='마벨', _subscription_event_group='1')
        self.assertEqual(self.bot.dedup_same_mail_events([a,b,independent,separate]), [b,independent,separate])
        # Equal final/display/raw/media scores preserve original order.
        a['_risk_score'] = b['_risk_score']
        self.assertEqual(self.bot.dedup_same_mail_events([a,b]), [a])

    def test_market_statistics_cannot_open_group_route_by_score_or_grade(self):
        statistic = {'entity':'', 'entities':[], 'event_type':'반대매매',
                     'grade':'긴급', '_risk_score':9.9, '_conf_raw':0.99,
                     '_subscription_stat_series':'market:금투협:미수금반대매매'}
        self.assertTrue(self.bot.decide_send_scope([statistic], {})['self_only'])
        self.assertEqual(self.bot.filter_articles_for_scope([statistic], {}, False), [])
        direct = dict(statistic, entity='한국투자증권', event_type='시스템장애')
        direct.pop('_subscription_stat_series')
        self.assertFalse(self.bot.decide_send_scope([statistic, direct], {})['self_only'])
        self.assertEqual(self.bot.filter_articles_for_scope([statistic, direct], {}, False), [direct])
        # Live portfolio crash is an independent gate, never replaced by a headline.
        with patch.object(self.bot.build_price_alert_section, 'last_alerted_count',
                          self.bot.MARKET_CRASH_STOCK_THRESHOLD, create=True), \
             patch.object(self.bot.build_price_alert_section, 'last_alerted_rbal',
                          self.bot.MARKET_CRASH_RBAL_THRESHOLD, create=True):
            scope = self.bot.decide_send_scope([statistic], {})
            self.assertFalse(scope['self_only'])
            self.assertTrue(scope['market_crash'])
            self.assertEqual(self.bot.filter_articles_for_scope([statistic], {}, False, True), [statistic])

    def test_market_statistics_other_urls_do_not_resend_same_reference_day(self):
        queue, smtp = self.market_statistics_fixture(), SMTP()
        self.run_consume(queue, smtp)
        queue.remote_seen = Path('seen_news.json').read_bytes()
        acknowledge(queue, 'test')
        queue, smtp = self.market_statistics_fixture(), SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        job['input']['history_hash'] = hashlib.sha256(queue.remote_seen).hexdigest()
        for key in ('items', 'raw_articles'):
            for item in job['input'][key]:
                item['url'] += '-another-publisher'
                item['title'] = '증시 대기자금 감소…강제청산 계속 ' + item['article_id']
        for body in job['body_manifest']:
            body['source_url'] += '-another-publisher'
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        self.run_consume(queue, smtp)
        self.assertEqual(queue.data['filter-logs/test.json']['final'], [])
        self.assertNotIn('another-publisher', queue.data['previews/test.json']['html'])
        self.assertTrue(queue.data['outbox/test.json']['self_only'])
        self.assertEqual(len(smtp.calls), 1)  # Owner status; no repeated statistic/group mail.

    def market_statistics_fixture(self):
        queue = self.grouped_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        item, body = copy.deepcopy(job['input']['items'][0]), copy.deepcopy(job['body_manifest'][0])
        analysis = copy.deepcopy(result['analyses']['one'])
        job['input']['items'], job['body_manifest'] = [], []
        result['classifications'], result['analyses'] = [], {}
        for aid, day in [('one',14), ('two',14), ('three',15), ('four',15)]:
            quote = f'지난 {day}일 위탁매매 미수금 대비 실제 반대매매 금액은 804억원으로 집계됐다.'
            text = '금융투자협회에 따르면 '+quote
            row = dict(item, article_id=aid, title=f'빚투 반대매매 급증 {aid}',
                       url='https://example.invalid/test/'+aid,
                       description=text, desc=text,
                       pubDate='Fri, 16 Oct 2026 17:37:00 +0900')
            job['input']['items'].append(row)
            job['body_manifest'].append(dict(body, article_id=aid, body=text,
                source_url=row['url'], body_hash=hashlib.sha256(text.encode()).hexdigest()))
            answer = copy.deepcopy(analysis)
            answer.update(entity='', entities=[], event_type='반대매매', confidence=0.96,
                          fact_summary=f'{day}일 미수금 반대매매가 804억원으로 집계됐다.',
                          action='시장 강제처분 실적과 보유 주식 평가손을 확인한다.', customer_notice=None,
                          judgment={'핵심사건':'반대매매','손실주체':'주주','당사연관':'무관','확정여부':'확정'},
                          evidence=[{'source_segment':'body','source_hash':hashlib.sha256(text.encode()).hexdigest(),
                                     'quote':quote}])
            result['classifications'].append({'article_id':aid,'relevant':True,'reason':'시장 실제 반대매매 집계.'})
            result['analyses'][aid] = answer
        job['input']['raw_articles'] = copy.deepcopy(job['input']['items'])
        result['event_groups'] = [{'article_ids':ids,'reason':'같은 집계일 통계 반복 보도.','reviewed':True}
                                  for ids in [['one','two'],['three','four']]]
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        return queue

    def test_successive_market_statistics_render_one_card_and_record_only_winner(self):
        for missing_groups in (False, True):
            with self.subTest(missing_groups=missing_groups):
                Path('seen_news.json').write_bytes(self.before)
                queue, smtp = self.market_statistics_fixture(), SMTP()
                if missing_groups:
                    queue.data['results/test.json']['event_groups'] = []
                self.run_consume(queue, smtp)
                final = queue.data['filter-logs/test.json']['final']
                self.assertEqual([a['article_id'] for a in final], ['three'])
                document = queue.data['previews/test.json']['html']
                self.assertIn('https://example.invalid/test/three', document)
                for aid in ('one','two','four'):
                    self.assertNotIn('https://example.invalid/test/'+aid, document)
                self.assertIn('1건 선별', document)
                self.assertEqual(len(smtp.calls), 1)
                self.assertTrue(queue.data['outbox/test.json']['self_only'])
                self.assertIn('15일 미수금 반대매매가 804억원', document)
                queue.remote_seen = Path('seen_news.json').read_bytes()
                acknowledge(queue, 'test')
                self.assertTrue(queue.data['jobs/test.json']['ack'])

    def test_market_statistics_higher_score_precedes_newer_source_date(self):
        queue, smtp = self.market_statistics_fixture(), SMTP()
        queue.data['results/test.json']['analyses']['one']['confidence'] = 0.99
        self.run_consume(queue, smtp)
        self.assertEqual([a['article_id'] for a in queue.data['filter-logs/test.json']['final']], ['one'])

    def partial_market_statistics_fixture(self):
        queue = self.market_statistics_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        # Same reviewed statistic, but only the first member has a body quote
        # with the amount. The second member cites its collected description.
        answer = result['analyses']['two']
        description = job['input']['items'][1]['description']
        answer['evidence'] = [{'source_segment':'description',
                              'source_hash':hashlib.sha256(description.encode()).hexdigest(),
                              'quote':description}]
        return queue

    def test_partial_reviewed_statistics_render_two_to_one_and_save_only_winner(self):
        queue, smtp = self.partial_market_statistics_fixture(), SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        # Isolate the two-member group: the formerly split member wins by score.
        for key in ('items','raw_articles'):
            job['input'][key] = job['input'][key][:2]
        job['body_manifest'] = job['body_manifest'][:2]
        result['classifications'] = result['classifications'][:2]
        result['analyses'] = {aid:result['analyses'][aid] for aid in ('one','two')}
        result['event_groups'] = result['event_groups'][:1]
        result['analyses']['two']['confidence'] = 0.99
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        self.run_consume(queue, smtp)
        final = queue.data['filter-logs/test.json']['final']
        self.assertEqual([a['article_id'] for a in final], ['two'])
        document = queue.data['previews/test.json']['html']
        self.assertIn('https://example.invalid/test/two', document)
        self.assertNotIn('https://example.invalid/test/one', document)
        self.assertIn('1건 선별', document)
        seen = json.loads(Path('seen_news.json').read_text())
        self.assertNotIn(job['input']['items'][0]['title'],
                         json.dumps(seen.get('_event_history', {}), ensure_ascii=False))
        self.assertEqual(len(smtp.calls), 1)
        queue.remote_seen = Path('seen_news.json').read_bytes()
        acknowledge(queue, 'test')
        self.assertTrue(queue.data['jobs/test.json']['ack'])

    def test_partial_reviewed_statistics_union_all_groups_without_losing_members(self):
        from risk_provider import Provider
        queue = self.partial_market_statistics_fixture()
        provider = Provider(queue.data['jobs/test.json'], queue.data['results/test.json'])
        self.assertEqual(set(provider.event_groups), {'one','two','three','four'})
        self.assertEqual(len(set(provider.event_groups.values())), 1)
        self.assertNotIn('two', provider.stat_dates)  # Never borrow another article's date.

    def test_reviewed_statistics_with_no_automatic_matches_keep_original_groups(self):
        from risk_provider import Provider
        queue = self.market_statistics_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        for answer in result['analyses'].values():
            answer['evidence'][0]['source_segment'] = 'description'
        provider = Provider(job, result)
        self.assertEqual(provider.event_groups, {'one':'0','two':'0','three':'1','four':'1'})
        self.assertEqual(provider.stat_dates, {})

    def test_partial_group_cannot_bridge_other_companies_risks_or_statistics(self):
        from risk_provider import Provider
        for case in ('company','risk','statistic','forecast'):
            queue = self.partial_market_statistics_fixture()
            job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
            answer = result['analyses']['two']
            if case == 'company':
                answer.update(entity='검수기업', entities=['검수기업'])
            elif case == 'risk':
                answer['event_type'] = '유동성위기'
            elif case == 'statistic':
                answer['fact_summary'] = '신용융자 반대매매가 증가했다.'
            else:
                answer['judgment']['확정여부'] = '가능성'
            with self.subTest(case=case):
                provider = Provider(job, result)
                self.assertEqual(provider.event_groups['one'], provider.event_groups['two'])
                self.assertNotEqual(provider.event_groups['one'], provider.event_groups['three'])

    def test_partial_group_final_score_then_source_date_then_input_order(self):
        from risk_provider import Provider
        queue = self.partial_market_statistics_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        provider = Provider(job, result)
        rows = copy.deepcopy(job['input']['items'])
        for row in rows:
            provider.attach(row)
            row.update(grade='주의', _risk_score=6.0)
        self.assertEqual(self.bot.dedup_same_mail_events(rows), [rows[2]])
        # A newer publication date alone cannot supply a statistic reference date.
        rows[1]['pubDate'] = 'Sat, 17 Oct 2026 17:37:00 +0900'
        self.assertEqual(self.bot.dedup_same_mail_events(rows), [rows[2]])
        rows[1]['_risk_score'] = 6.1
        self.assertEqual(self.bot.dedup_same_mail_events(rows), [rows[1]])
        rows[1]['_risk_score'] = 6.0
        self.assertEqual(self.bot.dedup_same_mail_events(rows[3:]+rows[:3]), [rows[3]])

    def test_market_statistic_guard_does_not_merge_other_risks_or_invent_source_dates(self):
        from risk_provider import Provider
        for case in ('named','credit','forecast','missing','other_statistic','no_source_amount','no_authority'):
            queue = self.market_statistics_fixture()
            job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
            result['event_groups'] = []
            answer, body = result['analyses']['one'], job['body_manifest'][0]
            if case == 'named':
                answer.update(entity='가상기업', entities=['가상기업'])
            elif case == 'credit':
                answer['event_type'] = '유동성위기'
            elif case == 'forecast':
                answer['judgment']['확정여부'] = '가능성'
            elif case == 'missing':
                answer['body_status'] = 'missing'
            elif case == 'other_statistic':
                answer['fact_summary'] = '신용융자 반대매매가 증가했다.'
            elif case == 'no_source_amount':
                answer['evidence'][0]['quote'] = '금융투자협회에 따르면'
            else:
                body['body'] = body['body'].replace('금융투자협회', '다른 기관')
            with self.subTest(case=case):
                provider = Provider(job, result)
                self.assertNotIn('one', provider.stat_dates)
                self.assertNotIn('one', provider.event_groups)
        queue = self.market_statistics_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        result['analyses']['one']['evidence'][0]['quote'] = '804억원으로 집계됐다.'
        provider = Provider(job, result)
        self.assertEqual(provider.stat_dates['one'], '')
        self.assertEqual(provider.stat_dates['three'], '2026-10-15')

    def test_duplicate_candidates_use_one_grade_slot_without_displacing_other_events(self):
        rows, exposure = [], {}
        for i in range(5):
            entity = '검수기업'+chr(65+i)
            article = {'entity':entity, 'entities':[entity], 'title':entity+' 신규 사업 부진',
                       'url':'https://example.invalid/'+str(i), 'grade':'주의',
                       '_ai_confidence':0.84, 'event_type':'기타리스크'}
            if i < 3:
                article['_subscription_event_group'] = '0'
            rows.append(article)
            exposure[entity] = [{'종목명':entity,'종목유형':'주식','잔고(억)':'150','고객수':'1'}]
        with patch.object(self.bot, 'get_price_change', return_value=None):
            graded = self.bot.regrade_by_score(rows, exposure)
        final = self.bot.dedup_same_mail_events(graded)
        self.assertEqual(len(final), 3)
        self.assertTrue(all(a['grade'] == '주의' for a in final))
        self.assertEqual({a['entity'] for a in final}, {'검수기업A','검수기업D','검수기업E'})

    def test_owner_pipeline_preserves_production_history(self):
        queue, smtp = self.fixture('verification'), SMTP()
        self.run_consume(queue, smtp, True)
        self.assertEqual(smtp.calls[0][0], ['111715@koreainvestment.com'])
        self.assertEqual(len(smtp.calls), 1)
        self.assertEqual(self.before, Path('seen_news.json').read_bytes())
        self.assertEqual(queue.data['jobs/test.json']['status'], 'verified')
    def test_uncertain_data_never_retries_or_saves_history(self):
        queue, smtp = self.fixture(), SMTP(fail_at=1)
        with self.assertRaises(RuntimeError): self.run_consume(queue, smtp)
        self.assertEqual(len(smtp.calls), 1)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'uncertain')
        self.assertEqual(self.before, Path('seen_news.json').read_bytes())
        with self.assertRaises(RuntimeError): self.run_consume(queue, smtp)
        self.assertEqual(len(smtp.calls), 1)
    def test_partial_delivery_never_acks_or_writes_history(self):
        queue, smtp = self.fixture(), SMTP(fail_at=2)
        with self.assertRaises(RuntimeError): self.run_consume(queue, smtp)
        self.assertEqual(len(smtp.calls), 2)
        self.assertEqual(set(queue.data['jobs/test.json']['receipts'].values()), {'accepted','uncertain'})
        self.assertEqual(self.before, Path('seen_news.json').read_bytes())
        with self.assertRaises(RuntimeError): acknowledge(queue, 'test')
    def test_login_failure_with_zero_data_returns_pending(self):
        queue, smtp = self.fixture(), SMTP(login_fail=True)
        with self.assertRaises(smtplib.SMTPAuthenticationError): self.run_consume(queue, smtp)
        self.assertEqual(smtp.calls, [])
        self.assertEqual(queue.data['jobs/test.json']['status'], 'pending')
        self.assertEqual(queue.data['jobs/test.json']['receipts'], {})
    def test_expiration_cleanup_never_unlocks_uncertain(self):
        queue = self.fixture()
        queue.data['jobs/test.json']['expires_at'] = (now()-timedelta(seconds=1)).isoformat()
        with patch('risk_subscription.notify_error'):
            expire_and_check(queue)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'expired')
        queue.data['jobs/test.json']['status'] = 'uncertain'
        with self.assertRaises(RuntimeError): expire_and_check(queue)

    def test_body_missing_holds_delivery_and_seen_history(self):
        queue, smtp = self.fixture(), SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        body = job['body_manifest'][0]
        body.update(body='', body_status='missing')
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        a = result['analyses']['one']
        title = job['input']['items'][0]['title']
        a.update(base_grade='참고',confidence=0.6,body_status='missing',
                 fact_summary='[본문 미수집] 한국투자증권 일부 잔고 조회 장애 보도.',
                 evidence=[{'source_segment':'title','source_hash':hashlib.sha256(title.encode()).hexdigest(),'quote':title}])
        with self.assertRaisesRegex(RuntimeError, '본문 검증 미완료'):
            self.run_consume(queue, smtp)
        self.assertEqual(smtp.calls, [])
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'pending')

    def test_price_diagnostics_use_fixed_owner_without_receipts(self):
        smtp = SMTP()
        self.bot.NO_RESULT_RECEIVER = 'executive@example.invalid'
        self.bot._OWNER_WARNINGS.add('가격 조회 미완료 2종목')
        with patch.object(smtplib, 'SMTP_SSL', lambda *a, **k: smtp):
            self.bot.flush_owner_warnings()
            self.bot.flush_owner_warnings()
            self.bot._notify_partial_failure('offline', {'exec@example.invalid': (550, b'refused')}, 1)
        self.assertEqual([c[0] for c in smtp.calls], [['myboy3743@gmail.com']] * 2)
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        from email import message_from_string
        from email.utils import parseaddr
        for _, raw in smtp.calls:
            message = message_from_string(raw)
            self.assertEqual(parseaddr(message['To'])[1], 'myboy3743@gmail.com')
            self.assertIsNone(message['Cc'])
            self.assertIsNone(message['Bcc'])

    def test_terminal_request_is_noop_without_smtp_or_history_write(self):
        queue=self.fixture('verification')
        queue.data['jobs/test.json']['status']='verified'
        queue.data['jobs/test.json']['receipts']={'owner':'accepted'}
        with patch('risk_subscription.verify_job',side_effect=AssertionError('terminal reanalysis')):
            consume(queue,'test',self_test=True)
        self.assertEqual(Path('seen_news.json').read_bytes(),self.before)
        self.assertEqual(queue.data['jobs/test.json']['receipts'],{'owner':'accepted'})

    def test_joint_subject_without_source_quote_is_rejected(self):
        from risk_result_validator import validate
        queue=self.fixture()
        job=queue.data['jobs/test.json'];result=queue.data['results/test.json']
        a=result['analyses']['one'];a['entities'].append('배경계열사')
        a['direct_event_entities']=['한국투자증권','배경계열사']
        with self.assertRaisesRegex(ValueError,'직접 사건 주체의 본문 인용 누락'):
            validate(result,job,job['input'],job['body_manifest'])

    def test_incomplete_result_blocks_smtp(self):
        queue, smtp = self.fixture(), SMTP()
        queue.data['results/test.json']['classifications'] = []
        with self.assertRaises(ValueError): self.run_consume(queue, smtp)
        self.assertEqual(smtp.calls, [])
        self.assertEqual(queue.data['jobs/test.json']['status'], 'pending')

    def test_paid_fallback_blocked_without_provider(self):
        with patch.dict(os.environ, {'RISK_ENGINE':'subscription'}):
            self.bot.RISK_PROVIDER = None
            with self.assertRaises(RuntimeError):
                self.bot._ai_post('https://api.anthropic.com/v1/messages', _route='classify', json={})


class BodyExtraction(unittest.TestCase):
    def test_news1_embedded_public_article_text_excludes_images_and_ads(self):
        from unittest.mock import MagicMock
        import risk_subscription as bridge
        bot = bridge.import_bot()
        state = {'props':{'pageProps':{'articleView':{'contentArrange':[
            {'type':'image','content':'사진 설명'}, {'type':'div','content':'광고'},
            {'type':'text','content':'<p>실제 사건 첫 문단.</p>'},
            {'type':'text','content':'<p>뒤쪽 HPE 급락 사실 문단.</p>'}]}}}}
        response = MagicMock()
        response.headers = {}
        response.text = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(state) + '</script><div id="articleBodyContent">사진 설명</div>'
        with patch.object(bot.requests.Session, 'get', return_value=response):
            self.assertEqual(bot.fetch_article_body('https://www.news1.kr/world/123'),
                             '실제 사건 첫 문단. 뒤쪽 HPE 급락 사실 문단.')

    def test_news1_malformed_state_falls_back_to_article_container(self):
        from unittest.mock import MagicMock
        import risk_subscription as bridge
        bot = bridge.import_bot()
        response = MagicMock()
        response.headers = {}
        response.text = '<script id="__NEXT_DATA__">invalid</script><div id="articleBodyContent">확인된 기사 본문</div>'
        with patch.object(bot.requests.Session, 'get', return_value=response):
            self.assertEqual(bot.fetch_article_body('https://www.news1.kr/world/123'), '확인된 기사 본문')

    def test_image_only_article_promotion_is_not_body(self):
        from unittest.mock import MagicMock
        import risk_subscription as bridge
        bot = bridge.import_bot()
        response = MagicMock()
        response.headers = {}
        promotion = '<p>Google 검색에서 매일경제 기사를 더 자주 볼 수 있습니다.</p>'
        response.text = '<article><img src="table.jpg">' + promotion + '</article>'
        with patch.object(bot.requests.Session, 'get', return_value=response):
            self.assertEqual(bot.fetch_article_body('https://example.invalid/table'), '')
        response.text = '<article>' + promotion + '<p>예시기업이 공시를 통해 납입 기한 변경을 발표했다.</p></article>'
        with patch.object(bot.requests.Session, 'get', return_value=response):
            self.assertEqual(bot.fetch_article_body('https://example.invalid/news'),
                             '예시기업이 공시를 통해 납입 기한 변경을 발표했다.')

    def test_article_containers_keep_late_facts_and_exclude_footer(self):
        from unittest.mock import MagicMock
        import risk_subscription as bridge
        bot = bridge.import_bot()
        text = '대상 회사의 자금 납입 일정과 실제 사건 문단을 확인한다. ' * 70 + '마지막 직접 주체: 예시기업'
        for opening, closing in [('<div id="newsView">','</div>'),
                                  ('<div class="vtxt detailCont">','</div>'),
                                  ('<div class="article-view__body">','</div>')]:
            response = MagicMock()
            response.headers = {}
            response.text = '<nav>관련 기사 메뉴</nav>' + opening + text + closing + '<footer><p>고객센터 연락처와 저작권 정보</p></footer>'
            with self.subTest(container=opening), patch.dict(os.environ, RISK_ENGINE='subscription'), \
                    patch.object(bot.requests.Session, 'get', return_value=response):
                body = bot.fetch_article_body('https://example.invalid/article')
                self.assertIn('마지막 직접 주체: 예시기업', body)
                self.assertNotIn('저작권', body)
                self.assertGreater(len(body), 600)
            with patch.dict(os.environ, RISK_ENGINE='api'), patch.object(bot.requests.Session, 'get', return_value=response):
                self.assertEqual(len(bot.fetch_article_body('https://example.invalid/article')), 600)


if __name__ == '__main__':
    unittest.main()
