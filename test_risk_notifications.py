"""Failed history/ACK notification stays owner-only and avoids receipt mutations."""
import contextlib
import copy
import io
import json
import os
import runpy
import tempfile
import unittest
from datetime import timedelta
from email import message_from_string
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
from unittest.mock import MagicMock,patch
import risk_subscription as bridge
from risk_owner_verification import OWNER, ERROR_EMAIL
from risk_queue import now
import test_risk_continuity as fixtures


class Notification(unittest.TestCase):
    def test_owner_notification_confirms_acceptance_without_public_details(self):
        bot=MagicMock(); bot.send_email_error.return_value=True
        capture=io.StringIO()
        with patch.dict(os.environ,{'EMAIL_PASSWORD':'offline','GITHUB_RUN_ID':'123'}), \
             patch.object(bridge,'import_bot',return_value=bot), \
             patch.object(bridge,'Queue',side_effect=AssertionError('queue write forbidden')), \
             contextlib.redirect_stdout(capture):
            self.assertTrue(bridge.notify_error(RuntimeError('private source'), 'private context'))
        self.assertEqual(bot.NO_RESULT_RECEIVER,OWNER)
        bot.send_email_error.assert_called_once()
        self.assertIn('actions/runs/123',bot.send_email_error.call_args.args[1])
        self.assertNotIn('private',capture.getvalue())
        self.assertNotIn(OWNER,capture.getvalue())
        self.assertIn('SMTP accepted 1건',capture.getvalue())
    def test_unconfirmed_notification_never_claims_accepted(self):
        bot=MagicMock();bot.send_email_error.return_value=False
        capture=io.StringIO()
        with patch.dict(os.environ,{'EMAIL_PASSWORD':'offline'}), \
             patch.object(bridge,'import_bot',return_value=bot),contextlib.redirect_stdout(capture):
            self.assertFalse(bridge.notify_error(RuntimeError('offline')))
        self.assertNotIn('SMTP accepted',capture.getvalue())
    def test_missing_credentials_cannot_send(self):
        with patch.dict(os.environ,{'EMAIL_PASSWORD':''}),patch.object(bridge,'import_bot') as loader:
            self.assertFalse(bridge.notify_error(RuntimeError('offline')))
            loader.assert_not_called()
    def test_notify_command_does_not_read_queue_or_resend_delivery(self):
        previous=Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp);Path('risk_subscription_config.json').write_text('{"engine":"subscription"}')
                with patch('sys.argv',['risk_subscription.py','notify-error']), \
                     patch.object(bridge,'notify_error') as notify, \
                     patch.object(bridge,'Queue',side_effect=AssertionError('receipt mutation forbidden')):
                    bridge.main();notify.assert_called_once()
            finally:os.chdir(previous)

class ReviewNotifications(unittest.TestCase):
    setUp = fixtures.NativeContinuity.setUp
    tearDown = fixtures.NativeContinuity.tearDown
    fixture = fixtures.NativeContinuity.fixture
    frozen = fixtures.NativeContinuity.frozen
    mixed = fixtures.NativeContinuity.mixed
    run_consume = fixtures.NativeContinuity.run_consume
    ack = fixtures.NativeContinuity.ack

    def delivered(self):
        queue, smtp = self.mixed(), fixtures.native_fixtures.SMTP()
        self.run_consume(queue, smtp)
        return queue, smtp

    def message(self, call):
        recipients, raw = call
        message = message_from_string(raw)
        self.assertEqual(recipients, [ERROR_EMAIL])
        self.assertEqual(parseaddr(message['To'])[1], ERROR_EMAIL)
        self.assertIsNone(message['Cc'])
        self.assertIsNone(message['Bcc'])
        subject = str(make_header(decode_header(message['Subject'])))
        body = message.get_payload()[0].get_payload(decode=True).decode()
        return subject, body

    def test_delivered_with_hold_notifies_only_after_ack_without_runtime_error(self):
        with patch.object(bridge, 'notify_error') as runtime:
            queue, smtp = self.delivered()
            self.assertEqual(len(smtp.calls), 3)
            notices = fixtures.native_fixtures.SMTP()
            self.ack(queue, notices)
            runtime.assert_not_called()
        self.assertEqual(len(notices.calls), 1)
        subject, body = self.message(notices.calls[0])
        self.assertIn('[리스크봇 운영 알림]', subject)
        self.assertNotIn('런타임', subject+body)
        self.assertNotIn('스택 트레이스', body)
        self.assertIn('발송·이력 저장·ACK 완료', body)
        self.assertIn('전체 기사 분석은 미완료', body)
        self.assertIn('https://example.invalid/held', body)
        job = queue.data['jobs/test.json']
        self.assertEqual(job['status'], 'completed')
        self.assertTrue(job['ack'] and job['history_saved'])
        self.assertFalse(job['analysis_complete'])
        self.assertEqual(len(job['receipts']), 3)
        self.assertEqual(queue.data['review/held.json']['status'], 'pending')
        self.assertNotIn('https://example.invalid/held', self.bot.load_seen_urls())

    def test_completed_replay_and_later_job_do_not_duplicate_notice(self):
        queue, smtp = self.delivered()
        self.ack(queue, smtp)
        snapshot = copy.deepcopy(queue.data['jobs/test.json'])
        review = copy.deepcopy(queue.data['review/held.json'])
        self.run_consume(queue, smtp)
        later_job = dict(snapshot, job_id='next-cycle')
        with patch.object(bridge.smtplib, 'SMTP_SSL', return_value=smtp):
            bridge.notify_reviews(queue, later_job)
        self.assertEqual(len(smtp.calls), 4)
        self.assertEqual(queue.data['jobs/test.json'], snapshot)
        self.assertEqual(queue.data['review/held.json'], review)

    def test_first_and_overdue_notice_once_then_no_repeated_age_warning(self):
        queue, smtp = self.delivered()
        self.ack(queue, smtp)
        job = queue.data['jobs/test.json']
        for age in (25, 48, 72):
            queue.data['review/held.json']['first_seen_at'] = (now()-timedelta(hours=age)).isoformat()
            with patch.object(bridge.smtplib, 'SMTP_SSL', return_value=smtp):
                bridge.notify_reviews(queue, job)
        self.assertEqual(len(smtp.calls), 5)  # Three production + initial + overdue.
        self.assertIn('24시간 이상 미확인', self.message(smtp.calls[-1])[1])
        self.assertEqual(queue.data['review/held.json']['status'], 'pending')

    def test_old_unnotified_semantic_hold_is_not_silently_dropped(self):
        queue, smtp = self.delivered()
        queue.data['review/held.json']['first_seen_at'] = (now()-timedelta(days=5)).isoformat()
        self.ack(queue, smtp)
        self.assertEqual(len(smtp.calls), 4)
        self.assertIn('24시간 이상 미확인', self.message(smtp.calls[-1])[1])

    def test_all_null_status_still_gets_distinct_personal_operational_notice(self):
        queue, smtp = self.fixture(), fixtures.native_fixtures.SMTP()
        self.frozen(queue)
        result = queue.data['results/test.json']
        result['classifications'][0]['relevant'] = None
        result['analyses'] = {}
        self.run_consume(queue, smtp)
        self.ack(queue, smtp)
        self.assertEqual(len(smtp.calls), 2)
        self.assertIn('[리스크봇 운영 알림]', self.message(smtp.calls[-1])[0])
        self.assertFalse(queue.data['jobs/test.json']['analysis_complete'])

    def test_routine_missing_and_unreadable_body_never_generate_review_notice(self):
        for status in ('missing', 'unreadable'):
            with self.subTest(status=status):
                queue = self.fixture()
                self.frozen(queue)
                job = queue.data['jobs/test.json']
                job.update(status='completed', ack=True, history_saved=True,
                           deferred_ids=['one'], body_deferred_ids=['one'])
                job['body_manifest'][0].update(body_status=status, body='')
                values, expected, _ = fixtures.continuity.prepare_reviews(queue, job, {'one': None})
                queue.put_many(values, expected)
                with patch.object(self.bot, 'send_email_error') as send:
                    bridge.notify_reviews(queue, job)
                send.assert_not_called()
                self.assertFalse(any(p.startswith('notifications/') for p in queue.data))
                self.assertEqual(queue.data['review/one.json']['status'], 'pending')

    def test_expired_cycle_is_preserved_without_error_or_repeated_operational_warning(self):
        queue = self.fixture()
        queue.data['jobs/test.json']['expires_at'] = (now()-timedelta(seconds=1)).isoformat()
        with patch.object(bridge, 'notify_error') as error, patch.object(self.bot, 'send_email_error') as send:
            bridge.expire_and_check(queue)
            bridge.expire_and_check(queue)
            bridge.notify_reviews(queue, queue.data['jobs/test.json'])
        error.assert_not_called()
        send.assert_not_called()
        self.assertEqual(queue.data['jobs/test.json']['status'], 'expired')
        self.assertEqual(queue.data['review/index.json']['pending'], ['one'])
        self.assertEqual(queue.data['review/one.json']['reason'], 'expired_unprocessed')

    def test_actual_delivery_exception_with_hold_stays_exception_without_review_notice(self):
        queue, smtp = self.mixed(), fixtures.native_fixtures.SMTP(fail_at=1)
        with patch.object(bridge, 'notify_reviews') as notify:
            with self.assertRaisesRegex(RuntimeError, 'SMTP DATA'):
                self.run_consume(queue, smtp)
        notify.assert_not_called()
        self.assertEqual(queue.data['jobs/test.json']['status'], 'uncertain')
        self.assertEqual(queue.data['review/held.json']['status'], 'pending')

    def test_history_failure_cannot_announce_completed_review(self):
        queue, smtp = self.delivered()
        queue.remote_seen = b'{}'
        with patch.object(bridge, 'notify_reviews') as notify:
            with self.assertRaisesRegex(RuntimeError, '이력'):
                bridge.acknowledge(queue, 'test')
        notify.assert_not_called()
        self.assertEqual(queue.data['jobs/test.json']['status'], 'delivery_complete')
        self.assertEqual(len(smtp.calls), 3)

    def test_uncertain_notice_is_not_resent_and_never_reverts_completed_delivery(self):
        queue, smtp = self.delivered()
        failed_notice = fixtures.native_fixtures.SMTP(fail_at=1)
        with self.assertRaisesRegex(RuntimeError, '운영 알림 SMTP'):
            self.ack(queue, failed_notice)
        job = queue.data['jobs/test.json']
        self.assertEqual(job['status'], 'completed')
        self.assertTrue(job['ack'] and job['history_saved'])
        self.assertEqual(len(job['receipts']), 3)
        states = [v['status'] for p, v in queue.data.items() if p.startswith('notifications/')]
        self.assertEqual(states, ['uncertain'])
        self.run_consume(queue, smtp)
        self.assertEqual(len(smtp.calls), 3)

    def test_notice_reservation_failure_is_real_error_before_notice_smtp(self):
        queue, smtp = self.delivered()
        put_many = queue.put_many
        def fail(values, expected):
            if any(p.startswith('notifications/') for p in values):
                raise RuntimeError('notification queue offline')
            return put_many(values, expected)
        with patch.object(queue, 'put_many', fail), self.assertRaisesRegex(RuntimeError, 'queue offline'):
            self.ack(queue, smtp)
        self.assertEqual(len(smtp.calls), 3)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'completed')

    def test_notice_reservation_readback_mismatch_blocks_smtp(self):
        queue, smtp = self.delivered()
        get = queue.get
        def changed(path, ref=None):
            value, sha = get(path, ref=ref)
            if path.startswith('notifications/') and value:
                value['attempt_id'] = 'other-worker'
            return value, sha
        with patch.object(queue, 'get', changed), self.assertRaisesRegex(RuntimeError, '선기록 불일치'):
            self.ack(queue, smtp)
        self.assertEqual(len(smtp.calls), 3)

    def test_verified_commit_evidence_is_private_and_required_before_smtp(self):
        from risk_audit import ACTIVE, Audit
        for fail_evidence in (False,True):
            with self.subTest(fail_evidence=fail_evidence):
                queue,smtp=self.delivered()
                audit=Audit(queue,'ack','test')
                token=ACTIVE.set(audit)
                put=queue.put
                def save(path,value,sha=None):
                    if (fail_evidence and path.startswith('audit/')
                            and value['events'][-1].get('facts',{}).get('reservation_commit')):
                        raise RuntimeError('private audit unavailable')
                    return put(path,value,sha)
                try:
                    with patch.object(queue,'put',save):
                        if fail_evidence:
                            with self.assertRaisesRegex(RuntimeError,'감사 기록 저장 실패'):
                                self.ack(queue,smtp)
                        else:self.ack(queue,smtp)
                finally:ACTIVE.reset(token)
                self.assertTrue(queue.data['jobs/test.json']['ack'])
                self.assertEqual(len(smtp.calls),3 if fail_evidence else 4)
                if not fail_evidence:
                    saved=queue.data[audit.path]
                    event=next(e for e in saved['events'] if e.get('facts',{}).get('reservation_commit'))
                    self.assertEqual(event['stage'],'owner_notification')
                    facts=event['facts']
                    self.assertEqual(facts['reservation_commit'],facts['read_ref'])
                    self.assertEqual(facts['notification_count'],1)
                    self.assertEqual(len(facts['verified_blob_shas']),1)
                    self.assertNotIn('https://example.invalid/held',json.dumps(facts))
                Path('seen_news.json').write_bytes(self.before)

    def test_confirmed_commit_read_ignores_stale_moving_main(self):
        queue, smtp = self.delivered()
        get = queue.get
        pinned = []
        def stale(path, ref=None):
            if path.startswith('notifications/'):
                if ref is None:
                    return None, None  # stale moving main, including after write
                pinned.append(ref)
            return get(path, ref=ref)
        with patch.object(queue, 'get', stale):
            self.ack(queue, smtp)
        self.assertEqual(len(smtp.calls), 4)
        self.assertEqual(len(pinned), 1)
        self.assertRegex(pinned[0], r'^[0-9a-f]{40}$')
        self.assertTrue(queue.data['jobs/test.json']['ack'])

    def test_entire_initial_and_overdue_batch_verified_before_single_notice(self):
        queue,smtp=self.delivered()
        job=queue.data['jobs/test.json']
        original=queue.data['review/held.json']
        # prepare_reviews supplies source-bound records for three extra held IDs.
        for i in range(3):
            aid='held'+str(i)
            record=copy.deepcopy(original)
            record['article_id']=aid
            record['item']['article_id']=aid
            record['item']['url']='https://example.invalid/'+aid
            record['body']['article_id']=aid
            record['body']['source_url']=record['item']['url']
            # read_review verifies source_hash against its source object.
            record['source_hash']=bridge.digest({'item':record['item'],'body':record['body']})
            if i==2:record['first_seen_at']=(now()-timedelta(hours=25)).isoformat()
            queue.data['review/'+aid+'.json']=record
            job['deferred_ids'].append(aid)
        checked=[]
        get=queue.get
        def inspect(path,ref=None):
            value=get(path,ref=ref)
            if ref and path.startswith('notifications/'):checked.append(path)
            return value
        sender=self.bot.send_email_error
        def send(*args,**kwargs):
            self.assertEqual(len(checked),4)
            return sender(*args,**kwargs)
        with patch.object(queue,'get',inspect),patch.object(self.bot,'send_email_error',send):
            self.ack(queue,smtp)
        self.assertEqual(len(smtp.calls),4)
        states=[v for p,v in queue.data.items() if p.startswith('notifications/')]
        self.assertEqual([v['phase'] for v in states].count('initial'),3)
        self.assertEqual([v['phase'] for v in states].count('overdue'),1)
        self.assertTrue(all(v['status']=='accepted' for v in states))

    def test_invalid_or_ambiguous_reservation_commit_never_sends_or_retries(self):
        for commit in (None, '', 'main', 'a'*39, {'sha':'a'*40}):
            with self.subTest(commit=commit):
                queue, smtp = self.delivered()
                original = queue.put_many
                calls = []
                def unconfirmed(values, expected):
                    value = original(values, expected)
                    if any(p.startswith('notifications/') for p in values):
                        calls.append(1)
                        return commit
                    return value
                with patch.object(queue, 'put_many', unconfirmed), \
                     self.assertRaisesRegex(RuntimeError, 'commit 미확인'):
                    self.ack(queue, smtp)
                self.assertEqual(calls, [1])
                self.assertEqual(len(smtp.calls), 3)
                self.assertTrue(queue.data['jobs/test.json']['ack'])
                with patch.object(bridge.smtplib, 'SMTP_SSL', return_value=smtp):
                    bridge.notify_reviews(queue, queue.data['jobs/test.json'])
                self.assertEqual(len(smtp.calls), 3)
                Path('seen_news.json').write_bytes(self.before)

    def test_pinned_read_failures_preserve_completed_ack_and_private_evidence(self):
        for fault in ('missing', 'read_error', 'wrong_sha', 'wrong_attempt'):
            with self.subTest(fault=fault):
                queue, smtp = self.delivered()
                get = queue.get
                def broken(path, ref=None):
                    value, sha = get(path, ref=ref)
                    if ref is not None and path.startswith('notifications/'):
                        if fault == 'missing': return None, None
                        if fault == 'read_error': raise RuntimeError('private GET unavailable')
                        if fault == 'wrong_sha': return value, '0'*40
                        value['attempt_id'] = 'other-worker'
                    return value, sha
                with patch.object(queue, 'get', broken), patch.object(bridge, 'record_error') as audit, \
                     self.assertRaises(RuntimeError):
                    self.ack(queue, smtp)
                self.assertEqual(len(smtp.calls), 3)
                self.assertTrue(queue.data['jobs/test.json']['ack'])
                self.assertEqual(audit.call_args.args[0], 'owner_notification')
                self.assertEqual(audit.call_args.args[2], 'review_reservation_readback_failed')
                facts = audit.call_args.kwargs
                self.assertEqual(facts['reservation_commit'], facts['read_ref'])
                self.assertRegex(facts['expected_blob_sha'], r'^[0-9a-f]{40}$')
                Path('seen_news.json').write_bytes(self.before)

    def test_receipt_write_failure_after_notice_keeps_reservation_and_prevents_resend(self):
        queue, smtp = self.delivered()
        put_many = queue.put_many
        def fail(values, expected):
            if any(p.startswith('notifications/') and v['status'] == 'accepted' for p, v in values.items()):
                raise RuntimeError('notice receipt offline')
            return put_many(values, expected)
        with patch.object(queue, 'put_many', fail), self.assertRaisesRegex(RuntimeError, 'receipt offline'):
            self.ack(queue, smtp)
        self.assertEqual(len(smtp.calls), 4)
        self.run_consume(queue, smtp)
        self.assertEqual(len(smtp.calls), 4)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'completed')

    def test_delivery_complete_recovery_sends_no_production_mail_and_notifies_after_ack(self):
        queue, smtp = self.delivered()
        queue.data['jobs/test.json']['created_at'] = now().isoformat()
        with patch.object(bridge.smtplib, 'SMTP_SSL', return_value=smtp):
            bridge.recover(queue, 'test')
        self.assertEqual(len(smtp.calls), 3)
        self.ack(queue, smtp)
        with patch.object(bridge.smtplib, 'SMTP_SSL', return_value=smtp):
            bridge.recover(queue, 'test')
        self.assertEqual(len(smtp.calls), 4)
        self.assertTrue(queue.data['jobs/test.json']['ack'])

    def test_resolved_before_notification_is_skipped(self):
        queue, smtp = self.delivered()
        queue.data['review/held.json']['status'] = 'resolved'
        self.ack(queue, smtp)
        self.assertEqual(len(smtp.calls), 3)

    def test_no_credentials_does_not_mark_notified_and_next_attempt_can_notify(self):
        queue, smtp = self.delivered()
        with patch.dict(os.environ, {'EMAIL_PASSWORD': ''}), self.assertRaisesRegex(RuntimeError, 'SMTP 설정 누락'):
            self.ack(queue, smtp)
        self.assertEqual(len(smtp.calls), 3)
        self.assertFalse(any(p.startswith('notifications/') for p in queue.data))
        self.run_consume(queue, smtp)
        self.assertEqual(len(smtp.calls), 4)

    def test_runtime_error_keeps_error_template_and_private_destination(self):
        smtp = fixtures.native_fixtures.SMTP()
        with patch.object(bridge.smtplib, 'SMTP_SSL', return_value=smtp):
            self.assertTrue(bridge.notify_error(RuntimeError('SMTP test failure'), 'test traceback'))
        subject, body = self.message(smtp.calls[0])
        self.assertIn('[리스크봇 오류]', subject)
        self.assertIn('런타임 오류 발생', subject)
        self.assertIn('스택 트레이스', body)

    def test_cli_error_reports_notified_only_after_smtp_acceptance(self):
        Path('risk_subscription_config.json').write_text('{"engine":"subscription"}')
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                output = Path('ack-output-'+str(accepted))
                with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}), \
                        patch('sys.argv', ['risk_subscription.py', 'ack', '--job-id', 'test']), \
                        patch('risk_queue.Queue', side_effect=RuntimeError('queue offline')), \
                        patch.object(self.bot, 'send_email_error', return_value=accepted), \
                        self.assertRaises(SystemExit) as exit_code:
                    runpy.run_path(str(fixtures.native_fixtures.ROOT/'risk_subscription.py'), run_name='__main__')
                self.assertEqual(exit_code.exception.code, 1)
                self.assertEqual(output.read_text() if output.exists() else '',
                                 'error_notified=true\n' if accepted else '')


if __name__=='__main__':unittest.main()
