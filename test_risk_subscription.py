"""Native pipeline/SMTP/history integration using synthetic local fixtures only."""
import copy
import hashlib
import importlib
import json
import os
import smtplib
import tempfile
import unittest
from datetime import timedelta, timezone
from email import message_from_bytes
from email.utils import format_datetime, getaddresses
from pathlib import Path
from unittest.mock import Mock, patch

from risk_delivery import address_key
from risk_queue import now, digest
from risk_subscription import consume, acknowledge, expire_and_check


ROOT = Path(__file__).resolve().parent


class Queue:
    def __init__(self, job, result):
        self.data = {'jobs/test.json': job, 'results/test.json': result, 'index.json': {'jobs': ['test']}}
        self.remote_seen = Path('seen_news.json').read_bytes()
        self.engine = 'subscription'
        self.commits = {}
    @staticmethod
    def blob_sha(value):
        raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()
        return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    def get(self, path, ref=None):
        data = self.data if ref is None else self.commits[ref]
        return copy.deepcopy(data.get(path)), self.blob_sha(data[path]) if path in data else None
    def put(self, path, value, sha=None):
        if sha is not None and self.get(path)[1] != sha:
            raise RuntimeError('queue changed')
        self.data[path] = copy.deepcopy(value)
    def put_many(self, values, expected, deletions=()):
        if any((self.blob_sha(self.data[path]) if path in self.data else None) != sha
               for path, sha in expected.items()):
            raise RuntimeError('queue changed')
        self.data.update(copy.deepcopy(values))
        commit = hashlib.sha1(json.dumps(self.data, sort_keys=True).encode()).hexdigest()
        self.commits[commit] = copy.deepcopy(self.data)
        return commit
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
        # A normal synthetic balance must not expire as the calendar advances.
        ref_date = now().astimezone(timezone(timedelta(hours=9))).date().isoformat()
        Path('exposure_data.csv').write_text('기준일,종목명,종목코드,종목유형,잔고(억),고객수,리스크종목,리스크고객수,리스크잔고(억)\n'+ref_date+',테스트주식,000000,주식,150,1,아니오,0,0\n')
        Path('risk_policy.json').write_bytes((ROOT/'risk_policy.json').read_bytes())
        for name in ('filter_prompt.txt','action_prompt.txt'):
            Path(name).write_bytes((ROOT/name).read_bytes())
        for key in ('EMAIL_SENDER','EMAIL_PASSWORD','EMAIL_RECEIVER','ANTHROPIC_API_KEY','NAVER_CLIENT_ID','NAVER_CLIENT_SECRET'):
            os.environ[key] = 'fake'
        os.environ['EMAIL_SENDER'] = 'sender@example.invalid'
        # Keep the three-target recovery fixture explicit, independent of SMTP login.
        os.environ['EMAIL_RECEIVER'] = 'sender@example.invalid,group@example.invalid'
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

    def test_retention_runs_only_after_ack_and_failure_preserves_delivery(self):
        queue, smtp = self.fixture(), SMTP()
        Path('risk_subscription_config.json').write_text('{"input_retention_days": 9}')
        def maintenance(*args):
            self.assertEqual(args[1], 9)
            state = queue.data['jobs/test.json']
            self.assertEqual(state['status'], 'completed')
            self.assertTrue(state['history_saved'] and state['ack'])
            self.assertTrue(all(v == 'accepted' for v in state['receipts'].values()))
            raise RuntimeError('retention unavailable')
        with patch('risk_subscription.prune_terminal_inputs', side_effect=maintenance) as prune:
            self.run_consume(queue, smtp)
            prune.assert_not_called()
            with self.assertRaises(RuntimeError):
                acknowledge(queue, 'test')
            prune.assert_not_called()
            queue.remote_seen = Path('seen_news.json').read_bytes()
            before = copy.deepcopy(queue.data['jobs/test.json']['receipts'])
            sent = len(smtp.calls)
            acknowledge(queue, 'test')
            prune.assert_called_once()
        self.assertEqual(queue.data['jobs/test.json']['status'], 'completed')
        self.assertEqual(queue.data['jobs/test.json']['receipts'], before)
        self.assertEqual(len(smtp.calls), sent)

    def test_explicit_data_rejection_has_recoverable_evidence(self):
        queue, smtp, to, cc = self.executive_fixture()
        smtp.data.return_value = (451, b'temporary refusal')
        with self.assertRaises(RuntimeError):
            self.run_consume(queue, smtp)
        job = queue.data['jobs/test.json']
        self.assertEqual(job['status'], 'delivery_rejected')
        self.assertEqual(job['receipts'], dict.fromkeys(map(address_key, to + cc), 'rejected'))
        self.assertEqual(job['smtp_evidence']['attempts'][0]['outcomes'][address_key(to[0])]['code'], 451)
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_missing_mts_body_cannot_be_excluded_without_source_evidence(self):
        from risk_result_validator import validate
        queue = self.fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        job['exclusion_review_version'] = 1
        job['body_manifest'][0].update(body='', body_status='missing')
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        result['classifications'][0].update(relevant=False, reason='일반 홍보로 판정해 제외합니다.')
        result['analyses'] = {}
        with self.assertRaisesRegex(ValueError, '제외'):
            validate(result, job, job['input'], job['body_manifest'])

    def executive_fixture(self):
        queue = self.fixture()
        queue.data['jobs/test.json']['report_layout_version'] = 1
        to = [f'leader{i}@example.invalid' for i in range(3)]
        cc = [f'list{i}@example.invalid' for i in range(2)]
        self.bot.EMAIL_RECEIVERS, self.bot.EMAIL_CC = cc[:1], cc[1:] + to
        roles = {'to': list(map(address_key, to)), 'cc': list(map(address_key, cc))}
        source = queue.source
        def configured(path):
            raw, sha = source(path)
            if path == 'risk_subscription_config.json':
                raw = json.dumps(dict(json.loads(raw), report_recipient_keys=roles)).encode()
            return raw, sha
        queue.source = configured
        smtp = Mock()
        smtp.mail.return_value = smtp.rcpt.return_value = smtp.data.return_value = (250, b'OK')
        return queue, smtp, to, cc

    def test_executives_to_groups_cc_one_data_after_safe_mail_reconnect(self):
        queue, smtp, to, cc = self.executive_fixture()
        smtp.mail.side_effect = [smtplib.SMTPServerDisconnected('before MAIL reply'), (250, b'OK')]
        self.run_consume(queue, smtp)
        self.assertEqual(smtp.login.call_count, 2)
        self.assertEqual([c.args[0] for c in smtp.rcpt.call_args_list], to + cc)
        smtp.data.assert_called_once()
        msg = message_from_bytes(smtp.data.call_args.args[0])
        for key, expected in [('To', to), ('Cc', cc)]:
            self.assertEqual([a for _, a in getaddresses(msg.get_all(key))], expected)
        self.assertIsNone(msg['Bcc'])
        self.assertEqual(getaddresses(msg.get_all('From'))[0][1], self.bot.EMAIL_SENDER)
        self.assertEqual(queue.data['jobs/test.json']['receipts'], dict.fromkeys(map(address_key, to + cc), 'accepted'))
        queue.remote_seen = Path('seen_news.json').read_bytes()
        acknowledge(queue, 'test')
        self.assertTrue(queue.data['jobs/test.json']['ack'])

    def test_executive_group_partial_rcpt_refusal_preserves_four_receipts(self):
        queue, smtp, to, cc = self.executive_fixture()
        smtp.rcpt.side_effect = [(550 if a == to[1] else 250, b'response') for a in to + cc]
        with self.assertRaisesRegex(RuntimeError, '거부'):
            self.run_consume(queue, smtp)
        smtp.data.assert_called_once()
        states = queue.data['jobs/test.json']['receipts']
        self.assertEqual(states[address_key(to[1])], 'rejected')
        self.assertEqual(list(states.values()).count('accepted'), 4)
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        with self.assertRaises(RuntimeError):
            acknowledge(queue, 'test')

    def test_executive_group_data_disconnect_locks_all_and_prevents_resend(self):
        queue, smtp, to, cc = self.executive_fixture()
        smtp.data.side_effect = smtplib.SMTPServerDisconnected('after submission')
        with self.assertRaisesRegex(RuntimeError, 'DATA 응답'):
            self.run_consume(queue, smtp)
        self.assertEqual(queue.data['jobs/test.json']['receipts'], dict.fromkeys(map(address_key, to + cc), 'uncertain'))
        with self.assertRaises(RuntimeError):
            self.run_consume(queue, smtp)
        smtp.data.assert_called_once()
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_unapproved_executive_recipient_fails_before_smtp(self):
        queue, smtp, _, _ = self.executive_fixture()
        self.bot.EMAIL_CC[-1] = 'unapproved@example.invalid'
        with self.assertRaisesRegex(RuntimeError, '수신자 불일치'):
            self.run_consume(queue, smtp)
        smtp.login.assert_not_called()
        self.assertEqual(queue.data['jobs/test.json']['receipts'], {})

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

    def cap_fixture(self):
        queue = self.grouped_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        date = now().astimezone(timezone(timedelta(hours=9))).date()
        body = (f'코스피 급락으로 시장 전체 평가액이 감소했다. {date.year}년 {date.month}월 {date.day}일 기준 국내 10대 그룹의 시가총액은 약 3371조4410억원으로 집계됐다. '
                '한국거래소와 금융정보업체 에프앤가이드에 따르면 지난달 말 4472조5308억원보다 약 1101조원 감소했다.')
        for item, manifest in zip(job['input']['items'], job['body_manifest']):
            item.update(title='10대 그룹 시가총액 1100조원 감소 '+item['article_id'], desc=body, description=body)
            manifest['body'] = body
            analysis = result['analyses'][item['article_id']]
            analysis.update(entity='', entities=[], event_type='기타리스크', base_grade='주의',
                            fact_summary='국내 10대 그룹 시가총액이 전월 말보다 1101조원 감소했다.',
                            judgment={'핵심사건':'기타리스크','손실주체':'주주','당사연관':'직접','확정여부':'확정'},
                            action='시장 전체 평가액 감소와 실제 보유자산 영향을 구분해 확인한다.',
                            evidence=[{'source_segment':'body','source_hash':hashlib.sha256(body.encode()).hexdigest(),
                                       'quote':body}], customer_notice=None)
        result['event_groups'] = []  # Omitted model grouping must not reopen a snapshot.
        job['input']['history_hash'] = hashlib.sha256(queue.remote_seen).hexdigest()
        job['input']['raw_articles'] = copy.deepcopy(job['input']['items'])
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        return queue

    def market_policy_fixture(self):
        queue = self.grouped_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        sources = [
            ('국내 시장 거래중단', '코스피가 8% 급락해 한국거래소가 서킷브레이커를 발동했다.', '주의'),
            ('정부 신용거래 제도 변경', '정부는 증권사 신용융자 담보비율 유지의무를 9일부터 3개월간 면제하기로 했다.', '참고')]
        for item, manifest, (title, body, grade) in zip(job['input']['items'], job['body_manifest'], sources):
            item.update(title=title, desc='검색 요약 대신 검증된 본문을 사용한다.', description='검색 요약 대신 검증된 본문을 사용한다.')
            manifest.update(body=body)
            result['analyses'][item['article_id']].update(
                entity='', entities=[], event_type='기타리스크', base_grade=grade,
                fact_summary=body, confidence=0.9,
                judgment={'핵심사건':title, '손실주체':'주주', '당사연관':'직접', '확정여부':'확정'},
                action='공식 안내의 시행일과 적용 대상을 확인한다.', customer_notice=None,
                evidence=[{'source_segment':'body', 'source_hash':hashlib.sha256(body.encode()).hexdigest(), 'quote':body}])
        result['event_groups'] = []
        job['input']['raw_articles'] = copy.deepcopy(job['input']['items'])
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        return queue

    def test_crash_report_keeps_market_representative_and_separate_credit_policy(self):
        queue, smtp = self.market_policy_fixture(), SMTP()
        with patch.object(self.bot.build_price_alert_section, 'last_alerted_count', 51, create=True), \
             patch.object(self.bot.build_price_alert_section, 'last_alerted_rbal', 431, create=True):
            self.run_consume(queue, smtp)
        final = queue.data['filter-logs/test.json']['final']
        self.assertEqual({a['article_id'] for a in final}, {'one','two'})
        self.assertTrue(all(a['grade'] == '참고' and a['entity'] == '' for a in final))
        document = queue.data['previews/test.json']['html']
        for a in queue.data['results/test.json']['analyses'].values():
            self.assertIn(a['fact_summary'], document)
        self.assertEqual(document.count('https://example.invalid/test/one'), 1)
        self.assertEqual(document.count('https://example.invalid/test/two'), 1)
        self.assertEqual(len(smtp.calls), 3)
        self.assertEqual(queue.data['jobs/test.json']['paid_ai_calls'], 0)

    def test_market_and_policy_do_not_open_new_full_send_gate(self):
        from risk_provider import Provider
        queue = self.market_policy_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        provider = Provider(job, result)
        rows = copy.deepcopy(job['input']['items'])
        for a in rows:
            provider.attach(a)
            a.update(entity='', entities=[], event_type='기타리스크', grade='참고', _risk_score=4.0)
        self.assertTrue(self.bot.decide_send_scope(rows, {})['self_only'])
        # An unrelated full-send trigger does not turn an ordinary market note into a crisis.
        self.assertEqual(self.bot.filter_articles_for_scope(rows, {}, False, False), [rows[1]])
        unverified = dict(rows[0])
        unverified.pop('_subscription_market_event')
        self.assertEqual(self.bot.filter_articles_for_scope([unverified], {}, False, True), [])

    def test_repeated_market_reports_cannot_displace_policy_before_final_grouping(self):
        queue, smtp = self.market_policy_fixture(), SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        result['analyses']['one']['confidence'] = 0.70
        result['analyses']['two']['confidence'] = 0.60
        members = ['one']
        for i in range(8):
            aid = 'repeat'+str(i)
            item = dict(job['input']['items'][0], article_id=aid, url='https://example.invalid/'+aid,
                        title='당일 코스피 서킷브레이커 확인 '+aid)
            job['input']['items'].append(item)
            job['body_manifest'].append(dict(job['body_manifest'][0], article_id=aid, source_url=item['url']))
            result['classifications'].append({'article_id':aid,'relevant':True,'reason':'검증된 동일 시장 사건 보도.'})
            result['analyses'][aid] = copy.deepcopy(result['analyses']['one'])
            result['analyses'][aid]['confidence'] = 0.72+i*0.03
            members.append(aid)
        result['event_groups'] = [{'article_ids':members, 'reason':'동일 거래일 코스피 서킷브레이커 반복 보도.', 'reviewed':True}]
        job['input']['raw_articles'] = copy.deepcopy(job['input']['items'])
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        with patch.object(self.bot.build_price_alert_section, 'last_alerted_count', 51, create=True), \
             patch.object(self.bot.build_price_alert_section, 'last_alerted_rbal', 431, create=True):
            self.run_consume(queue, smtp)
        final = queue.data['filter-logs/test.json']['final']
        self.assertEqual({a['article_id'] for a in final}, {'repeat7','two'})
        self.assertEqual(queue.data['metrics/test.json']['summary_source_ids'], ['repeat7','two'])

    def test_policy_and_market_cannot_be_grouped_or_policy_upgraded_to_urgent(self):
        from risk_result_validator import validate
        for case in ('mixed_group','urgent','customer_notice'):
            with self.subTest(case=case):
                queue = self.market_policy_fixture()
                job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
                if case == 'mixed_group':
                    result['event_groups'] = [{'article_ids':['one','two'],
                        'reason':'동일 사건으로 잘못 합친 검수 입력.', 'reviewed':True}]
                elif case == 'urgent':
                    result['analyses']['two']['base_grade'] = '긴급'
                else:
                    result['analyses']['two']['customer_notice'] = '모든 고객의 반대매매가 유예됩니다.'
                with self.assertRaisesRegex(ValueError, '제도 변경'):
                    validate(result, job, job['input'], job['body_manifest'])

    def test_cap_snapshot_native_mail_then_sibling_replay_is_suppressed(self):
        from event_history import EventHistory, HISTORY_KEY
        from risk_provider import Provider
        queue, smtp = self.cap_fixture(), SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        provider = Provider(job, result)
        self.assertEqual(provider.event_groups['one'], provider.event_groups['two'])
        self.run_consume(queue, smtp)
        document = queue.data['previews/test.json']['html']
        self.assertIn('1건 선별', document)
        self.assertIn('https://example.invalid/test/two', document)
        self.assertNotIn('https://example.invalid/test/one', document)
        # Only the selected URL is delivered, but the same source snapshot
        # suppresses a different URL in the next round even after serialization.
        saved = json.loads(Path('seen_news.json').read_text())
        history = EventHistory(saved[HISTORY_KEY])
        row = copy.deepcopy(job['input']['items'][0])
        provider.attach(row)
        self.assertNotIn('_subscription_stat_series', row)
        self.assertTrue(history.duplicate_statistic(row))
        self.assertEqual(saved[HISTORY_KEY]['records'][0]['scope'], 'self')
        # Execute a fresh native round from the saved state, with different URLs.
        queue2 = self.cap_fixture()
        self.run_consume(queue2, SMTP())
        self.assertEqual(queue2.data['filter-logs/test.json']['final'], [])
        self.assertNotIn('10대 그룹 시가총액 1100조원 감소', queue2.data['previews/test.json']['html'])

    def test_cap_snapshot_requires_exact_source_identity_and_preserves_revisions(self):
        from risk_provider import market_cap_snapshot
        from datetime import date
        published = date(2026, 10, 25)
        body = ('한국거래소와 금융정보업체 에프앤가이드에 따르면 지난 23일 기준 국내 10대 그룹의 '
                '시가총액은 약 3371조4410억원으로 집계됐다. 지난달 말 4472조5308억원보다 감소했다.')
        original = market_cap_snapshot(body, published)
        self.assertIsNotNone(original)
        for changed in (body.replace('23일', '24일'), body.replace('10대', '20대'),
                        body.replace('3371조4410', '3372조4410'), body.replace('4472조5308', '4473조5308')):
            self.assertNotEqual(market_cap_snapshot(changed, published)[0], original[0])
        for changed in (body.replace('23일 기준', '기준일 미상'), body.replace('에프앤가이드', '익명관계자'),
                        body.replace('지난달 말', '일정 기간 전'), body.replace('23일', '32일')):
            with self.subTest(changed=changed):
                try:
                    value = market_cap_snapshot(changed, published)
                except ValueError:
                    value = None
                self.assertIsNone(value)
        # A publication date cannot by itself create a new snapshot.
        self.assertEqual(market_cap_snapshot(body, date(2026,10,26)), original)

    def test_subscription_price_exception_requires_dated_verified_source_quote(self):
        from risk_provider import Provider
        queue = self.fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        published = now().astimezone(timezone(timedelta(hours=9))).date()
        text = f'{published.year}년 {published.month}월 {published.day}일 한국투자증권 주가는 9.78% 내린 가격에 마감했다.'
        for body_status, quote, expected in (
                ('verified', text, published.isoformat()),
                ('verified', '한국투자증권 주가는 9.78% 내린 가격에 마감했다.', ''),
                ('verified', '효력정지 인용 시 영향이 없다고 밝혔다.', ''),
                ('missing', text, '')):
            with self.subTest(body_status=body_status, quote=quote):
                job['body_manifest'][0]['body'] = text+' 효력정지 인용 시 영향이 없다고 밝혔다.'
                a = result['analyses']['one']
                a['body_status'] = body_status
                a['evidence'][0]['quote'] = quote
                provider = Provider(job, result)
                row = copy.deepcopy(job['input']['items'][0])
                provider.attach(row)
                self.assertEqual(row['_subscription_price_date'], expected)

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

    def unqualified_statistics_fixture(self):
        queue = self.market_statistics_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        for item, body in zip(job['input']['items'], job['body_manifest']):
            day = 14 if item['article_id'] in ('one', 'two') else 15
            quote = f'지난 {day}일 반대매매 금액은 804억원으로 집계됐다.'
            text = '금융투자협회에 따르면 '+quote
            item.update(description=text, desc=text)
            body.update(body=text, body_hash=hashlib.sha256(text.encode()).hexdigest())
            analysis = result['analyses'][item['article_id']]
            analysis.update(fact_summary=f'{day}일 반대매매 금액은 804억원이다.',
                            evidence=[{'source_segment':'body','source_hash':hashlib.sha256(text.encode()).hexdigest(),
                                       'quote':quote}])
        job['input']['raw_articles'] = copy.deepcopy(job['input']['items'])
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        return queue

    def test_dated_kofia_amount_without_misugeum_never_reopens_group_route_or_same_day(self):
        queue, smtp = self.unqualified_statistics_fixture(), SMTP()
        self.run_consume(queue, smtp)
        self.assertTrue(queue.data['outbox/test.json']['self_only'])
        self.assertEqual(len(smtp.calls), 1)
        self.assertEqual([a['article_id'] for a in queue.data['filter-logs/test.json']['final']], ['three'])
        queue.remote_seen = Path('seen_news.json').read_bytes()
        acknowledge(queue, 'test')
        queue, smtp = self.unqualified_statistics_fixture(), SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        job['input']['history_hash'] = hashlib.sha256(queue.remote_seen).hexdigest()
        for key in ('items', 'raw_articles'):
            for item in job['input'][key]:
                item['url'] += '-another-report'
        for body in job['body_manifest']:
            body['source_url'] += '-another-report'
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        self.run_consume(queue, smtp)
        self.assertEqual(queue.data['filter-logs/test.json']['final'], [])
        self.assertTrue(queue.data['outbox/test.json']['self_only'])
        self.assertEqual(len(smtp.calls), 1)

    def test_unqualified_amount_needs_institution_dated_quote_and_market_identity(self):
        from risk_provider import Provider
        for case in ('institution', 'date', 'amount', 'credit', 'quote', 'company', 'forecast'):
            with self.subTest(case=case):
                queue = self.unqualified_statistics_fixture()
                job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
                result['event_groups'] = []
                body, analysis = job['body_manifest'][0], result['analyses']['one']
                if case == 'institution': body['body'] = body['body'].replace('금융투자협회', '다른기관')
                if case in {'date', 'amount', 'credit'}:
                    quote = analysis['evidence'][0]['quote']
                    changed = quote.replace('14일', '집계일 미상') if case == 'date' else (
                              quote.replace('804억원', '증가') if case == 'amount' else '신용융자 담보부족 '+quote)
                    body['body'] = body['body'].replace(quote, changed)
                    analysis['evidence'][0]['quote'] = changed
                if case == 'quote': analysis['evidence'][0]['source_segment'] = 'description'
                if case == 'company': analysis.update(entity='개별회사', entities=['개별회사'])
                if case == 'forecast': analysis['judgment']['확정여부'] = '예정'
                provider = Provider(job, result)
                self.assertNotEqual(provider.event_groups.get('one'), 'market:금투협:미수금반대매매')

    def test_reviewed_group_with_different_credit_statistic_never_bridges_series(self):
        from risk_provider import Provider
        queue = self.unqualified_statistics_fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        result['analyses']['two']['fact_summary'] = '신용융자 담보부족 반대매매 금액은 804억원이다.'
        provider = Provider(job, result)
        for aid in ('one', 'two'):
            self.assertNotEqual(provider.event_groups.get(aid), 'market:금투협:미수금반대매매')

    def test_market_statistics_higher_score_precedes_newer_source_date(self):
        queue, smtp = self.market_statistics_fixture(), SMTP()
        queue.data['results/test.json']['analyses']['one']['confidence'] = 0.99
        self.run_consume(queue, smtp)
        self.assertEqual([a['article_id'] for a in queue.data['filter-logs/test.json']['final']], ['one'])

    def test_market_grade_demotion_reaches_native_mail_and_ack(self):
        for before, after in (('긴급', '주의'), ('주의', '참고'), ('참고', '참고')):
            with self.subTest(base_grade=before):
                Path('seen_news.json').write_bytes(self.before)
                queue, smtp = self.market_statistics_fixture(), SMTP()
                for analysis in queue.data['results/test.json']['analyses'].values():
                    analysis['base_grade'] = before
                self.run_consume(queue, smtp)
                final = queue.data['filter-logs/test.json']['final']
                self.assertEqual([a['article_id'] for a in final], ['three'])
                self.assertEqual(final[0]['grade'], after)
                self.assertTrue(final[0]['_grade_locked'])
                self.assertEqual(final[0]['_ai_confidence'], 0.96)
                score = self.bot.display_risk_score(final[0])
                low, high = self.bot._GRADE_SCORE_BAND[after]
                self.assertTrue(low <= score <= high)
                self.assertIn('https://example.invalid/test/three', queue.data['previews/test.json']['html'])
                self.assertTrue(queue.data['outbox/test.json']['self_only'])
                self.assertEqual(len(smtp.calls), 1)
                queue.remote_seen = Path('seen_news.json').read_bytes()
                acknowledge(queue, 'test')
                self.assertTrue(queue.data['jobs/test.json']['ack'])

    def test_named_market_news_is_reference_in_native_mail(self):
        queue, smtp = self.fixture(), SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        title = '마이크론·AMD·인텔 5%대 급락…美 반도체주 일제히 휘청'
        body = '미국 반도체주가 동반 하락했다. 마이크론 주가는 5.63% 하락했다.'
        job['input']['items'][0].update(title=title, description=body, desc=body)
        job['input']['raw_articles'] = copy.deepcopy(job['input']['items'])
        job['body_manifest'][0]['body'] = body
        result['analyses']['one'].update(entity='마이크론', entities=['마이크론'],
            event_type='기타리스크', base_grade='주의', fact_summary=body,
            action='마이크론 보유 및 여신 계좌의 가격을 확인한다.',
            judgment={'핵심사건':'기타리스크','손실주체':'주주','당사연관':'직접','확정여부':'확정'},
            evidence=[{'source_segment':'body','source_hash':hashlib.sha256(body.encode()).hexdigest(),
                       'quote':'마이크론 주가는 5.63% 하락했다.'}])
        date = now().astimezone(self.bot.timezone(self.bot.timedelta(hours=9))).date().isoformat()
        Path('exposure_data.csv').write_text('기준일,종목명,종목코드,종목유형,뱅잔고,영잔고,뱅고객수,영고객수\n'
                                           +date+',마이크론,999001,채권,500,0,2,0\n')
        job['input']['exposure_hash'] = hashlib.sha256(Path('exposure_data.csv').read_bytes()).hexdigest()
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        self.run_consume(queue, smtp)
        final = queue.data['filter-logs/test.json']['final']
        self.assertEqual([a['grade'] for a in final], ['참고'])
        self.assertEqual(final[0]['entity'], '마이크론')
        self.assertTrue(final[0]['_market_grade_demoted'])
        self.assertTrue(queue.data['outbox/test.json']['self_only'])
        self.assertEqual(len(smtp.calls), 1)
        self.assertIn('참고', queue.data['previews/test.json']['html'])

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

    def test_operational_no_result_uses_gmail_envelope_and_headers(self):
        from email import message_from_string
        from email.utils import parseaddr
        smtp = SMTP()
        self.bot.NO_RESULT_RECEIVER = 'company@example.invalid'
        with patch.object(smtplib, 'SMTP_SSL', return_value=smtp):
            self.bot.send_email_no_result('No detection', '<p>No detection</p>')
        self.assertEqual([c[0] for c in smtp.calls], [['myboy3743@gmail.com']])
        message = message_from_string(smtp.calls[0][1])
        self.assertEqual(parseaddr(message['To'])[1], 'myboy3743@gmail.com')
        self.assertIsNone(message['Cc'])
        self.assertIsNone(message['Bcc'])

    def test_reference_business_report_keeps_configured_self_recipient(self):
        queue, smtp = self.market_statistics_fixture(), SMTP()
        self.run_consume(queue, smtp)
        self.assertEqual([c[0] for c in smtp.calls], [['owner@example.invalid']])
        self.assertEqual(queue.data['outbox/test.json']['recipient_route'], 'configured')

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


class ExclusionEvidence(unittest.TestCase):
    setUp = Integration.setUp
    tearDown = Integration.tearDown
    fixture = Integration.fixture
    run_consume = Integration.run_consume

    def excluded(self, risky=False, body_missing=False):
        from risk_result_validator import EXCLUSION_RULES
        q = self.fixture()
        j, r = q.data['jobs/test.json'], q.data['results/test.json']
        j['exclusion_review_version'] = 1
        a, b = j['input']['items'][0], j['body_manifest'][0]
        title = ('타 증권사 일부 MTS 잔고 조회 장애 발생' if risky else '한국투자증권 신규 상품 출시 기념 이벤트 안내')
        a.update(title=title, description=title, desc=title)
        b.update(body='' if body_missing else title+' 관련 공식 안내 내용이다.',
                 body_status='missing' if body_missing else 'verified')
        evidence = {'source_segment':'title','source_hash':hashlib.sha256(title.encode()).hexdigest(),'quote':title}
        review = {'rule_quote':next(rule for rule in EXCLUSION_RULES if rule.startswith('타 증권사' if risky else '주간·월간')),
                  'kind':'other_entity' if risky else 'non_risk', 'evidence':evidence,'reviewed':True}
        if risky and not body_missing:
            review['countercheck'] = {'reason':'직접 장애 주체가 타 증권사이며 당사의 직접 사건은 아니라는 원문을 재검토했다.',
                'reviewed':True,'evidence':{'source_segment':'body','source_hash':hashlib.sha256(b['body'].encode()).hexdigest(),'quote':b['body']}}
        r['classifications'][0].update(relevant=False, reason='기존 제외 규칙과 실제 주체를 확인했다.', exclusion_review=review)
        r['analyses'] = {}
        j['input']['raw_articles'] = copy.deepcopy(j['input']['items'])
        j['input_hash'] = r['input_hash'] = digest(j['input'])
        j['body_manifest_hash'] = r['body_manifest_hash'] = digest(j['body_manifest'])
        return q

    def verify(self, q):
        from risk_result_validator import validate
        j = q.data['jobs/test.json']
        return validate(q.data['results/test.json'], j, j['input'], j['body_manifest'])

    def test_clear_marketing_with_missing_body_can_still_be_excluded(self):
        self.verify(self.excluded(body_missing=True))

    def test_critical_missing_body_cannot_be_excluded_even_with_title_quote(self):
        with self.assertRaisesRegex(ValueError, '위험 후보 제외'):
            self.verify(self.excluded(risky=True, body_missing=True))

    def test_verified_other_entity_requires_second_body_review(self):
        q = self.excluded(risky=True)
        self.verify(q)
        q.data['results/test.json']['classifications'][0]['exclusion_review'].pop('countercheck')
        with self.assertRaisesRegex(ValueError, '재심사'):
            self.verify(q)

    def test_critical_article_cannot_be_called_generic_marketing(self):
        q = self.excluded(risky=True)
        q.data['results/test.json']['classifications'][0]['exclusion_review']['kind'] = 'non_risk'
        with self.assertRaisesRegex(ValueError, '위험 후보 제외'):
            self.verify(q)

    def test_forged_quote_hash_rule_and_positive_criterion_are_rejected(self):
        for mutation in ('quote','hash','rule','positive_rule'):
            q = self.excluded()
            review = q.data['results/test.json']['classifications'][0]['exclusion_review']
            if mutation == 'quote':
                review['evidence']['quote'] = '이 문장은 원문에 없는 조작된 인용문이다.'
            elif mutation == 'hash':
                review['evidence']['source_hash'] = '0'*64
            else:
                review['rule_quote'] = ('확신 없으면 일반 홍보로 자동 제외한다.' if mutation == 'rule'
                    else '한국투자증권 MTS·HTS 시스템 직접 장애·보안사고·오류')
            with self.assertRaises(ValueError):
                self.verify(q)

    def test_unrelated_body_quote_cannot_satisfy_critical_countercheck(self):
        q = self.excluded(risky=True)
        j, r = q.data['jobs/test.json'], q.data['results/test.json']
        body = j['body_manifest'][0]['body']+' 일반 상품 출시 홍보 문장이다.'
        j['body_manifest'][0]['body'] = body
        j['body_manifest_hash'] = r['body_manifest_hash'] = digest(j['body_manifest'])
        e = r['classifications'][0]['exclusion_review']['countercheck']['evidence']
        e.update(source_hash=hashlib.sha256(body.encode()).hexdigest(), quote='일반 상품 출시 홍보 문장이다.')
        with self.assertRaisesRegex(ValueError, '사건 본문'):
            self.verify(q)

    def test_invalid_false_does_not_resolve_prior_hold_or_send_anything(self):
        from risk_continuity import prepare_reviews
        q = self.excluded(risky=True, body_missing=True)
        j = q.data['jobs/test.json']
        values, expected, _ = prepare_reviews(q, j, {'one':None})
        q.put_many(values, expected)
        before = copy.deepcopy(q.data['review/one.json'])
        smtp = SMTP()
        with self.assertRaisesRegex(ValueError, '위험 후보 제외'):
            self.run_consume(q, smtp)
        self.assertEqual(q.data['review/one.json'], before)
        self.assertEqual(q.data['review/index.json']['pending'], ['one'])
        self.assertEqual(smtp.calls, [])
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_new_metadata_cannot_be_added_to_old_results_or_non_false_rows(self):
        for mutation in ('old', 'true'):
            q = self.excluded()
            if mutation == 'old':
                q.data['jobs/test.json'].pop('exclusion_review_version')
            else:
                q.data['results/test.json']['classifications'][0]['relevant'] = True
            with self.assertRaisesRegex(ValueError, '적용 대상'):
                self.verify(q)

    def test_adopted_credit_policy_cannot_be_excluded_as_generic_easing(self):
        from risk_result_validator import EXCLUSION_RULES
        for text, blocked in (
            ('정부는 증권사 신용융자 담보비율 유지의무를 9일부터 3개월간 면제하기로 했다.', True),
            ('정부는 부동산PF 익스포저 한도 규제를 3개월간 완화하기로 했다.', False),
            ('금융위원회는 신용융자 담보비율 면제를 검토한다.', False)):
            with self.subTest(text=text):
                q = self.excluded()
                j, r = q.data['jobs/test.json'], q.data['results/test.json']
                item = j['input']['items'][0]
                item.update(title=text, desc=text, description=text)
                j['body_manifest'][0].update(body=text)
                evidence = {'source_segment':'body','source_hash':hashlib.sha256(text.encode()).hexdigest(),'quote':text}
                review = r['classifications'][0]['exclusion_review']
                review.update(rule_quote=next(x for x in EXCLUSION_RULES if x.startswith('규제 완화')),
                              kind='not_eligible', evidence=evidence)
                j['input']['raw_articles'] = copy.deepcopy(j['input']['items'])
                j['input_hash'] = r['input_hash'] = digest(j['input'])
                j['body_manifest_hash'] = r['body_manifest_hash'] = digest(j['body_manifest'])
                if blocked:
                    with self.assertRaisesRegex(ValueError, '제도 변경'):
                        self.verify(q)
                else:
                    self.verify(q)


if __name__ == '__main__':
    unittest.main()
