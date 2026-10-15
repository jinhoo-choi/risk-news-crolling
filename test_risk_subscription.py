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
        self.assertEqual([c[0] for c in smtp.calls], [['111715@koreainvestment.com']])
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        from email import message_from_string
        message = message_from_string(smtp.calls[0][1])
        from email.utils import parseaddr
        self.assertEqual(parseaddr(message['To'])[1], '111715@koreainvestment.com')
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


if __name__ == '__main__':
    unittest.main()
