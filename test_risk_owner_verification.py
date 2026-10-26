import copy
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import timedelta
from unittest import TestCase, main
from email.mime.text import MIMEText

from risk_owner_verification import OwnerSMTP, OWNER, ERROR_EMAIL, require_pending
from risk_queue import now


class Queue:
    def __init__(self):
        self.job = {'mode': 'verification', 'status': 'pending', 'receipts': {},
                    'expires_at': (now()+timedelta(minutes=5)).isoformat()}
    def get(self, path):
        return copy.deepcopy(self.job), 'sha'
    def put(self, path, value, sha=None):
        self.job = copy.deepcopy(value)


class SMTP:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0
    def sendmail(self, sender, recipients, raw):
        self.calls += 1
        if self.fail:
            raise ConnectionError('DATA response unknown')
        return {}
    def quit(self):
        pass
    def ehlo(self):
        pass
    def login(self,*args):
        pass


def message(to=OWNER, bcc=None):
    m = MIMEText('test', 'html')
    m['To'] = to
    if bcc:
        m['Bcc'] = bcc
    return m.as_string()


class IsolationTests(TestCase):
    def setUp(self):
        self.queue, self.smtp = Queue(), SMTP()
        self.guard = OwnerSMTP(lambda *a, **k: self.smtp, self.queue, 'job')
    def test_accepted_is_reserved_and_recorded_once(self):
        with self.guard.factory('smtp.gmail.com', 465) as server:
            server.sendmail('sender@example.invalid', [OWNER], message())
        self.assertEqual(self.smtp.calls, 1)
        self.assertEqual(list(self.queue.job['receipts'].values()), ['accepted'])
        self.assertEqual(self.queue.job['status'], 'sending')
        self.assertEqual(self.guard.accepted, 1)
        with self.assertRaises(RuntimeError):
            self.guard.factory('smtp.gmail.com', 465)
    def test_ambiguous_data_locks_without_retry(self):
        self.smtp.fail = True
        with self.assertRaises(RuntimeError):
            with self.guard.factory('smtp.gmail.com', 465) as server:
                server.sendmail('sender@example.invalid', [OWNER], message())
        self.assertEqual(self.queue.job['status'], 'uncertain')
        self.assertEqual(list(self.queue.job['receipts'].values()), ['uncertain'])
        with self.assertRaises(RuntimeError):
            self.guard.factory('smtp.gmail.com', 465)
        self.assertEqual(self.smtp.calls, 1)
    def test_group_envelope_is_rejected_before_attempt(self):
        with self.assertRaises(RuntimeError):
            with self.guard.factory('smtp.gmail.com', 465) as server:
                server.sendmail('sender@example.invalid', ['group@example.invalid'], message())
        self.assertEqual(self.smtp.calls, 0)
        self.assertEqual(self.queue.job['status'], 'pending')
    def test_header_drift_rejected(self):
        for raw in (message('group@example.invalid'), message(bcc='group@example.invalid')):
            with self.assertRaises(RuntimeError):
                with self.guard.factory('smtp.gmail.com', 465) as server:
                    server.sendmail('sender@example.invalid', [OWNER], raw)
        self.assertEqual(self.smtp.calls, 0)
    def test_expired_or_started_job_rejected(self):
        for status in ('sending', 'uncertain', 'delivery_complete', 'verified', 'completed'):
            self.queue.job['status'] = status
            with self.assertRaises(RuntimeError):
                require_pending(self.queue.job)
        self.queue.job['status'] = 'pending'
        self.queue.job['expires_at'] = (now()-timedelta(seconds=1)).isoformat()
        with self.assertRaises(RuntimeError):
            require_pending(self.queue.job)
    def test_production_job_cannot_be_self_verified(self):
        self.queue.job['mode'] = 'production'
        with self.assertRaises(RuntimeError):
            require_pending(self.queue.job)

    def test_gmail_suite_rejects_company_group_and_header_drift(self):
        guard = OwnerSMTP(lambda *a, **k: self.smtp, self.queue, 'job', recipient=ERROR_EMAIL)
        for target, raw in [(OWNER,message(OWNER)), ('group@example.invalid',message('group@example.invalid')),
                            (ERROR_EMAIL,message(OWNER)), (ERROR_EMAIL,message(ERROR_EMAIL,bcc=OWNER))]:
            with self.assertRaises(RuntimeError):
                with guard.factory('smtp.gmail.com',465) as server:
                    server.sendmail('sender@example.invalid',[target],raw)
        self.assertEqual(self.smtp.calls,0)
        with guard.factory('smtp.gmail.com',465) as server:
            server.sendmail('sender@example.invalid',[ERROR_EMAIL],message(ERROR_EMAIL))
        self.assertEqual(self.smtp.calls,1)
        self.assertEqual(guard.accepted,1)

    def test_arbitrary_verification_recipient_is_denied(self):
        with self.assertRaises(RuntimeError):
            OwnerSMTP(lambda *a, **k: self.smtp,self.queue,'job',recipient='other@example.invalid')

    def test_live_suite_uses_native_sender_with_gmail_guard_and_preserves_operating_files(self):
        import risk_scenario_verification as suite
        from risk_subscription import import_bot
        from test_risk_hardening import Memory
        previous=Path.cwd()
        with patch.dict(os.environ,dict(os.environ),clear=True),tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                for name in ('seen_news.json','exposure_data.csv','run_stats.jsonl'):
                    Path(name).write_text('operating fixture')
                before={p:Path(p).read_bytes() for p in ('seen_news.json','exposure_data.csv','run_stats.jsonl')}
                bot=import_bot()
                os.environ.update(EMAIL_SENDER=ERROR_EMAIL,EMAIL_PASSWORD='fake',EMAIL_RECEIVER='',EMAIL_CC='',
                                  GITHUB_RUN_ATTEMPT='1')
                os.environ.pop('ANTHROPIC_API_KEY',None)
                queue=Memory();queue.get_fresh=queue.get
                smtp=SMTP();captured=[]
                send=smtp.sendmail
                def capture(sender,recipients,raw):
                    captured.append((sender,recipients,raw));return send(sender,recipients,raw)
                smtp.sendmail=capture
                prepared=[{'scenario':'S01','title':'transport test','html':'<html><body>fixture</body></html>',
                           'native_html':True,'selected':0}]
                with patch.object(suite,'prepare',return_value=prepared), \
                     patch.object(suite,'Queue',return_value=queue), \
                     patch.object(suite,'capture_previews',return_value={'status':'test'}), \
                     patch.object(suite.smtplib,'SMTP_SSL',lambda *a,**k:smtp),contextlib.redirect_stdout(io.StringIO()):
                    suite.run({'operation':'scenario-suite','self_test':True,'request_id':'suite-test','send':True})
                self.assertEqual(smtp.calls,1)
                self.assertEqual(captured[0][1],[ERROR_EMAIL])
                self.assertEqual(queue.data['jobs/suite-test-S01.json']['status'],'verified')
                self.assertEqual(before,{p:Path(p).read_bytes() for p in before})
                self.assertEqual(bot.EMAIL_RECEIVERS,[])
                self.assertEqual(bot.EMAIL_CC,[])
            finally:
                os.chdir(previous)

    def test_live_suite_rejects_reruns_before_preparation_queue_or_smtp(self):
        import risk_scenario_verification as suite
        request = {'operation':'scenario-suite','self_test':True,'request_id':'rerun-test','send':True}
        for attempt in ('2', '3'):
            with self.subTest(attempt=attempt), \
                    patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT':attempt}), \
                    patch.object(suite, 'prepare') as prepare, \
                    patch.object(suite, 'Queue') as queue, \
                    patch.object(suite.smtplib, 'SMTP_SSL') as smtp:
                with self.assertRaisesRegex(RuntimeError, '검수 재실행 금지'):
                    suite.run(request)
                prepare.assert_not_called()
                queue.assert_not_called()
                smtp.assert_not_called()

    def test_live_failure_notification_is_owner_only_native_labelled_and_keeps_source(self):
        import risk_owner_verification as verify
        from risk_subscription import import_bot
        from test_risk_hardening import Memory
        previous = Path.cwd()
        with patch.dict(os.environ, dict(os.environ), clear=True), tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                for name in ('seen_news.json','exposure_data.csv','run_stats.jsonl'):
                    Path(name).write_text('operating fixture')
                before = verify.snapshot()
                bot = import_bot()
                os.environ.update(EMAIL_SENDER=ERROR_EMAIL, EMAIL_PASSWORD='fake', EMAIL_CC='', GITHUB_RUN_ATTEMPT='1')
                os.environ.pop('EMAIL_RECEIVER', None)
                os.environ.pop('ANTHROPIC_API_KEY', None)
                queue = Memory(); queue.get_fresh = queue.get
                source = {'mode':'production','status':'pending','receipts':{},'input_hash':'input','policy_hash':'policy','input_count':1028}
                queue.data['jobs/source.json'] = copy.deepcopy(source)
                queue.data['analysis-attempts/source.json'] = {'state':'blocked','input_hash':'input','policy_hash':'policy',
                    'processed_count':0,'stage':'started','errors':[{'error_type':'duplicate_delivery_risk','cause_confirmed':True}]}
                smtp, captured = SMTP(), []
                send = smtp.sendmail
                def capture(sender, recipients, raw):
                    captured.append((recipients, raw)); return send(sender, recipients, raw)
                smtp.sendmail = capture
                request = {'operation':'failure-mail','source_job_id':'source','self_test':True,'request_id':'owner-error-test'}
                cached = sys.modules.pop('naver_news_monitor', None)
                try:
                    with patch.object(verify, 'Queue', return_value=queue), \
                            patch.object(verify.smtplib, 'SMTP_SSL', lambda *a, **k: smtp), contextlib.redirect_stdout(io.StringIO()):
                        verify.failure_mail(request)
                        os.environ.pop('ANTHROPIC_API_KEY', None)
                        verify.failure_mail(request)
                    bot = sys.modules['naver_news_monitor']
                finally:
                    if cached is not None:
                        sys.modules['naver_news_monitor'] = cached
                self.assertEqual(smtp.calls, 1)
                self.assertEqual(captured[0][0], [ERROR_EMAIL])
                preview = queue.data['previews/owner-error-test.json']
                self.assertTrue(preview['subject'].startswith('[TEST][owner-error-test]'))
                self.assertIn('duplicate_delivery_risk', preview['html'])
                self.assertIn('기사·운영 확인 필요', preview['html'])
                self.assertNotIn('런타임 오류 발생', preview['html'])
                self.assertEqual(queue.data['jobs/source.json'], source)
                self.assertEqual(queue.data['jobs/owner-error-test.json']['status'], 'verified')
                self.assertEqual(verify.snapshot(), before)
                self.assertEqual(bot.EMAIL_RECEIVERS, [])
                self.assertEqual(bot.EMAIL_CC, [])
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    main()
