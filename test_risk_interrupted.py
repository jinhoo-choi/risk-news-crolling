"""Offline regression of stale refs and isolation across real native deliveries."""
import copy
import json
import os
import smtplib
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import risk_subscription as bridge
from risk_delivery import Delivery
from risk_partial_close import source_version
from risk_queue import Queue, digest, now
import test_risk_subscription as fixtures


class ReadAfterWrite(unittest.TestCase):
    def test_old_main_is_retried_until_confirmed_write_is_visible(self):
        q = object.__new__(Queue)
        floor, old, latest = 'a'*40, 'b'*40, 'c'*40
        q.request = Mock(side_effect=[{'object': {'sha': old}}, {'status': 'behind'},
                                      {'object': {'sha': latest}}, {'status': 'ahead'}])
        q.get = Mock(return_value=({'delivery_revision': 'current'}, 'blob'))
        with patch('risk_queue.time.sleep') as sleep:
            self.assertEqual(q.get_after('jobs/test.json', floor)[0]['delivery_revision'], 'current')
        q.get.assert_called_once_with('jobs/test.json', ref=latest)
        sleep.assert_called_once_with(1)

    def test_unknown_diverged_or_persistently_old_ref_never_uses_write_snapshot(self):
        for status in ['diverged', None, 'behind']:
            q = object.__new__(Queue)
            q.request = Mock(side_effect=lambda path: {'object': {'sha': 'b'*40}}
                             if path.startswith('/git/ref/') else {'status': status})
            q.get = Mock()
            with patch('risk_queue.time.sleep'), self.assertRaises(RuntimeError):
                q.get_after('jobs/test.json', 'a'*40)
            q.get.assert_not_called()
            self.assertEqual(q.request.call_count, 6 if status == 'behind' else 2)

    def test_delivery_passes_the_confirmed_reservation_commit_to_live_read(self):
        q = Mock(spec=['get', 'get_fresh', 'get_after', 'put'])
        q.get_fresh.return_value = ({'status': 'sending'}, 'old')
        q.put.return_value = {'commit': {'sha': 'a'*40}}
        guard = Delivery(q, 'test', Mock(), Mock())
        job = guard.change(lambda j: j.update(receipts={'one': 'sending'}))
        changed = dict(job, delivery_revision='other-worker')
        q.get_after.return_value = (changed, 'new')
        self.assertEqual(guard.read()[0], changed)
        q.get_after.assert_called_once_with('jobs/test.json', 'a'*40)
        self.assertNotEqual(changed['delivery_revision'], guard.revision)


class InterruptedRounds(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    tearDown = fixtures.Integration.tearDown
    fixture = fixtures.Integration.fixture
    run_consume = fixtures.Integration.run_consume

    def interrupted(self):
        q = self.fixture()
        with self.assertRaisesRegex(RuntimeError, '접수 불확실'):
            self.run_consume(q, fixtures.SMTP(fail_at=1))
        q.data['jobs/test.json']['expires_at'] = (now()-timedelta(minutes=1)).isoformat()
        return q

    def isolate(self, q):
        versions = set()
        index, sha = bridge.expire_and_check(q, versions, isolate_interrupted=True)
        return index, sha, versions

    def test_expired_interruption_reserves_only_mailed_versions_without_closing_or_sending(self):
        q = self.interrupted()
        before = copy.deepcopy(q.data)
        with patch.object(smtplib, 'SMTP_SSL', side_effect=AssertionError('SMTP forbidden')):
            index, _, versions = self.isolate(q)
            self.isolate(q)  # Idempotent, original record is immutable.
        self.assertIn('test', index['interrupted_jobs'])
        for path, value in before.items():
            if path != 'index.json':
                self.assertEqual(q.data[path], value)
        self.assertEqual(versions, {source_version(b) for b in before['jobs/test.json']['body_manifest']})
        self.assertEqual(q.data['jobs/test.json']['status'], 'uncertain')
        self.assertFalse(q.data['jobs/test.json'].get('ack'))
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_live_interruption_and_explicit_recovery_stay_blocked(self):
        q = self.interrupted()
        q.data['jobs/test.json']['expires_at'] = (now()+timedelta(minutes=30)).isoformat()
        with self.assertRaisesRegex(RuntimeError, '미종결'):
            self.isolate(q)
        q.data['jobs/test.json']['expires_at'] = (now()-timedelta(minutes=1)).isoformat()
        self.isolate(q)
        with self.assertRaises(RuntimeError):
            bridge.recover(q, 'test')
        self.assertNotIn('ack', q.data['jobs/test.json'])

    def test_missing_packet_or_changed_receipt_does_not_unlock_collection(self):
        q = self.interrupted()
        packet = q.data.pop('outbox/test.json')
        with self.assertRaisesRegex(RuntimeError, '고정본 누락'):
            self.isolate(q)
        q.data['outbox/test.json'] = packet
        self.isolate(q)
        q.data['jobs/test.json']['receipts']['other'] = 'accepted'
        with self.assertRaisesRegex(RuntimeError, '증거 변경'):
            self.isolate(q)

    def test_tampered_isolation_or_fixed_packet_is_rejected(self):
        for path in ['interruptions/test.json', 'outbox/test.json', 'results/test.json']:
            q = self.interrupted()
            self.isolate(q)
            q.data[path]['tampered'] = True
            with self.assertRaises(RuntimeError):
                self.isolate(q)

    def test_staged_without_smtp_does_not_suppress_unmailed_articles(self):
        q = self.interrupted()
        j = q.data['jobs/test.json']
        j['receipts'] = {}; j['attempted_count'] = 0; j['status'] = 'sending'
        self.assertFalse(self.isolate(q)[2])

    def test_only_packet_articles_and_reviewed_event_members_are_reserved(self):
        q = self.interrupted()
        j = q.data['jobs/test.json']; r = q.data['results/test.json']
        item = dict(j['input']['items'][0], article_id='held', url='https://example.invalid/held')
        j['input']['items'].append(item)
        j['body_manifest'].append(dict(j['body_manifest'][0], article_id='held', source_url=item['url']))
        j['input_hash'] = digest(j['input']); j['body_manifest_hash'] = digest(j['body_manifest'])
        r['input_hash'] = j['input_hash']; r['body_manifest_hash'] = j['body_manifest_hash']
        packet = q.data['outbox/test.json']
        packet.update(input_hash=j['input_hash'], body_manifest_hash=j['body_manifest_hash'], result_hash=digest(r))
        packet.pop('packet_hash'); packet['packet_hash'] = digest(packet)
        versions = self.isolate(q)[2]
        self.assertIn(source_version(j['body_manifest'][0]), versions)
        self.assertNotIn(source_version(j['body_manifest'][1]), versions)
        changed = dict(j['body_manifest'][0], body=j['body_manifest'][0]['body']+' 새 처분이 확정됐다.')
        self.assertNotIn(source_version(changed), versions)

    def test_cas_failure_leaves_old_round_and_index_untouched(self):
        q = self.interrupted(); before = copy.deepcopy(q.data)
        with patch.object(q, 'put_many', side_effect=RuntimeError('CAS conflict')):
            with self.assertRaises(RuntimeError):
                self.isolate(q)
        self.assertEqual(q.data, before)

    def test_next_native_delivery_and_ack_succeed_while_old_receipts_remain_uncertain(self):
        q = self.interrupted(); old = copy.deepcopy(q.data['jobs/test.json'])
        self.isolate(q)
        new = self.fixture()
        job = new.data['jobs/test.json']; result = new.data['results/test.json']
        job['job_id'] = result['job_id'] = 'next'
        for a in job['input']['items']:
            a.update(article_id='new', url='https://example.invalid/new')
        job['body_manifest'][0].update(article_id='new', source_url='https://example.invalid/new')
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        result['classifications'][0]['article_id'] = 'new'
        result['analyses']['new'] = result['analyses'].pop('one')
        q.data['jobs/next.json'] = job; q.data['results/next.json'] = result
        q.data['index.json']['jobs'].append('next')
        smtp = fixtures.SMTP()
        with patch.object(smtplib, 'SMTP_SSL', lambda *a, **k: smtp):
            bridge.consume(q, 'next')
        q.remote_seen = Path('seen_news.json').read_bytes()
        bridge.acknowledge(q, 'next')
        self.assertEqual(q.data['jobs/next.json']['status'], 'completed')
        self.assertTrue(q.data['jobs/next.json']['ack'])
        self.assertEqual(len(smtp.calls), 3)
        self.assertEqual(q.data['jobs/test.json'], old)

    def self_only_interrupted(self):
        q = self.interrupted()
        j = q.data['jobs/test.json']; p = q.data['outbox/test.json']
        key = 'a'*64
        j['expected_receipts'] = [key]; j['receipts'] = {key: 'uncertain'}; j['attempted_count'] = 1
        p.update(self_only=True, expected_receipts=[key])
        p.pop('packet_hash'); p['packet_hash'] = digest(p)
        self.assertFalse(self.isolate(q)[2])  # Keep full-recipient eligibility.
        return q

    def next_same_article(self, q):
        new = self.fixture()
        job = new.data['jobs/test.json']; result = new.data['results/test.json']
        job['job_id'] = result['job_id'] = 'next'
        q.data['jobs/next.json'] = job; q.data['results/next.json'] = result
        q.data['index.json']['jobs'].append('next')

    def test_self_only_uncertainty_does_not_block_first_full_report(self):
        q = self.self_only_interrupted(); old = copy.deepcopy(q.data['jobs/test.json'])
        self.next_same_article(q)
        smtp = fixtures.SMTP()
        with patch.object(smtplib, 'SMTP_SSL', lambda *a, **k: smtp):
            bridge.consume(q, 'next')
        self.assertFalse(q.data['outbox/next.json']['self_only'])
        self.assertIn('https://example.invalid/test/one', q.data['outbox/next.json']['html'])
        self.assertEqual(len(smtp.calls), 3)
        self.assertEqual(q.data['jobs/test.json'], old)

    def test_self_only_reserved_card_is_not_remailed_or_marked_seen(self):
        q = self.self_only_interrupted(); old = copy.deepcopy(q.data['jobs/test.json'])
        self.next_same_article(q); self.bot.FORCE_SELF_ONLY = True
        smtp = fixtures.SMTP()
        with patch.object(smtplib, 'SMTP_SSL', lambda *a, **k: smtp):
            bridge.consume(q, 'next')
        self.assertNotIn('https://example.invalid/test/one', q.data['outbox/next.json']['html'])
        self.assertNotIn('https://example.invalid/test/one', Path('seen_news.json').read_text())
        self.assertEqual(q.data['jobs/test.json'], old)

    def test_native_smtp_waits_for_its_confirmed_write_without_repeating_data(self):
        q, smtp, to, cc = fixtures.Integration.executive_fixture(self)
        put, old_reads = q.put, []
        def confirmed_put(path, value, sha=None):
            put(path, value, sha)
            commit = digest(q.data)[:40]
            q.commits[commit] = copy.deepcopy(q.data)
            return {'commit': {'sha': commit}}
        def after(path, commit):
            reader = object.__new__(Queue)
            reader.get = q.get
            if smtp.rcpt.called and not old_reads:
                old_reads.append(True)
                reader.request = Mock(side_effect=[{'object': {'sha': '0'*40}},
                                                     {'status': 'behind'}, {'object': {'sha': commit}}])
            else:
                reader.request = Mock(return_value={'object': {'sha': commit}})
            return reader.get_after(path, commit)
        with patch.object(q, 'put', confirmed_put), patch.object(q, 'get_after', after, create=True), \
                patch('risk_queue.time.sleep'):
            self.run_consume(q, smtp)
        self.assertTrue(old_reads)
        smtp.data.assert_called_once()
        self.assertEqual(set(q.data['jobs/test.json']['receipts'].values()), {'accepted'})

    def test_existing_request_workflow_isolates_without_collect_consume_smtp_or_ack(self):
        import risk_subscription_request as request
        q = self.interrupted(); before = copy.deepcopy(q.data['jobs/test.json'])
        wake = {'operation': 'isolate-interrupted', 'job_id': 'test', 'self_test': False,
                'dry_run': False, 'request_id': 'isolation-maintenance'}
        Path('risk_subscription_request.json').write_text(json.dumps(wake))
        with patch('risk_queue.Queue', return_value=q), patch.object(q, 'preflight', create=True), \
                patch.object(request.subprocess, 'run') as process, \
                patch.object(smtplib, 'SMTP_SSL') as smtp, patch.dict(os.environ, GITHUB_OUTPUT='workflow-output'):
            with patch.dict(os.environ, GITHUB_REF='refs/heads/topic'), self.assertRaises(RuntimeError):
                request.main()
            request.main()
        smtp.assert_not_called(); process.assert_not_called()
        self.assertEqual(Path('workflow-output').read_text().splitlines(), ['job_id=test', 'needs_ack=false'])
        self.assertIn('test', q.data['index.json']['interrupted_jobs'])
        self.assertEqual(q.data['jobs/test.json'], before)


if __name__ == '__main__':
    unittest.main()
