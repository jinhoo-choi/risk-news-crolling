"""Real native delivery + queue conflicts; all SMTP/HTTP are local mocks."""
import copy
import contextlib
import io
import json
import os
import smtplib
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import risk_subscription as bridge
import risk_subscription_request as request
from risk_delivery import address_key
from risk_outbox import sha, store_packet
from risk_partial_close import close_partial, source_version
from risk_queue import Queue, QueueConflict, QueueReadUnavailable, digest, now
import risk_queue
import test_risk_subscription as fixtures


class Conflicts(unittest.TestCase):
    def test_parallel_readers_share_one_request_start_budget(self):
        clock, starts = [0.0], []
        def sleep(seconds):
            clock[0] += seconds
        def response(*args, **kwargs):
            starts.append(clock[0])
            value = Mock()
            value.__enter__ = Mock(return_value=io.StringIO('{}'))
            value.__exit__ = Mock(return_value=False)
            return value
        queues = [object.__new__(Queue) for _ in range(2)]
        for q in queues:
            q.token = 'synthetic-token'
        with patch.object(risk_queue, '_NEXT_REQUEST_AT', 0.0), \
                patch.object(risk_queue, '_RATE_LIMIT_ERROR', None), \
                patch.object(risk_queue.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(risk_queue.time, 'sleep', side_effect=sleep), \
                patch.object(risk_queue.urllib.request, 'urlopen', side_effect=response):
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda i: queues[i % 2].request('/contents/synthetic'), range(24)))
        self.assertEqual(len(starts), 24)
        self.assertTrue(all(b-a >= 0.149999 for a, b in zip(starts, starts[1:])))

    def test_rate_limits_stop_pending_readers_and_writes_without_private_output(self):
        cases = [(403, {'X-RateLimit-Remaining':'0', 'X-RateLimit-Reset':'2000000000'},
                  'API rate limit exceeded', 'primary_rate_limit'),
                 (403, {'Retry-After':'120'}, 'You have exceeded a secondary rate limit', 'secondary_rate_limit'),
                 (429, {}, 'Too many requests', 'secondary_rate_limit')]
        for code, headers, message, kind in cases:
            with self.subTest(code=code, kind=kind):
                q = object.__new__(Queue); q.token = 'synthetic-token'
                body = json.dumps({'message':message, 'private':'PRIVATE ARTICLE synthetic-token'}).encode()
                error = urllib.error.HTTPError('https://api.github.com', code, 'limited',
                    dict(headers, **{'X-GitHub-Request-Id':'ABCD:1234'}), io.BytesIO(body))
                output = io.StringIO()
                with patch.object(risk_queue, '_RATE_LIMIT_ERROR', None), \
                        patch.object(risk_queue, '_NEXT_REQUEST_AT', 0.0), \
                        patch.object(risk_queue.urllib.request, 'urlopen', side_effect=error) as call, \
                        contextlib.redirect_stdout(output):
                    for method in ('GET', 'GET', 'PUT'):
                        with self.assertRaisesRegex(RuntimeError, kind):
                            q.request('/contents/PRIVATE_PATH', method)
                    self.assertEqual(call.call_count, 1)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                self.assertTrue(risk_queue.HTTP_DIAGNOSTIC.fullmatch(output.getvalue().strip()))
                for private in ('PRIVATE', q.token):
                    self.assertNotIn(private, output.getvalue())

    def test_permission_and_unknown_403_are_not_retried_or_called_rate_limits(self):
        for body, kind in [(b'{"message":"Resource not accessible by personal access token"}', 'authorization'),
                           (b'PRIVATE malformed body', 'unclassified')]:
            q = object.__new__(Queue); q.token = 'synthetic-token'
            error = urllib.error.HTTPError('https://api.github.com', 403, 'forbidden',
                {'Retry-After':'PRIVATE', 'X-RateLimit-Remaining':'3\nPRIVATE',
                 'X-GitHub-Request-Id':'bad\nPRIVATE'}, io.BytesIO(body))
            output = io.StringIO()
            with patch.object(risk_queue, '_RATE_LIMIT_ERROR', None), \
                    patch.object(risk_queue.urllib.request, 'urlopen', side_effect=error) as call, \
                    contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, kind):
                q.request('')
            self.assertEqual(call.call_count, 1)
            self.assertNotIn('PRIVATE', output.getvalue())
            self.assertTrue(risk_queue.HTTP_DIAGNOSTIC.fullmatch(output.getvalue().strip()))

    def test_atomic_batch_checks_932_leases_in_four_tree_reads(self):
        q = object.__new__(Queue)
        expected = {'review/synthetic'+str(i)+'.json': 'old'+str(i) for i in range(932)}
        expected['review/missing.json'] = None
        trees = {'root':[{'path':'subscription','type':'tree','sha':'subscription'}],
                 'subscription':[{'path':'risk','type':'tree','sha':'risk'}],
                 'risk':[{'path':'review','type':'tree','sha':'review'}],
                 'review':[{'path':p.split('/')[-1], 'type':'blob', 'sha':sha}
                           for p, sha in expected.items() if sha is not None]}
        q.request = Mock(side_effect=lambda path: {'tree':trees[path.split('/')[-1]], 'truncated':False})
        # Four trees, independent of record count; the fifth read is the commit
        # used by put_many to obtain this root (see ref-race regression below).
        self.assertEqual(q.file_shas(expected, 'root'), expected)
        self.assertEqual(q.request.call_count, 4)
        q.request = Mock(return_value={'tree':[], 'truncated':True})
        with self.assertRaisesRegex(RuntimeError, '불완전'):
            q.file_shas(expected, 'root')

    def test_conflict_diagnostics_distinguish_file_state_without_private_values(self):
        for state, fresh in [('same_file', ({'private': 'OLD'}, 'old')),
                             ('changed_file', ({'private': 'CHANGED'}, 'new')),
                             ('confirmed_existing', ({'private': 'NEW'}, 'new')),
                             ('read_failed', RuntimeError('PRIVATE READ ERROR'))]:
            with self.subTest(state=state):
                q = object.__new__(Queue)
                q.request = Mock(side_effect=QueueConflict('ABCD:1234', 'sha_conflict'))
                q.get_fresh = Mock(side_effect=fresh if isinstance(fresh, Exception) else None,
                                   return_value=fresh)
                output = io.StringIO()
                with contextlib.redirect_stdout(output), patch('risk_queue.time.sleep'):
                    try:
                        q.put('jobs/PRIVATE_JOB.json', {'private': 'NEW'}, 'old')
                    except RuntimeError:
                        self.assertNotEqual(state, 'confirmed_existing')
                lines = output.getvalue().splitlines()
                self.assertEqual(len(lines), 3 if state == 'same_file' else 1)
                for n, line in enumerate(lines, 1):
                    self.assertEqual(line, f'RISK_QUEUE_CONFLICT operation=put category=job attempt={n} '
                                     f'outcome={state} kind=sha_conflict http=409 request_id=ABCD:1234')

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
            if path.startswith('/git/trees/') and method == 'GET':
                tree = path.split('/')[-1]
                if tree.endswith('-tree'): entries = [{'path':'subscription','type':'tree','sha':'subscription'}]
                elif tree == 'subscription': entries = [{'path':'risk','type':'tree','sha':'risk'}]
                elif tree == 'risk': entries = [{'path':'jobs','type':'tree','sha':'jobs'}]
                else: entries = [{'path':'test.json','type':'blob','sha':'unchanged'}]
                return {'tree':entries,'truncated':False}
            if path == '/git/trees': bases.append(data['base_tree']); return {'sha': 'new-tree'}
            if path == '/git/commits': return {'sha': head[0]+'-child'}
            if method == 'PATCH':
                refs.append(data)
                if len(refs) == 1:
                    head[0] = 'other-bot'
                    raise QueueConflict(method='PATCH', code=422)
                return {}
            raise AssertionError(path)
        q.request = Mock(side_effect=api); q.get = Mock(side_effect=AssertionError('per-file body GET forbidden'))
        with patch('risk_queue.time.sleep'):
            q.put_many({'jobs/test.json': {'ack': True}}, {'jobs/test.json': 'unchanged'})
        self.assertEqual(bases, ['one-tree', 'other-bot-tree'])
        self.assertEqual(len(refs), 2)
        self.assertTrue(all(r['force'] is False for r in refs))
        q.get.assert_not_called()
        roots = [call.args[0] for call in q.request.call_args_list
                 if call.args[0] in ('/git/trees/one-tree','/git/trees/other-bot-tree')]
        self.assertEqual(roots, ['/git/trees/one-tree','/git/trees/other-bot-tree'])

    def test_atomic_batch_changed_lease_blocks_before_any_write(self):
        q = object.__new__(Queue)
        q.request = Mock(side_effect=[{'object':{'sha':'fixed'}}, {'tree':{'sha':'root'}},
            {'tree':[{'path':'subscription','type':'tree','sha':'subscription'}]},
            {'tree':[{'path':'risk','type':'tree','sha':'risk'}]},
            {'tree':[{'path':'review','type':'tree','sha':'review'}]},
            {'tree':[{'path':'one.json','type':'blob','sha':'changed'}]}])
        with self.assertRaisesRegex(RuntimeError, '원본 SHA 변경'):
            q.put_many({'review/one.json':{'new':True}}, {'review/one.json':'original'})
        self.assertTrue(all(len(call.args) == 1 for call in q.request.call_args_list))


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


class ReportRouting(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    tearDown = fixtures.Integration.tearDown
    fixture = fixtures.Integration.fixture
    run_consume = fixtures.Integration.run_consume

    def test_groups_and_explicit_executive_keep_envelopes_without_implicit_sender(self):
        from email import message_from_string
        from email.utils import parseaddr
        from risk_delivery import address_key
        self.bot.EMAIL_RECEIVERS = ['general@example.invalid', 'pb@example.invalid']
        self.bot.EMAIL_CC = ['executive@example.invalid', 'general@example.invalid']
        q, smtp = self.fixture(), fixtures.SMTP()
        self.run_consume(q, smtp)
        targets = ['general@example.invalid', 'pb@example.invalid', 'executive@example.invalid']
        self.assertEqual([c[0] for c in smtp.calls], [[a] for a in targets])
        for recipients, raw in smtp.calls:
            msg = message_from_string(raw)
            self.assertEqual(parseaddr(msg['To'])[1], recipients[0])
            self.assertIsNone(msg['Cc'])
            self.assertIsNone(msg['Bcc'])
        job = q.data['jobs/test.json']
        self.assertEqual(job['expected_receipts'], [address_key(a) for a in targets])
        self.assertNotIn(address_key(self.bot.EMAIL_SENDER), job['receipts'])
        q.remote_seen = Path('seen_news.json').read_bytes()
        bridge.acknowledge(q, 'test')
        completed = copy.deepcopy(q.data['jobs/test.json'])
        self.run_consume(q, smtp)
        self.assertEqual(len(smtp.calls), 3)
        self.assertEqual(q.data['jobs/test.json'], completed)
        self.assertTrue(completed['ack'])

    def test_explicit_sender_is_preserved_and_empty_config_cannot_fall_back(self):
        self.bot.EMAIL_RECEIVERS = [self.bot.EMAIL_SENDER, 'general@example.invalid']
        self.bot.EMAIL_CC = [self.bot.EMAIL_SENDER]
        smtp = fixtures.SMTP()
        with patch.object(smtplib, 'SMTP_SSL', lambda *a, **k: smtp):
            self.bot.send_email('report', '<p>report</p>')
        self.assertEqual([c[0] for c in smtp.calls], [[self.bot.EMAIL_SENDER], ['general@example.invalid']])
        self.bot.EMAIL_RECEIVERS, self.bot.EMAIL_CC = [], []
        with patch.object(smtplib, 'SMTP_SSL', side_effect=AssertionError('SMTP forbidden')):
            with self.assertRaisesRegex(RuntimeError, '수신자 미설정'):
                self.bot.send_email('report', '<p>report</p>')

    def test_existing_all_accepted_sender_copy_packet_finishes_without_smtp_or_rewriting(self):
        from risk_queue import digest
        q, smtp = self.fixture(), fixtures.SMTP()
        self.run_consume(q, smtp)  # All three explicitly configured targets accepted.
        packet = q.data['outbox/test.json']
        packet['recipient_route'] = 'configured'  # Pre-change frozen plan.
        packet['packet_hash'] = digest({k: v for k, v in packet.items() if k != 'packet_hash'})
        q.data['jobs/test.json']['outbox_hash'] = packet['packet_hash']
        q.data['jobs/test.json']['created_at'] = now().isoformat()
        frozen = copy.deepcopy(packet)
        receipts = copy.deepcopy(q.data['jobs/test.json']['receipts'])
        self.bot.EMAIL_RECEIVERS = ['group@example.invalid']  # Sender only came from old implicit copy.
        Path('seen_news.json').write_bytes(self.before)
        with patch.object(smtplib, 'SMTP_SSL', side_effect=AssertionError('accepted must not resend')):
            bridge.recover(q, 'test')
        self.assertEqual(q.data['outbox/test.json'], frozen)
        self.assertEqual(q.data['jobs/test.json']['receipts'], receipts)
        q.remote_seen = Path('seen_news.json').read_bytes()
        bridge.acknowledge(q, 'test')
        self.assertTrue(q.data['jobs/test.json']['ack'])

    def test_group_rejection_keeps_history_and_never_falls_back_to_sender(self):
        self.bot.EMAIL_RECEIVERS, self.bot.EMAIL_CC = ['general@example.invalid'], []
        q, smtp = self.fixture(), fixtures.SMTP()
        def refused(sender, recipients, raw):
            smtp.calls.append((recipients, raw))
            return {recipients[0]: (550, b'refused')}
        smtp.sendmail = refused
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        self.assertEqual([c[0] for c in smtp.calls], [['general@example.invalid']])
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        self.assertFalse(q.data['jobs/test.json'].get('ack', False))


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

    def writeoff_fixture(self):
        q = fixtures.Integration.grouped_fixture(self)
        q.data['jobs/test.json']['paid_ai_calls'] = 0
        with self.assertRaises(RuntimeError):
            self.run_consume(q, fixtures.SMTP(fail_at=2))
        j, job_sha = q.get('jobs/test.json')
        packet, out_sha = q.get('outbox/test.json')
        q.data['partial-close-requests/test.json'] = {
            'operation':'close_without_resend', 'job_id':'test', 'request_id':'approved',
            'job_sha':job_sha, 'outbox_sha':out_sha, 'result_sha':q.get('results/test.json')[1],
            'sent_html_hash':sha(packet['html']), 'reported_article_ids':['two'],
            'accepted_sent_messages':{k:'abc012345678' for k,v in j['receipts'].items() if v == 'accepted'}}
        return q

    def test_partial_close_preserves_receipts_and_releases_only_unchanged_versions(self):
        q = self.writeoff_fixture()
        old = q.get('jobs/test.json')[0]
        before = copy.deepcopy(q.data)
        with patch.object(smtplib, 'SMTP_SSL', side_effect=AssertionError('SMTP forbidden')):
            close_partial(q, 'test', 'approved')
            close_partial(q, 'test', 'approved')
        job = q.get('jobs/test.json')[0]
        self.assertEqual(job['receipts'], old['receipts'])
        self.assertEqual(job['attempted_count'], 2)
        self.assertEqual(job['status'], 'closed_partial')
        self.assertFalse(job.get('ack') or job.get('history_saved'))
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        self.assertEqual({p for p in q.data if q.data[p] != before[p]}, {'index.json','jobs/test.json'})
        versions = set()
        bridge.expire_and_check(q, versions)
        self.assertEqual(job['partial_closure']['reported_ids'], ['two'])
        self.assertEqual(versions, {source_version(b) for b in old['body_manifest']})
        revised = dict(old['body_manifest'][0], body='같은 매체가 새로운 처분 내용을 추가했다.')
        self.assertNotIn(source_version(revised), versions)
        # A rolled scheduling index still carries the original closure evidence.
        q.data['index.json']['jobs'] = []
        bridge.expire_and_check(q)
        q.data['jobs/test.json']['receipts'].clear()
        with self.assertRaisesRegex(RuntimeError,'종결 증거'):
            bridge.expire_and_check(q)
        with self.assertRaises(RuntimeError):
            bridge.acknowledge(q,'test')
        with self.assertRaises(RuntimeError):
            self.resume(q,fixtures.SMTP())

    def test_partial_close_rejects_stale_approval_wrong_sent_copy_and_unmailed_article(self):
        for field, value in [('job_sha','old'), ('sent_html_hash','wrong'),
                             ('reported_article_ids',['one','two']), ('accepted_sent_messages',{})]:
            with self.subTest(field=field):
                q = self.writeoff_fixture()
                q.data['partial-close-requests/test.json'][field] = value
                with self.assertRaises(RuntimeError):
                    close_partial(q,'test','approved')
                self.assertEqual(q.data['jobs/test.json']['status'],'uncertain')
                self.assertNotIn('partial_closures', q.data['index.json'])

    def test_public_close_request_never_enters_consume_and_skips_history_ack(self):
        import risk_subscription_request as request
        q = self.writeoff_fixture()
        wake = {'operation': 'close-partial', 'job_id': 'test', 'self_test': False,
                'dry_run': False, 'request_id': 'approved'}
        Path('risk_subscription_request.json').write_text(json.dumps(wake))
        with patch('risk_queue.Queue', return_value=q), patch.object(q, 'preflight', create=True), \
                patch.object(request.subprocess, 'run') as process, \
                patch.object(smtplib, 'SMTP_SSL') as smtp, patch.dict(os.environ, GITHUB_OUTPUT='workflow-output'):
            with patch.dict(os.environ, GITHUB_REF='refs/heads/topic'), self.assertRaises(RuntimeError):
                request.main()
            with patch.dict(os.environ, GITHUB_REF='refs/heads/main'):
                request.main()
        process.assert_not_called()
        smtp.assert_not_called()
        self.assertEqual(Path('workflow-output').read_text().splitlines(), ['job_id=test', 'needs_ack=false'])
        self.assertEqual(q.get('jobs/test.json')[0]['status'], 'closed_partial')

    def test_partial_close_conflicting_receipt_lease_keeps_old_lock(self):
        q = self.writeoff_fixture()
        put = q.put_many
        def race(values, expected):
            q.data['jobs/test.json']['delivery_revision'] = 'other-worker'
            return put(values, expected)
        with patch.object(q,'put_many',race), self.assertRaises(RuntimeError):
            close_partial(q,'test','approved')
        with self.assertRaisesRegex(RuntimeError,'미종결'):
            bridge.expire_and_check(q)

    def test_all_accepted_finish_and_cleanup_conflicts_keep_original_then_recover_smtp_zero(self):
        q, smtp = self.fixture(), fixtures.SMTP()
        put, output = q.put, io.StringIO()
        first, cleanup = QueueConflict('FIRST'), QueueConflict('CLEANUP')
        def conflict(path, value, old=None):
            if path == 'jobs/test.json':
                if value.get('status') == 'delivery_complete':
                    raise first
                if value.get('status') == 'uncertain':
                    raise cleanup
            return put(path, value, old)
        with patch.object(q, 'put', conflict), contextlib.redirect_stdout(output), \
                self.assertRaises(QueueConflict) as raised:
            self.run_consume(q, smtp)
        self.assertIs(raised.exception, first)
        self.assertIn('RISK_DELIVERY_CLEANUP outcome=failed', output.getvalue())
        self.assertEqual(len(smtp.calls), 3)
        self.assertEqual(set(q.data['jobs/test.json']['receipts'].values()), {'accepted'})
        Path('seen_news.json').write_bytes(self.before)
        fresh = fixtures.SMTP()
        self.resume(q, fresh)
        self.assertEqual(fresh.calls, [])
        q.remote_seen = Path('seen_news.json').read_bytes()
        bridge.acknowledge(q, 'test')
        self.assertTrue(q.data['jobs/test.json']['ack'])
        self.resume(q, fresh)
        self.assertEqual(fresh.calls, [])

    def test_recovery_cleanup_conflict_preserves_original_with_zero_smtp(self):
        q, smtp = self.fixture(), fixtures.SMTP()
        self.run_consume(q, smtp)
        Path('seen_news.json').write_bytes(self.before)
        put, first = q.put, QueueConflict('FIRST')
        def conflict(path, value, old=None):
            if path == 'jobs/test.json':
                if value.get('status') == 'delivery_complete':
                    raise first
                if value.get('status') == 'uncertain':
                    raise QueueConflict('CLEANUP')
            return put(path, value, old)
        fresh = fixtures.SMTP()
        with patch.object(q, 'put', conflict), self.assertRaises(QueueConflict) as raised:
            self.resume(q, fresh)
        self.assertIs(raised.exception, first)
        self.assertEqual(fresh.calls, [])
        self.assertFalse(q.data['jobs/test.json'].get('ack', False))

    def test_owner_status_recovery_restores_only_signed_owner_route(self):
        from risk_continuity import freeze_exposure
        from risk_owner_verification import ERROR_EMAIL
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
        self.assertEqual([c[0] for c in smtp.calls], [[ERROR_EMAIL]])
        self.assertEqual(q.data['outbox/test.json'], frozen)
        self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_complete')

    def test_old_company_status_packet_is_rejected_without_resending(self):
        from risk_continuity import freeze_exposure
        from risk_delivery import address_key
        from risk_owner_verification import OWNER
        from risk_queue import digest
        q, smtp = self.fixture(), fixtures.SMTP()
        job = q.data['jobs/test.json']
        q.data.update(freeze_exposure(job, Path('exposure_data.csv').read_bytes(), now().isoformat()))
        q.data['results/test.json']['classifications'][0]['relevant'] = None
        q.data['results/test.json']['analyses'] = {}
        with patch.object(bridge, 'finish_packet', side_effect=RuntimeError('offline before SMTP')), \
             patch.object(bridge, 'notify_best_effort'), self.assertRaisesRegex(RuntimeError, 'offline'):
            self.run_consume(q, smtp)
        packet = q.data['outbox/test.json']
        packet['expected_receipts'] = [address_key(OWNER)]
        packet['packet_hash'] = digest({k: v for k, v in packet.items() if k != 'packet_hash'})
        before_packet = copy.deepcopy(packet)
        before_receipts = copy.deepcopy(q.data['jobs/test.json'].get('receipts', {}))
        with self.assertRaisesRegex(RuntimeError, '담당자 상태 고정본 수신자 오류'):
            self.resume(q, smtp)
        self.assertEqual(smtp.calls, [])
        self.assertEqual(q.data['outbox/test.json'], before_packet)
        self.assertEqual(q.data['jobs/test.json'].get('receipts', {}), before_receipts)

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

    def test_old_implicit_sender_partial_plan_recovers_only_unattempted_target(self):
        from risk_queue import digest
        q, original = self.partial()
        packet = q.data['outbox/test.json']
        packet['recipient_route'] = 'configured'
        packet['packet_hash'] = digest({k: v for k, v in packet.items() if k != 'packet_hash'})
        frozen = copy.deepcopy(packet)
        self.bot.EMAIL_RECEIVERS = ['group@example.invalid']
        fresh = fixtures.SMTP()
        self.resume(q, fresh)
        self.assertEqual([c[0] for c in fresh.calls], [['cc@example.invalid']])
        self.assertEqual(q.data['outbox/test.json'], frozen)
        self.assertEqual(len(q.data['jobs/test.json']['receipts']), 3)

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
    def test_failure_forwards_only_safe_403_diagnostics(self):
        value = {'operation':'consume', 'job_id':'test', 'self_test':False,
                 'dry_run':False, 'request_id':'http-test'}
        safe = ('RISK_QUEUE_HTTP method=GET http=403 kind=secondary_rate_limit '
                'remaining=4700 reset=2000000000 retry_after=60 request_id=ABCD:1234')
        error = request.subprocess.CalledProcessError(1, ['child'], output='\n'.join([
            safe, safe+' PRIVATE', safe.replace('remaining=4700','remaining=PRIVATE'),
            safe.replace('kind=secondary_rate_limit','kind=PRIVATE'), 'PRIVATE TRACE']),
            stderr='PRIVATE RESPONSE')
        output = io.StringIO()
        with patch.object(request.Path, 'read_text', return_value=json.dumps(value)), \
                patch.object(request.subprocess, 'run', side_effect=error), \
                contextlib.redirect_stdout(output), self.assertRaises(request.subprocess.CalledProcessError):
            request.main()
        self.assertEqual(output.getvalue(), safe+'\n')

    def test_failure_forwards_only_fixed_queue_and_cleanup_diagnostics(self):
        value = {'operation':'consume', 'job_id':'test', 'self_test':False,
                 'dry_run':False, 'request_id':'conflict-test'}
        safe = ('RISK_QUEUE_CONFLICT operation=put category=job attempt=3 '
                'outcome=changed_file kind=conflict http=409 request_id=ABCD:1234')
        cleanup = 'RISK_DELIVERY_CLEANUP outcome=failed'
        lines = [safe, cleanup, safe+' PRIVATE', safe.replace('category=job', 'category=PRIVATE'),
                 safe.replace('attempt=3', 'attempt=4'), 'PRIVATE TRACEBACK']
        error = request.subprocess.CalledProcessError(1, ['child'], output='\n'.join(lines),
                                                     stderr='PRIVATE RESPONSE')
        output = io.StringIO()
        with patch.object(request.Path, 'read_text', return_value=json.dumps(value)), \
                patch.object(request.subprocess, 'run', side_effect=error), \
                contextlib.redirect_stdout(output), self.assertRaises(request.subprocess.CalledProcessError):
            request.main()
        self.assertEqual(output.getvalue(), safe+'\n'+cleanup+'\n')

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


class ExplicitRejections(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    tearDown = fixtures.Integration.tearDown
    run_consume = fixtures.Integration.run_consume
    fixture = fixtures.Integration.fixture

    def grouped(self):
        q, smtp, to, cc = fixtures.Integration.executive_fixture(self)
        q.data['jobs/test.json'].update(created_at=now().isoformat(),
            expires_at=(now()+bridge.timedelta(hours=2)).isoformat(), paid_ai_calls=0)
        return q, smtp, to, cc

    def retry(self, q, smtp):
        import risk_delivery
        later = now()+bridge.timedelta(minutes=31)
        with patch.object(risk_delivery, 'now', return_value=later), patch.object(bridge, 'now', return_value=later), \
                patch.object(smtplib, 'SMTP_SSL', lambda *a, **k: smtp):
            bridge.recover(q, 'test')

    def test_mail_421_reconnects_once_and_records_both_attempts(self):
        q, smtp, to, cc = self.grouped()
        smtp.mail.side_effect = [(421, b'temporary'), (250, b'OK')]
        self.run_consume(q, smtp)
        self.assertEqual(smtp.login.call_count, 2)
        smtp.data.assert_called_once()
        self.assertEqual(len(q.data['jobs/test.json']['smtp_evidence']['attempts']), 2)
        self.assertEqual(set(q.data['jobs/test.json']['receipts'].values()), {'accepted'})

    def test_data_451_waits_then_recovers_same_packet_and_ack(self):
        q, smtp, to, cc = self.grouped()
        smtp.data.side_effect = [(451, b'temporary'), (250, b'OK')]
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        packet = copy.deepcopy(q.data['outbox/test.json'])
        with self.assertRaisesRegex(RuntimeError, '대기'):
            bridge.recover(q, 'test')
        self.assertEqual(smtp.data.call_count, 1)
        self.retry(q, smtp)
        self.assertEqual(smtp.data.call_count, 2)
        self.assertEqual(q.data['outbox/test.json'], packet)
        q.remote_seen = Path('seen_news.json').read_bytes()
        bridge.acknowledge(q, 'test')
        self.assertTrue(q.data['jobs/test.json']['ack'])
        self.retry(q, smtp)
        self.assertEqual(smtp.data.call_count, 2)

    def test_partial_451_retries_only_rejected_recipient(self):
        q, smtp, to, cc = self.grouped()
        smtp.rcpt.side_effect = [(451 if a == to[1] else 250, b'answer') for a in to+cc]+[(250, b'OK')]
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        self.assertEqual(list(q.data['jobs/test.json']['receipts'].values()).count('accepted'), 4)
        self.retry(q, smtp)
        self.assertEqual([c.args[0] for c in smtp.rcpt.call_args_list], to+cc+[to[1]])
        self.assertEqual(smtp.data.call_count, 2)
        self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_complete')

    def test_all_550_closes_failure_and_carries_every_input_without_ack(self):
        q, smtp, _, _ = self.grouped()
        smtp.data.return_value = (550, b'permanent')
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        j = q.data['jobs/test.json']
        self.assertEqual(j['status'], 'delivery_failed')
        self.assertEqual(j['delivery_failure']['carried_ids'], ['one'])
        self.assertEqual(q.data['review/index.json']['pending'], ['one'])
        self.assertEqual(q.data['review/one.json']['item'], j['input']['items'][0])
        self.assertFalse(j.get('ack') or j.get('history_saved'))
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        bridge.expire_and_check(q)
        with self.assertRaises(RuntimeError):
            bridge.acknowledge(q, 'test')
        smtp.data.assert_called_once()

    def test_two_pre_data_rejections_exhaust_budget_across_invocations(self):
        q, smtp, _, _ = self.grouped()
        smtp.mail.return_value = (421, b'temporary')
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        self.assertEqual(smtp.login.call_count, 2)
        smtp.data.assert_not_called()
        self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_failed')
        with self.assertRaises(RuntimeError):
            self.retry(q, smtp)
        self.assertEqual(smtp.login.call_count, 2)

    def test_two_data_451s_close_failure_without_an_unbounded_third_attempt(self):
        q, smtp, _, _ = self.grouped()
        smtp.data.return_value = (451, b'temporary')
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        with self.assertRaises(RuntimeError):
            self.retry(q, smtp)
        self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_failed')
        with self.assertRaises(RuntimeError):
            self.retry(q, smtp)
        self.assertEqual(smtp.data.call_count, 2)
        bridge.expire_and_check(q)

    def test_partial_permanent_refusal_cannot_release_collection_or_retry_accepted(self):
        q, smtp, to, cc = self.grouped()
        smtp.rcpt.side_effect = [(550 if a == to[1] else 250, b'answer') for a in to+cc]
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_rejected')
        with self.assertRaises(RuntimeError):
            self.retry(q, smtp)
        with self.assertRaises(RuntimeError):
            bridge.expire_and_check(q)
        smtp.data.assert_called_once()

    def test_grouped_partial_refusal_can_use_explicit_no_resend_closure(self):
        q, smtp, to, cc = self.grouped()
        smtp.rcpt.side_effect = [(550 if a == to[1] else 250, b'answer') for a in to+cc]
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        j, job_sha = q.get('jobs/test.json')
        packet, packet_sha = q.get('outbox/test.json')
        q.data['partial-close-requests/test.json'] = {
            'operation':'close_without_resend','job_id':'test','request_id':'approved',
            'job_sha':job_sha,'outbox_sha':packet_sha,'result_sha':q.get('results/test.json')[1],
            'sent_html_hash':sha(packet['html']),'reported_article_ids':['one'],
            'accepted_sent_messages':{k:'abc012345678' for k,v in j['receipts'].items() if v == 'accepted'}}
        with patch.object(smtplib, 'SMTP_SSL', side_effect=AssertionError('resend forbidden')):
            close_partial(q, 'test', 'approved')
        closed = q.data['jobs/test.json']
        self.assertEqual(closed['status'], 'closed_partial')
        self.assertEqual(closed['receipts'], j['receipts'])
        self.assertFalse(closed.get('ack') or closed.get('history_saved'))
        bridge.expire_and_check(q)
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)
        smtp.data.assert_called_once()

    def test_data_reply_loss_never_acquires_rejection_recovery(self):
        q, smtp, _, _ = self.grouped()
        smtp.data.side_effect = TimeoutError('reply lost')
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        self.assertEqual(q.data['jobs/test.json']['status'], 'uncertain')
        with self.assertRaises(RuntimeError):
            self.retry(q, smtp)
        with self.assertRaises(RuntimeError):
            bridge.expire_and_check(q)
        smtp.data.assert_called_once()

    def test_receipt_write_failure_cannot_be_inferred_from_smtp_return(self):
        q, smtp, _, _ = self.grouped()
        put = q.put
        def fail_receipt(path, value, sha=None):
            if path == 'jobs/test.json' and 'accepted' in value.get('receipts', {}).values():
                raise QueueConflict()
            put(path, value, sha)
        with patch.object(q, 'put', fail_receipt), self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        with self.assertRaises(RuntimeError):
            self.retry(q, smtp)
        smtp.data.assert_called_once()
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_missing_proof_and_modified_packet_never_recover(self):
        for mutation in ('missing', 'packet'):
            q, smtp, _, _ = self.grouped()
            smtp.data.return_value = (451, b'temporary')
            with self.assertRaises(RuntimeError):
                self.run_consume(q, smtp)
            if mutation == 'missing':
                q.data['jobs/test.json'].pop('smtp_evidence')
            else:
                q.data['jobs/test.json']['smtp_evidence']['packet_hash'] = '0'*64
            with self.assertRaises(RuntimeError):
                self.retry(q, smtp)
            smtp.data.assert_called_once()

    def test_failed_carry_commit_keeps_collection_blocked(self):
        q, smtp, _, _ = self.grouped()
        smtp.data.return_value = (550, b'permanent')
        put_many = q.put_many
        def conflict(values, expected, **kwargs):
            if values.get('jobs/test.json', {}).get('status') == 'delivery_failed':
                raise QueueConflict()
            return put_many(values, expected, **kwargs)
        with patch.object(q, 'put_many', conflict):
            with self.assertRaises(RuntimeError):
                self.run_consume(q, smtp)
            self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_rejected')
            with self.assertRaises(RuntimeError):
                bridge.expire_and_check(q)
            self.assertNotIn('review/one.json', q.data)
        bridge.expire_and_check(q)
        self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_failed')
        smtp.data.assert_called_once()

    def test_expired_rejection_carries_without_retry(self):
        q, smtp, _, _ = self.grouped()
        smtp.data.return_value = (451, b'temporary')
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        q.data['jobs/test.json']['expires_at'] = (now()-bridge.timedelta(seconds=1)).isoformat()
        with self.assertRaises(RuntimeError):
            self.retry(q, smtp)
        self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_failed')
        smtp.data.assert_called_once()

    def test_new_owner_only_route_uses_rejection_evidence_without_company_recipients(self):
        q, smtp, _, _ = self.grouped()
        q.data['jobs/test.json']['smtp_evidence_version'] = 1
        r = q.data['results/test.json']
        r['analyses'] = {}
        r['classifications'][0].update(relevant=False, reason='검수용 제외 사례')
        smtp.data.side_effect = [(451, b'temporary'), (250, b'OK')]
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        packet = q.data['outbox/test.json']
        self.assertTrue(packet['self_only'])
        self.assertEqual(len(packet['expected_receipts']), 1)
        self.retry(q, smtp)
        self.assertEqual(len(smtp.rcpt.call_args_list), 2)
        self.assertEqual(smtp.rcpt.call_args_list[0], smtp.rcpt.call_args_list[1])
        self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_complete')

    def test_unknown_data_reply_cannot_be_promoted_to_definite_rejection(self):
        q, smtp, _, _ = self.grouped()
        smtp.data.return_value = (354, b'invalid final reply')
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        self.assertEqual(q.data['jobs/test.json']['status'], 'uncertain')
        with self.assertRaises(RuntimeError):
            self.retry(q, smtp)
        smtp.data.assert_called_once()

    def test_receipt_is_durable_before_quit_or_diagnostics(self):
        q, smtp, _, _ = self.grouped()
        observed = []
        def quit_after_reply():
            observed.append(copy.deepcopy(q.data['jobs/test.json']))
            raise smtplib.SMTPServerDisconnected('QUIT failed after acceptance')
        smtp.quit.side_effect = quit_after_reply
        self.run_consume(q, smtp)
        self.assertEqual(len(observed), 1)
        self.assertEqual(set(observed[0]['receipts'].values()), {'accepted'})
        self.assertTrue(observed[0]['smtp_evidence']['attempts'][-1]['finished_at'])
        self.assertEqual(q.data['jobs/test.json']['status'], 'delivery_complete')
        smtp.data.assert_called_once()

    def test_connection_greeting_421_has_same_one_retry_budget(self):
        q, smtp, _, _ = self.grouped()
        with patch.object(smtplib, 'SMTP_SSL', side_effect=[smtplib.SMTPConnectError(421, b'busy'), smtp]):
            bridge.consume(q, 'test')
        self.assertEqual(len(q.data['jobs/test.json']['smtp_evidence']['attempts']), 2)
        smtp.data.assert_called_once()

    def test_concurrent_change_before_data_sends_nothing_and_blocks_recovery(self):
        q, smtp, _, _ = self.grouped()
        def recipient(address):
            q.data['jobs/test.json']['delivery_revision'] = 'other-worker'
            return 250, b'OK'
        smtp.rcpt.side_effect = recipient
        with self.assertRaisesRegex(RuntimeError, '소유권'):
            self.run_consume(q, smtp)
        with self.assertRaises(RuntimeError):
            self.retry(q, smtp)
        smtp.data.assert_not_called()

    def test_all_current_contracts_keep_held_ids_after_recovery_and_ack(self):
        from risk_continuity import freeze_exposure
        from risk_shards import split_job
        q, smtp, _, _ = self.grouped()
        j, r = q.data['jobs/test.json'], q.data['results/test.json']
        j.update(review_pass_version=1, body_quality_version=1, event_grouping_version=1,
                 action_quality_version=1, exclusion_review_version=1, smtp_evidence_version=1)
        plan = {'event_anchor':'잔고 조회', 'current_task':'잔고 조회 장애 접수 고객과 주문 처리에 영향이 있는 고객을 구분',
                'check_source':'장애 공지', 'followup_condition':'복구 완료 공지가 나오면',
                'followup_task':'잔고 조회 재시도 결과와 미해결 고객을 담당 부서에 인계'}
        r['analyses']['one'].update(action_plan=plan, action=' → '.join(plan.values()))
        r['event_groups'] = []
        held = dict(j['input']['items'][0], article_id='held', url='https://example.invalid/held')
        j['input']['items'].append(held)
        j['input']['raw_articles'].append(copy.deepcopy(held))
        j['body_manifest'].append({'article_id':'held','source_url':held['url'],'body_status':'missing','body':''})
        r['classifications'].append({'article_id':'held','relevant':None,'reason':'실제 사건 본문 미확보로 보류'})
        j['input_hash'] = r['input_hash'] = digest(j['input'])
        j['body_manifest_hash'] = r['body_manifest_hash'] = digest(j['body_manifest'])
        q.data.update(freeze_exposure(j, Path('exposure_data.csv').read_bytes(), now().isoformat()))
        small, shards = split_job(j)
        q.data.update(shards)
        q.data['jobs/test.json'] = small
        original = copy.deepcopy(r)
        smtp.data.side_effect = [(451, b'temporary'), (250, b'OK')]
        with self.assertRaises(RuntimeError):
            self.run_consume(q, smtp)
        self.retry(q, smtp)
        q.remote_seen = Path('seen_news.json').read_bytes()
        bridge.acknowledge(q, 'test')
        self.assertEqual(q.data['jobs/test.json']['status'], 'completed')
        self.assertFalse(q.data['jobs/test.json']['analysis_complete'])
        self.assertEqual(q.data['review/index.json']['pending'], ['held'])
        self.assertNotIn(held['url'], q.remote_seen.decode())
        self.assertEqual(q.data['results/test.json'], original)
        self.assertEqual(smtp.data.call_count, 2)


if __name__ == '__main__':
    unittest.main()
