"""Failure evidence, SMTP boundaries and silence detection; synthetic local data."""
import contextlib
import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import risk_audit as audit
import risk_subscription as bridge
from risk_queue import now, QueueConflict
import test_risk_subscription as fixtures


class Evidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.tmp.name)
        Path('seen_news.json').write_text('{}')
        self.q = fixtures.Queue({}, {})
        self.q.preflight = Mock()

    def tearDown(self):
        os.chdir(self.previous)
        self.tmp.cleanup()

    def records(self):
        return [v for p, v in self.q.data.items() if p.startswith('audit/')]

    def test_success_keeps_stages_and_no_delivery_mutation(self):
        original = copy.deepcopy(self.q.data)
        audit.run_audited(self.q, 'consume', 'test', lambda: audit.checkpoint('verify'))
        record, = self.records()
        self.assertEqual(record['state'], 'completed')
        self.assertEqual([e['stage'] for e in record['events']], ['started', 'verify', 'verify'])
        for p, value in original.items():
            self.assertEqual(self.q.data[p], value)

    def test_preflight_failure_never_writes_private_payload_to_unverified_repo(self):
        self.q.preflight.side_effect = RuntimeError('repository is public')
        self.q.put = Mock()
        action = Mock()
        with self.assertRaises(RuntimeError):
            audit.run_audited(self.q, 'consume', 'test', action)
        action.assert_not_called()
        self.q.put.assert_not_called()
        self.assertEqual(Path('risk_audit_spool.jsonl').stat().st_mode & 0o777, 0o600)

    def test_initial_audit_failure_stops_before_any_action_and_marks_unsaved(self):
        self.q.put = Mock(side_effect=QueueConflict())
        action, output = Mock(), io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(RuntimeError):
            audit.run_audited(self.q, 'consume', 'test', action)
        action.assert_not_called()
        self.assertIn('recorded=false', output.getvalue())

    def test_child_failure_and_cause_are_private_redacted_and_public_output_is_fixed(self):
        secret = 'not-a-real-private-token'
        error = subprocess.CalledProcessError(1, ['child'], output='PRIVATE ARTICLE '+secret,
                                             stderr='PRIVATE TRACE Authorization: Bearer '+secret)
        def fail():
            audit.checkpoint('request')
            raise error
        output = io.StringIO()
        with patch.dict(os.environ, RISK_QUEUE_TOKEN=secret), contextlib.redirect_stdout(output), \
                self.assertRaises(subprocess.CalledProcessError) as raised:
            audit.run_audited(self.q, 'request', 'test', fail)
        self.assertIs(raised.exception, error)
        record, = self.records()
        self.assertEqual(record['stage'], 'request')
        self.assertEqual(record['state'], 'failed')
        text = json.dumps(record)
        self.assertIn('PRIVATE ARTICLE', text)
        self.assertIn('PRIVATE TRACE', text)
        self.assertNotIn(secret, text)
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_cleanup_evidence_never_advances_primary_failure_stage(self):
        def fail():
            audit.checkpoint('smtp_data')
            audit.record_error('cleanup', QueueConflict('SECOND'), 'cleanup_failed')
            raise ConnectionError('FIRST DATA FAILURE')
        with self.assertRaises(ConnectionError):
            audit.run_audited(self.q, 'consume', 'test', fail)
        record, = self.records()
        self.assertEqual(record['stage'], 'smtp_data')
        self.assertEqual(record['events'][-2]['stage'], 'cleanup')
        self.assertIn('FIRST DATA FAILURE', record['events'][-1]['error']['message'])

    def test_error_record_failure_does_not_replace_original_exception(self):
        put = self.q.put
        def fail_later(path, value, sha=None):
            if value.get('state') == 'failed':
                raise RuntimeError('SECOND STORAGE FAILURE')
            return put(path, value, sha)
        first = ValueError('FIRST VALIDATION FAILURE')
        with patch.object(self.q, 'put', fail_later), self.assertRaises(ValueError) as raised:
            audit.run_audited(self.q, 'consume', 'test', Mock(side_effect=first))
        self.assertIs(raised.exception, first)
        spool = Path('risk_audit_spool.jsonl').read_text()
        self.assertIn('FIRST VALIDATION FAILURE', spool)
        self.assertIn('SECOND STORAGE FAILURE', spool)

    def test_sensitive_facts_remain_valid_json_and_secrets_are_removed(self):
        value = audit.clean_facts({'token': 'abc', 'nested': [{'cookie': 'xyz', 'ok': 'safe'}]})
        self.assertEqual(value, {'token': '[REDACTED]', 'nested': [{'cookie': '[REDACTED]', 'ok': 'safe'}]})

    def test_workflow_failure_is_not_relabelled_completed(self):
        Path('risk_history_error.log').write_text('fatal: rebase conflict in seen_news.json')
        with patch.dict(os.environ, RISK_AUDIT_OPERATION='consume-workflow', RISK_AUDIT_JOB_ID='test',
                        RISK_AUDIT_CONSUME='success', RISK_AUDIT_HISTORY='failure', RISK_AUDIT_ACK='skipped'), \
                patch.object(audit, 'Queue', return_value=self.q):
            audit.workflow_main()
        record, = self.records()
        self.assertEqual(record['state'], 'failed')
        self.assertEqual(record['events'][-2]['facts']['history'], 'failure')
        self.assertIn('rebase conflict', record['events'][-2]['facts']['history_log'])

    def test_silent_pending_is_an_observation_not_invented_cause_and_does_not_repeat(self):
        self.q.data.pop('results/test.json')
        job = {'job_id': 'test', 'mode': 'production', 'status': 'pending', 'receipts': {},
               'input_count': 1028, 'created_at': (now()-timedelta(hours=2)).isoformat()}
        before = copy.deepcopy(job)
        audit.observe_pending(self.q, job)
        record = self.q.data['incidents/test-analysis-silent.json']
        self.assertEqual(record['code'], 'analysis_evidence_missing')
        self.assertFalse(record['cause_confirmed'])
        saved = copy.deepcopy(record)
        audit.observe_pending(self.q, job)
        self.assertEqual(self.q.data['incidents/test-analysis-silent.json'], saved)
        self.assertEqual(job, before)

    def test_recent_heartbeat_existing_result_and_terminal_job_do_not_report_silence(self):
        job = {'job_id': 'test', 'mode': 'production', 'status': 'pending',
               'created_at': (now()-timedelta(hours=2)).isoformat()}
        audit.observe_pending(self.q, job)  # A result already exists.
        self.q.data.pop('results/test.json')
        self.q.data['analysis-attempts/test.json'] = {'updated_at': now().isoformat(), 'stage': 'analyzing'}
        audit.observe_pending(self.q, job)
        self.q.data.pop('analysis-attempts/test.json')
        audit.observe_pending(self.q, dict(job, status='completed'))
        self.assertFalse(any(p.startswith('incidents/') for p in self.q.data))

    def test_blocked_legacy_utc_record_is_reported_immediately_and_invalid_time_is_distinct(self):
        self.q.data.pop('results/test.json')
        job = {'job_id': 'test', 'mode': 'production', 'status': 'pending',
               'created_at': (now()-timedelta(hours=2)).isoformat()}
        self.q.data['analysis-attempts/test.json'] = {
            'state':'blocked', 'stage':'started', 'updated_at':now().strftime('%Y-%m-%d %H:%M:%S UTC')}
        audit.observe_pending(self.q, job)
        self.assertEqual(self.q.data['incidents/test-analysis-blocked.json']['code'], 'analysis_blocked')
        self.q.data['analysis-attempts/test.json']['updated_at'] = 'invalid timestamp'
        audit.observe_pending(self.q, job)
        self.assertEqual(self.q.data['incidents/test-analysis-attempt-invalid.json']['code'], 'analysis_attempt_invalid')


class DeliveryEvidence(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    tearDown = fixtures.Integration.tearDown
    fixture = fixtures.Integration.fixture
    run_consume = fixtures.Integration.run_consume

    def prepare(self):
        q = self.fixture()
        q.preflight = Mock()
        return q

    def audited_consume(self, q, smtp):
        return audit.run_audited(q, 'consume', 'test', lambda: self.run_consume(q, smtp))

    def test_audit_failure_before_data_never_reserves_or_sends(self):
        q, smtp = self.prepare(), fixtures.SMTP()
        put = q.put
        def blocked(path, value, sha=None):
            if path.startswith('audit/') and value['stage'] == 'smtp_data' and value['state'] == 'running':
                raise QueueConflict()
            return put(path, value, sha)
        with patch.object(q, 'put', blocked), self.assertRaises(RuntimeError):
            self.audited_consume(q, smtp)
        self.assertEqual(smtp.calls, [])
        self.assertEqual(q.data['jobs/test.json'].get('attempted_count', 0), 0)
        self.assertEqual(q.data['jobs/test.json']['receipts'], {})
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_after_data_audit_failure_preserves_accepted_receipt(self):
        q, smtp = self.prepare(), fixtures.SMTP()
        put = q.put
        def blocked(path, value, sha=None):
            if path.startswith('audit/') and value['stage'] == 'smtp_receipt' and value['state'] == 'running':
                raise QueueConflict()
            return put(path, value, sha)
        with patch.object(q, 'put', blocked), self.assertRaises(RuntimeError):
            self.audited_consume(q, smtp)
        self.assertEqual(len(smtp.calls), 1)
        self.assertEqual(list(q.data['jobs/test.json']['receipts'].values()), ['accepted'])
        self.assertEqual(q.data['jobs/test.json']['attempted_count'], 1)
        self.assertFalse(q.data['jobs/test.json'].get('ack', False))

    def test_smtp_and_uncertain_write_errors_are_both_kept_and_never_retried(self):
        q, smtp = self.prepare(), fixtures.SMTP(fail_at=1)
        put = q.put
        def blocked(path, value, sha=None):
            if path == 'jobs/test.json' and 'uncertain' in value.get('receipts', {}).values():
                raise QueueConflict('SECOND')
            return put(path, value, sha)
        with patch.object(q, 'put', blocked), self.assertRaises(RuntimeError) as raised:
            self.audited_consume(q, smtp)
        self.assertIn('SMTP DATA', str(raised.exception))
        self.assertEqual(len(smtp.calls), 1)
        records = json.dumps([v for p, v in q.data.items() if p.startswith('audit/')])
        self.assertIn('uncertain DATA response', records)
        self.assertIn('SECOND', records)
        self.assertFalse(q.data['jobs/test.json'].get('ack', False))

    def test_production_smtp_teardown_keeps_data_error_and_accepted_receipt(self):
        for fail_at in (1, 0):
            with self.subTest(fail_at=fail_at):
                q, smtp = self.prepare(), fixtures.SMTP(fail_at=fail_at)
                q.data['jobs/test.json']['created_at'] = now().isoformat()
                smtp.quit = Mock(side_effect=ConnectionError('QUIT FAILURE'))
                smtp.close = Mock(side_effect=ConnectionError('CLOSE FAILURE'))
                with self.assertRaises(RuntimeError if fail_at else ConnectionError) as raised:
                    self.audited_consume(q, smtp)
                if fail_at:
                    self.assertEqual(str(raised.exception.__cause__), 'uncertain DATA response')
                else:
                    self.assertEqual(str(raised.exception), 'CLOSE FAILURE')
                self.assertEqual(len(smtp.calls), 1 if fail_at else 3)
                self.assertEqual(set(q.data['jobs/test.json']['receipts'].values()),
                                 {'uncertain' if fail_at else 'accepted'})
                records = json.dumps([v for p, v in q.data.items() if p.startswith('audit/')])
                self.assertIn('QUIT FAILURE', records)
                self.assertIn('CLOSE FAILURE', records)
                if fail_at:
                    self.assertIn('uncertain DATA response', records)
                self.assertFalse(q.data['jobs/test.json'].get('ack', False))
                if not fail_at:
                    fresh = fixtures.SMTP()
                    with patch('smtplib.SMTP_SSL', lambda *a, **k: fresh):
                        bridge.recover(q, 'test')
                    self.assertEqual(fresh.calls, [])

    def test_owner_smtp_data_cleanup_and_teardown_errors_all_survive(self):
        from email.mime.text import MIMEText
        from risk_owner_verification import OwnerSMTP, ERROR_EMAIL
        q, smtp = self.prepare(), fixtures.SMTP(fail_at=1)
        q.data['jobs/owner.json'] = {'mode':'verification', 'status':'pending', 'receipts':{},
                                    'expires_at':(now()+timedelta(hours=1)).isoformat()}
        put = q.put
        def blocked(path, value, sha=None):
            if path == 'jobs/owner.json' and value.get('status') == 'uncertain':
                raise QueueConflict('OWNER UNCERTAIN WRITE FAILURE')
            return put(path, value, sha)
        smtp.quit = Mock(side_effect=ConnectionError('OWNER QUIT FAILURE'))
        smtp.close = Mock(side_effect=ConnectionError('OWNER CLOSE FAILURE'))
        guard = OwnerSMTP(lambda *a, **k: smtp, q, 'jobs/owner.json', ERROR_EMAIL)
        msg = MIMEText('synthetic test'); msg['To'] = ERROR_EMAIL
        def send():
            with guard.factory('smtp.gmail.com', 465) as session:
                session.sendmail(ERROR_EMAIL, [ERROR_EMAIL], msg.as_string())
        with patch.object(q, 'put', blocked), self.assertRaises(RuntimeError) as raised:
            audit.run_audited(q, 'failure-mail', 'owner', send)
        self.assertIsInstance(raised.exception.__cause__, ConnectionError)
        self.assertEqual(str(raised.exception.__cause__), 'uncertain DATA response')
        self.assertEqual(len(smtp.calls), 1)
        records = json.dumps([v for p, v in q.data.items() if p.startswith('audit/')])
        for reason in ('uncertain DATA response', 'OWNER UNCERTAIN WRITE FAILURE',
                       'OWNER QUIT FAILURE', 'OWNER CLOSE FAILURE'):
            self.assertIn(reason, records)

    def test_final_owner_warning_cannot_mask_primary_failure(self):
        q, smtp = self.prepare(), fixtures.SMTP()
        first = ValueError('FIRST RENDER FAILURE')
        with patch.object(self.bot, 'main', side_effect=first), \
                patch.object(self.bot, 'flush_owner_warnings', side_effect=RuntimeError('SECOND WARNING FAILURE')), \
                self.assertRaises(ValueError) as raised:
            self.audited_consume(q, smtp)
        self.assertIs(raised.exception, first)
        self.assertEqual(smtp.calls, [])
        records = json.dumps([v for p, v in q.data.items() if p.startswith('audit/')])
        self.assertIn('FIRST RENDER FAILURE', records)
        self.assertIn('SECOND WARNING FAILURE', records)

    def test_body_fetch_failure_returns_missing_with_specific_reason(self):
        detail = {}
        with patch.object(self.bot.requests.Session, 'get', side_effect=TimeoutError('BODY TIMEOUT')):
            self.assertEqual(self.bot.fetch_article_body('https://example.invalid', detail), '')
        self.assertEqual(detail['code'], 'body_fetch_failed')
        self.assertIsInstance(detail['error'], TimeoutError)


if __name__ == '__main__':
    unittest.main()
