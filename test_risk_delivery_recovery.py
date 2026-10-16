"""Real native delivery + queue conflicts; all SMTP/HTTP are local mocks."""
import copy
import contextlib
import io
import json
import os
import smtplib
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import risk_subscription as bridge
import risk_subscription_request as request
from risk_delivery import address_key
from risk_outbox import sha, store_packet
from risk_queue import Queue, QueueConflict, QueueReadUnavailable, digest, now
import test_risk_subscription as fixtures


class Conflicts(unittest.TestCase):
    def test_transient_get_exhaustion_keeps_safe_http_diagnostics(self):
        q = object.__new__(Queue); q.token = 'not-a-real-secret'
        error = urllib.error.HTTPError('https://api.github.com', 503, 'Unavailable',
            {'X-GitHub-Request-Id': 'ABCD:1234'}, io.BytesIO(b'PRIVATE RESPONSE'))
        with patch('risk_queue.urllib.request.urlopen', side_effect=error) as call, \
                patch('risk_queue.time.sleep'), self.assertRaises(QueueReadUnavailable) as raised:
            q.request('/contents/test')
        self.assertEqual(call.call_count, 3)
        self.assertEqual((raised.exception.code, raised.exception.request_id), (503, 'ABCD:1234'))
        self.assertNotIn('PRIVATE', str(raised.exception))

    def test_timeout_put_is_never_retried_or_treated_as_read(self):
        q = object.__new__(Queue); q.token = 'not-a-real-secret'
        with patch('risk_queue.urllib.request.urlopen', side_effect=TimeoutError) as call, \
                self.assertRaises(RuntimeError) as raised:
            q.request('/contents/test', 'PUT', {})
        self.assertNotIsInstance(raised.exception, QueueReadUnavailable)
        self.assertEqual(call.call_count, 1)

    def test_same_file_branch_conflict_retries_identical_cas(self):
        q = object.__new__(Queue)
        q.request = Mock(side_effect=[QueueConflict('request-id'), {'ok': True}])
        q.get_fresh = Mock(return_value=({'old': True}, 'old'))
        with patch('risk_queue.time.sleep'):
            self.assertEqual(q.put('jobs/test.json', {'new': True}, 'old'), {'ok': True})
        self.assertEqual(q.request.call_count, 2)
        self.assertEqual(q.request.call_args_list[0], q.request.call_args_list[1])

    def test_changed_file_or_receipt_is_never_overwritten(self):
        q = object.__new__(Queue)
        q.request = Mock(side_effect=QueueConflict())
        q.get_fresh = Mock(return_value=({'receipts': {'other': 'accepted'}}, 'changed'))
        with self.assertRaises(QueueConflict):
            q.put('jobs/test.json', {'receipts': {}}, 'old')
        self.assertEqual(q.request.call_count, 1)

    def test_confirmed_exact_write_is_read_back_not_written_again(self):
        q = object.__new__(Queue)
        q.request = Mock(side_effect=QueueConflict())
        value = {'delivery_revision': 'unique-operation', 'receipts': {'one': 'sending'}}
        q.get_fresh = Mock(return_value=(value, 'applied'))
        self.assertTrue(q.put('jobs/test.json', value, 'old')['confirmed_existing'])
        self.assertEqual(q.request.call_count, 1)

    def test_conflict_retries_are_bounded(self):
        q = object.__new__(Queue)
        q.request = Mock(side_effect=QueueConflict())
        q.get_fresh = Mock(return_value=({'old': True}, 'old'))
        with patch('risk_queue.time.sleep'), self.assertRaises(QueueConflict):
            q.put('jobs/test.json', {'new': True}, 'old')
        self.assertEqual(q.request.call_count, 3)

    def test_ambiguous_network_error_is_not_a_conflict_retry(self):
        q = object.__new__(Queue)
        q.request = Mock(side_effect=RuntimeError('ambiguous write'))
        q.get_fresh = Mock()
        with self.assertRaises(RuntimeError):
            q.put('jobs/test.json', {'new': True}, 'old')
        q.get_fresh.assert_not_called()
        self.assertEqual(q.request.call_count, 1)

    def test_conflict_response_exposes_only_allowlisted_diagnostics(self):
        q = object.__new__(Queue); q.token = 'not-a-real-secret'
        error = urllib.error.HTTPError('https://api.github.com', 409, 'Conflict',
            {'X-GitHub-Request-Id': 'ABCD:1234'}, io.BytesIO(b'{"message":"sha mismatch; PRIVATE ARTICLE ADDRESS"}'))
        with patch('risk_queue.urllib.request.urlopen', side_effect=error), self.assertRaises(QueueConflict) as raised:
            q.request('/contents/test', 'PUT', {})
        self.assertIn('ABCD:1234', str(raised.exception))
        self.assertIn('sha_conflict', str(raised.exception))
        self.assertNotIn('PRIVATE', str(raised.exception))
        self.assertNotIn(q.token, str(raised.exception))

    def test_fresh_read_pins_contents_to_commit(self):
        q = object.__new__(Queue)
        q.request = Mock(return_value={'object': {'sha': 'fixed-head'}})
        q.get = Mock(return_value=({'receipts': {}}, 'sha'))
        q.get_fresh('jobs/test.json')
        q.get.assert_called_once_with('jobs/test.json', ref='fixed-head')

    def test_atomic_batch_rebases_unrelated_ref_race_with_intact_file_leases(self):
        q = object.__new__(Queue)
        head = ['one']; refs = []; bases = []
        def api(path, method='GET', data=None):
            if path == '/git/ref/heads/main': return {'object': {'sha': head[0]}}
            if path.startswith('/git/commits/') and method == 'GET': return {'tree': {'sha': head[0]+'-tree'}}
            if path == '/git/trees': bases.append(data['base_tree']); return {'sha': 'new-tree'}
            if path == '/git/commits': return {'sha': head[0]+'-child'}
            if method == 'PATCH':
                refs.append(data)
                if len(refs) == 1:
                    head[0] = 'other-bot'
                    raise QueueConflict(method='PATCH', code=422)
                return {}
            raise AssertionError(path)
        q.request = api; q.get = Mock(return_value=({'old': True}, 'unchanged'))
        with patch('risk_queue.time.sleep'):
            q.put_many({'jobs/test.json': {'ack': True}}, {'jobs/test.json': 'unchanged'})
        self.assertEqual(bases, ['one-tree', 'other-bot-tree'])
        self.assertEqual(len(refs), 2)
        self.assertTrue(all(r['force'] is False for r in refs))
        self.assertEqual(q.get.call_args.kwargs, {'ref': 'other-bot'})


class Outbox(unittest.TestCase):
    def queue(self):
        q = object.__new__(Queue)
        q.get_fresh = Mock(return_value=(None, None))
        q.get = Mock()
        q.put = Mock(return_value={'commit': {'sha': 'a'*40}})
        return q

    def test_put_commit_is_verified_even_if_main_still_reports_missing(self):
        q = self.queue(); value = {'job_id': 'test'}
        q.get.return_value = (value, 'blob')
        store_packet(q, value)
        q.get.assert_called_once_with('outbox/test.json', ref='a'*40)
        q.get_fresh.assert_called_once_with('outbox/test.json')
        q.put.assert_called_once_with('outbox/test.json', value)

    def test_missing_or_transient_read_rechecks_without_rewriting(self):
        for first in [(None, None), QueueReadUnavailable('temporary', 503, 'ABCD:1234')]:
            with self.subTest(first=first):
                q = self.queue(); value = {'job_id': 'test'}
                q.get.side_effect = [first, (value, 'blob')]
                with patch('risk_outbox.time.sleep') as sleep:
                    store_packet(q, value)
                self.assertEqual(q.get.call_count, 2)
                self.assertEqual(q.get.call_args_list[0], q.get.call_args_list[1])
                self.assertEqual(q.put.call_count, 1)
                sleep.assert_called_once_with(1)

    def test_persistently_missing_read_is_bounded_and_fails_closed(self):
        q = self.queue(); q.get.return_value = (None, None)
        with patch('risk_outbox.time.sleep'), self.assertRaisesRegex(RuntimeError, 'SMTP 금지'):
            store_packet(q, {'job_id': 'test'})
        self.assertEqual(q.get.call_count, 3)
        self.assertEqual(q.put.call_count, 1)

    def test_changed_content_or_integrity_or_auth_error_never_retries(self):
        for response in [({'job_id': 'test', 'html': 'other'}, 'blob'),
                         RuntimeError('비공개 큐 blob 해시 불일치'),
                         RuntimeError('비공개 큐 GET HTTP 403')]:
            with self.subTest(response=response):
                q = self.queue()
                q.get.side_effect = [response]
                with patch('risk_outbox.time.sleep') as sleep, self.assertRaises(RuntimeError):
                    store_packet(q, {'job_id': 'test'})
                self.assertEqual(q.get.call_count, 1)
                self.assertEqual(q.put.call_count, 1)
                sleep.assert_not_called()

    def test_failed_or_unknown_put_never_becomes_read_or_write_retry(self):
        for error in [RuntimeError('PUT HTTP 403'), RuntimeError('PUT response lost'), QueueConflict()]:
            with self.subTest(error=error):
                q = self.queue(); q.put.side_effect = error
                with self.assertRaises(RuntimeError): store_packet(q, {'job_id': 'test'})
                self.assertEqual(q.put.call_count, 1)
                q.get.assert_not_called()
                self.assertEqual(q.get_fresh.call_count, 1)

    def test_existing_packet_is_immutable_and_never_republished(self):
        for existing in [{'job_id': 'test'}, {'job_id': 'test', 'html': 'other'}]:
            q = self.queue(); q.get_fresh.return_value = (existing, 'blob')
            if existing == {'job_id': 'test'}:
                store_packet(q, existing)
            else:
                with self.assertRaisesRegex(RuntimeError, '덮어쓰기 금지'):
                    store_packet(q, {'job_id': 'test'})
            q.put.assert_not_called()

    def test_conflict_confirmed_existing_still_requires_fresh_read(self):
        q = self.queue(); value = {'job_id': 'test'}
        q.put.return_value = {'confirmed_existing': True, 'content': {'sha': 'blob'}}
        q.get_fresh.side_effect = [(None, None), (value, 'blob')]
        store_packet(q, value)
        self.assertEqual(q.get_fresh.call_count, 2)
        q.get.assert_not_called()


class Recovery(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    tearDown = fixtures.Integration.tearDown
    run_consume = fixtures.Integration.run_consume

    def fixture(self):
        queue = fixtures.Integration.fixture(self)
        queue.data['jobs/test.json']['created_at'] = now().isoformat()
        return queue

    def partial(self):
        q, smtp = self.fixture(), fixtures.SMTP()
        put = q.put
        def fail_third(path, value, old=None):
            if (path == 'jobs/test.json' and len(value.get('receipts', {})) == 3
                    and 'sending' in value['receipts'].values()):
                raise QueueConflict()
            return put(path, value, old)
        with patch.object(q, 'put', fail_third), self.assertRaises(QueueConflict):
            self.run_consume(q, smtp)
        self.assertEqual(len(smtp.calls), 2)
        self.assertEqual(q.data['jobs/test.json']['status'], 'uncertain')
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        return q, smtp

    def resume(self, q, smtp):
        with patch.object(smtplib, 'SMTP_SSL', lambda *a, **k: smtp):
            bridge.recover(q, 'test')

    def test_owner_status_recovery_restores_only_signed_owner_route(self):
        from risk_continuity import freeze_exposure
        from risk_owner_verification import OWNER
        q, smtp = self.fixture(), fixtures.SMTP()
        job = q.data['jobs/test.json']
        q.data.update(freeze_exposure(job, Path('exposure_data.csv').read_bytes(), now().isoformat()))
        q.data['results/test.json']['classifications'][0]['relevant'] = None
        q.data['results/test.json']['analyses'] = {}
        with patch.object(bridge, 'finish_packet', side_effect=RuntimeError('offline before SMTP')), \
             patch.object(bridge, 'notify_best_effort'), self.assertRaisesRegex(RuntimeError, 'offline'):
            self.run_consume(q, smtp)
        self.assertEqual(smtp.calls, [])
        frozen = copy.deepcopy(q.data['outbox/test.json'])
        self.assertEqual(frozen['recipient_route'], 'owner_status')
        self.bot.NO_RESULT_RECEIVER = 'executive-admin@example.invalid'
        self.resume(q, smtp)
        self.assertEqual([c[0] for c in smtp.calls], [[OWNER]])
        self.assertEqual(q.data['outbox/test.json'], frozen)
        self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_complete')

    def test_immutable_plan_and_history_exist_before_any_smtp(self):
        q, smtp = self.fixture(), fixtures.SMTP()
        send = smtp.sendmail
        def inspect(*args):
            packet = q.data['outbox/test.json']
            self.assertEqual(packet['seen_after_hash'], sha(packet['seen_after_snapshot']))
            self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
            self.assertIn('한국투자증권', packet['html'])
            return send(*args)
        smtp.sendmail = inspect
        self.run_consume(q, smtp)
        self.assertEqual(len(smtp.calls), 3)
        self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_complete')

    def test_partial_two_accepted_recovers_only_unattempted_third_and_acks(self):
        q, original = self.partial(); smtp = fixtures.SMTP()
        frozen = copy.deepcopy(q.data['outbox/test.json'])
        with patch.object(self.bot, 'main', side_effect=AssertionError('rerender forbidden')):
            self.resume(q, smtp)
        self.assertEqual([c[0] for c in smtp.calls], [['cc@example.invalid']])
        self.assertEqual(q.data['outbox/test.json'], frozen)
        self.assertEqual(Path('seen_news.json').read_text(), frozen['seen_after_snapshot'])
        self.assertEqual(set(q.data['jobs/test.json']['receipts'].values()), {'accepted'})
        self.assertEqual(q.data['jobs/test.json']['attempted_count'], 3)
        q.remote_seen = Path('seen_news.json').read_bytes()
        bridge.acknowledge(q, 'test')
        self.assertTrue(q.data['jobs/test.json']['ack'])
        self.resume(q, smtp)
        self.assertEqual(len(smtp.calls), 1)

    def test_all_accepted_recovery_restores_history_without_any_smtp(self):
        q, smtp = self.fixture(), fixtures.SMTP()
        self.run_consume(q, smtp)
        Path('seen_news.json').write_bytes(self.before)
        fresh = fixtures.SMTP()
        self.resume(q, fresh)
        self.assertEqual(fresh.calls, [])
        self.assertEqual(Path('seen_news.json').read_text(), q.data['outbox/test.json']['seen_after_snapshot'])

    def test_uncertain_or_reserved_recipient_is_never_retried(self):
        for state in ('uncertain', 'sending', 'rejected'):
            with self.subTest(state=state):
                q, _ = self.partial()
                q.data['jobs/test.json']['receipts'][address_key('cc@example.invalid')] = state
                q.data['jobs/test.json']['attempted_count'] = 3
                smtp = fixtures.SMTP()
                with self.assertRaises(RuntimeError): self.resume(q, smtp)
                self.assertEqual(smtp.calls, [])

    def test_smtp_data_exception_stays_uncertain_with_no_recovery(self):
        q, smtp = self.fixture(), fixtures.SMTP(fail_at=1)
        with self.assertRaises(RuntimeError): self.run_consume(q, smtp)
        with self.assertRaises(RuntimeError): self.resume(q, smtp)
        self.assertEqual(len(smtp.calls), 1)
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_outbox_storage_failure_never_sends_or_writes_main(self):
        q, smtp = self.fixture(), fixtures.SMTP(); put = q.put
        def fail(path, value, old=None):
            if path.startswith('outbox/'): raise RuntimeError('storage failed')
            return put(path, value, old)
        with patch.object(q, 'put', fail), self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        self.assertEqual(smtp.calls, [])
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        self.run_consume(q, smtp)
        self.assertEqual(len(smtp.calls), 3)

    def test_put_saved_but_read_missing_blocks_then_recovers_exactly_once(self):
        q, smtp = self.fixture(), fixtures.SMTP()
        def missing(path):
            return (None, None) if path.startswith('outbox/') else q.get(path)
        with patch.object(q, 'get_fresh', missing, create=True), patch('risk_outbox.time.sleep'), \
                self.assertRaisesRegex(RuntimeError, '저장 확인 실패'):
            self.run_consume(q, smtp)
        frozen = copy.deepcopy(q.data['outbox/test.json'])
        self.assertEqual(smtp.calls, [])
        self.assertEqual(q.data['jobs/test.json']['receipts'], {})
        self.assertEqual(q.data['jobs/test.json']['status'], 'pending')
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        with patch.object(self.bot, 'main', side_effect=AssertionError('rerender forbidden')):
            self.run_consume(q, smtp)
        self.assertEqual(len(smtp.calls), 3)
        q.remote_seen = Path('seen_news.json').read_bytes()
        bridge.acknowledge(q, 'test')
        completed = copy.deepcopy(q.data['jobs/test.json'])
        self.resume(q, smtp)
        self.run_consume(q, smtp)
        self.assertEqual(len(smtp.calls), 3)
        self.assertEqual(q.data['outbox/test.json'], frozen)
        self.assertEqual(q.data['jobs/test.json'], completed)

    def test_transient_post_put_get_never_sends_before_exact_readback(self):
        q, smtp = self.fixture(), fixtures.SMTP(); reads = []
        def fresh(path):
            if path.startswith('outbox/') and path in q.data:
                reads.append(path)
                if len(reads) == 1: raise QueueReadUnavailable('temporary')
            return q.get(path)
        send = smtp.sendmail
        def inspect(*args):
            self.assertEqual(len(reads), 2)
            return send(*args)
        smtp.sendmail = inspect
        with patch.object(q, 'get_fresh', fresh, create=True), patch('risk_outbox.time.sleep'):
            self.run_consume(q, smtp)
        self.assertEqual(len(smtp.calls), 3)

    def test_ambiguous_put_after_storage_stops_without_any_smtp(self):
        q, smtp = self.fixture(), fixtures.SMTP(); put = q.put; outbox_writes = []
        def lost_response(path, value, old=None):
            result = put(path, value, old)
            if path.startswith('outbox/'):
                outbox_writes.append(path)
                raise RuntimeError('PUT response lost')
            return result
        with patch.object(q, 'put', lost_response), self.assertRaisesRegex(RuntimeError, 'response lost'):
            self.run_consume(q, smtp)
        self.assertEqual(outbox_writes, ['outbox/test.json'])
        self.assertEqual(smtp.calls, [])
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        self.resume(q, smtp)
        self.assertEqual(len(smtp.calls), 3)

    def test_concurrent_recovery_job_change_blocks_losing_worker(self):
        q, _ = self.partial(); smtp = fixtures.SMTP(); put = q.put
        def competing_worker(path, value, old=None):
            if path == 'jobs/test.json' and value.get('recovery_count'):
                q.data[path]['delivery_revision'] = 'another-worker'
            return put(path, value, old)
        with patch.object(q, 'put', competing_worker), self.assertRaisesRegex(RuntimeError, 'queue changed'):
            self.resume(q, smtp)
        self.assertEqual(smtp.calls, [])
        self.assertEqual(len(q.data['jobs/test.json']['receipts']), 2)
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_login_failure_reuses_frozen_packet_without_reanalysis(self):
        q = self.fixture()
        with self.assertRaises(smtplib.SMTPAuthenticationError):
            self.run_consume(q, fixtures.SMTP(login_fail=True))
        packet = copy.deepcopy(q.data['outbox/test.json'])
        smtp = fixtures.SMTP()
        with patch.object(self.bot, 'main', side_effect=AssertionError('rerender forbidden')):
            self.run_consume(q, smtp)
        self.assertEqual(len(smtp.calls), 3)
        self.assertEqual(packet, q.data['outbox/test.json'])

    def test_owner_zero_data_retry_reuses_plan_and_preserves_operating_history(self):
        q = fixtures.Integration.fixture(self, 'verification')
        with self.assertRaises(smtplib.SMTPAuthenticationError):
            self.run_consume(q, fixtures.SMTP(login_fail=True), True)
        packet = copy.deepcopy(q.data['outbox/test.json'])
        smtp = fixtures.SMTP()
        with patch.object(self.bot, 'main', side_effect=AssertionError('rerender forbidden')):
            self.run_consume(q, smtp, True)
        self.assertEqual(len(smtp.calls), 1)
        self.assertEqual(q.data['jobs/test.json']['status'], 'verified')
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        self.assertEqual(packet, q.data['outbox/test.json'])

    def test_changed_history_or_packet_or_targets_blocks_recovery(self):
        for changed in ('history', 'packet', 'targets'):
            with self.subTest(changed=changed):
                q, _ = self.partial(); smtp = fixtures.SMTP()
                if changed == 'history': q.remote_seen = b'{"changed":true}'
                if changed == 'packet': q.data['outbox/test.json']['html'] += 'tampered'
                if changed == 'targets': self.bot.EMAIL_CC = ['changed@example.invalid']
                with self.assertRaises(RuntimeError): self.resume(q, smtp)
                self.assertEqual(smtp.calls, [])
                self.bot.EMAIL_CC = ['cc@example.invalid']

    def test_legacy_adoption_matches_saved_html_and_marks_only_delivered_urls(self):
        q, _ = self.partial()
        packet = q.data.pop('outbox/test.json')
        job, result = q.data['jobs/test.json'], q.data['results/test.json']
        q.data['recoveries/test.json'] = {'schema_version': 1, 'job_id': 'test',
            'job_hash': digest(job), 'result_hash': digest(result),
            'preview_hash': digest(q.data['previews/test.json']),
            'gmail_html_sha256': sha(packet['html']), 'subject': packet['subject'],
            'confirmed_accepted_keys': sorted(job['receipts']), 'gmail_message_ids': ['one', 'two'],
            'delivered_article_ids': ['one']}
        smtp = fixtures.SMTP()
        self.resume(q, smtp)
        self.assertEqual([c[0] for c in smtp.calls], [['cc@example.invalid']])
        self.assertTrue(q.data['outbox/test.json']['legacy_adoption'])
        self.assertEqual(q.data['outbox/test.json']['html'], packet['html'])
        self.assertIn('https://example.invalid/test/one', self.bot.load_seen_urls())

    def test_legacy_without_gmail_confirmation_does_not_unlock(self):
        q, _ = self.partial(); q.data.pop('outbox/test.json')
        smtp = fixtures.SMTP()
        with self.assertRaises(RuntimeError): self.resume(q, smtp)
        self.assertEqual(smtp.calls, [])


class Request(unittest.TestCase):
    def test_failure_exposes_only_allowlisted_outbox_metadata(self):
        value = {'operation': 'consume', 'job_id': 'test', 'self_test': False,
                 'dry_run': False, 'request_id': 'outbox-read'}
        safe = 'RISK_OUTBOX_VERIFY phase=read source=put_commit attempt=3 outcome=missing commit='+'a'*40+' http=0 request_id=unconfirmed'
        private = 'RISK_OUTBOX_VERIFY PRIVATE ARTICLE ADDRESS TOKEN'
        error = request.subprocess.CalledProcessError(1, ['child'], output=safe+'\n'+private,
                                                     stderr='PRIVATE TRACEBACK')
        output = io.StringIO()
        with patch.object(request.Path, 'read_text', return_value=json.dumps(value)), \
                patch.object(request.subprocess, 'run', side_effect=error), \
                contextlib.redirect_stdout(output), self.assertRaises(request.subprocess.CalledProcessError):
            request.main()
        self.assertEqual(output.getvalue(), safe+'\n')

    def test_owner_diagnostic_after_summary_preserves_history_ack_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'output'
            value = {'operation':'consume', 'job_id':'test', 'self_test':False,
                     'dry_run':False, 'request_id':'review-summary'}
            summary = json.dumps({'job_id':'test', 'status':'delivery_complete', 'paid_ai_calls':0})
            stdout = summary + '\n담당자 오류 알림 SMTP accepted 1건; 운영 영수증 변경0/API0\n'
            with patch.object(request.Path, 'read_text', return_value=json.dumps(value)), \
                    patch.object(request.subprocess, 'run', return_value=SimpleNamespace(stdout=stdout)), \
                    patch.dict(os.environ, GITHUB_OUTPUT=str(path)):
                request.main()
            self.assertIn('needs_ack=true', path.read_text())
            self.assertIn('job_id=test', path.read_text())

    def test_missing_or_duplicate_summary_never_guesses_completion(self):
        summary = json.dumps({'job_id':'test', 'status':'delivery_complete', 'paid_ai_calls':0})
        for output in ['담당자 알림만 있음', summary + '\n' + summary]:
            with self.subTest(output=output), self.assertRaisesRegex(RuntimeError, '요약 누락·중복'):
                request.read_summary(output)

    def test_review_wake_is_fixed_collection_only_without_ack(self):
        value = {'operation': 'collect-review', 'job_id': 'pending-review', 'self_test': False,
                 'dry_run': False, 'request_id': 'review-test'}
        result = json.dumps({'job_id': 'risk-production-review-test', 'status': 'pending', 'paid_ai_calls': 0})
        with patch.object(request.Path, 'read_text', return_value=json.dumps(value)), \
                patch.object(request.subprocess, 'run', return_value=SimpleNamespace(stdout=result)) as run:
            request.main()
        self.assertEqual(run.call_args.args[0], [request.sys.executable, 'risk_subscription.py', 'collect-review'])
        for edit in [{'self_test':True}, {'job_id':'another'}]:
            with patch.object(request.Path, 'read_text', return_value=json.dumps({**value, **edit})), \
                    patch.object(request.subprocess, 'run') as run, self.assertRaises(RuntimeError):
                request.main()
            run.assert_not_called()

    def test_completed_noop_does_not_request_ack_again(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'output'
            value = {'operation': 'recover', 'job_id': 'test', 'self_test': False,
                     'dry_run': False, 'request_id': 'recovery-test'}
            result = json.dumps({'job_id': 'test', 'status': 'completed', 'noop': True, 'paid_ai_calls': 0})
            with patch.object(request.Path, 'read_text', return_value=json.dumps(value)), \
                    patch.object(request.subprocess, 'run', return_value=SimpleNamespace(stdout=result)), \
                    patch.dict(os.environ, GITHUB_OUTPUT=str(path)):
                request.main()
            self.assertIn('needs_ack=false', path.read_text())


if __name__ == '__main__':
    unittest.main()
