"""Expiration/collection/retention regressions: synthetic data, no API or SMTP."""
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import risk_subscription as bridge
from risk_queue import Queue, now, digest


class Memory:
    def __init__(self, jobs=()):
        self.data = {'jobs/'+j['job_id']+'.json': copy.deepcopy(j) for j in jobs}
        self.data['index.json'] = {'jobs': [j['job_id'] for j in jobs]}
        self.commits = []
        self.preflight = Mock()

    def get(self, path):
        value = self.data.get(path)
        return copy.deepcopy(value), self.sha(value) if value is not None else None

    @staticmethod
    def sha(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)

    def put(self, path, value, sha=None):
        if sha is not None and self.get(path)[1] != sha:
            raise RuntimeError('changed')
        self.data[path] = copy.deepcopy(value)

    def put_many(self, values, expected, deletions=()):
        if any(self.get(p)[1] != sha for p, sha in expected.items()):
            raise RuntimeError('changed')
        updated = copy.deepcopy(self.data)
        for path in deletions:
            del updated[path]
        updated.update(copy.deepcopy(values))
        self.data = updated
        self.commits.append((copy.deepcopy(values), list(deletions)))

    def list_input_entries(self, jid=None):
        if jid is None:
            ids = sorted({p.split('/')[1] for p in self.data if p.startswith('inputs/')})
            return [{'path': i, 'type': 'tree', 'sha': 'tree'} for i in ids]
        prefix = 'inputs/'+jid+'/'
        return [{'path': p[len(prefix):], 'type': 'blob', 'sha': self.get(p)[1],
                 'size': len(json.dumps(v).encode())}
                for p, v in self.data.items() if p.startswith(prefix)]

    def source(self, path):
        if path == bridge.CONFIG:
            return b'{"engine":"subscription","policy_version":"risk-subscription-v1"}', 'sha'
        csv = '기준일,종목명\n'+now().date().isoformat()+',fixture\n'
        return b'{}' if path == 'seen_news.json' else csv.encode(), 'sha'


def job(jid='old', status='pending', mode='production', age=8):
    old = (now()-timedelta(days=age)).isoformat()
    inputs = {'items': [], 'raw_articles': [], 'history_hash': 'x', 'exposure_hash': 'y'}
    return {'job_id': jid, 'status': status, 'mode': mode, 'created_at': old,
            'expires_at': old, status+'_at': old, 'receipts': {}, 'attempted_count': 0,
            'input': inputs, 'input_hash': digest(inputs), 'body_manifest': [],
            'body_manifest_hash': digest([]), 'input_count': 0, 'raw_count': 0,
            'input_parts': {field: [] for field in ('items', 'raw_articles', 'body_manifest')}}


class Collection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.previous)
        self.addCleanup(self.temp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch('risk_subscription.smtplib.SMTP_SSL', side_effect=AssertionError('SMTP forbidden')))
        self.stack.enter_context(patch.dict(os.environ, {
            'GITHUB_RUN_ID': 'hardening-test', 'RISK_ENGINE': 'subscription',
            'EMAIL_SENDER': 'sender@example.invalid', 'EMAIL_PASSWORD': 'fake',
            'EMAIL_RECEIVER': 'owner@example.invalid', 'ANTHROPIC_API_KEY': 'fake',
            'NAVER_CLIENT_ID': 'fake', 'NAVER_CLIENT_SECRET': 'fake'}))
        self.bot = SimpleNamespace(KEYWORDS=['one'], get_overseas_keywords=lambda _: [],
                                   load_seen_urls=lambda: {}, load_exposure_data=lambda: {},
                                   requests=SimpleNamespace(get=Mock()),
                                   crawl_naver_news=Mock(return_value=[]))
        self.stack.enter_context(patch.object(bridge, 'import_bot', return_value=self.bot))
        self.stack.enter_context(patch.object(bridge, 'policy', return_value={}))

    def collect(self, queue):
        bridge.collect({'job_ttl_minutes': 180}, queue)

    def test_production_expiry_preserves_metadata_without_repeated_alert(self):
        queue = Memory([job()])
        with patch.object(bridge, 'notify_error') as notify:
            bridge.expire_and_check(queue)
            bridge.expire_and_check(queue)
        notify.assert_not_called()
        self.assertIn('created_at', queue.data['jobs/old.json'])
        self.assertIn('expired_at', queue.data['jobs/old.json'])
        self.assertEqual(queue.data['jobs/old.json']['status'], 'expired')

    def test_verification_expiry_does_not_alert(self):
        queue = Memory([job(mode='verification')])
        with patch.object(bridge, 'notify_error') as notify:
            bridge.expire_and_check(queue)
        notify.assert_not_called()
        self.assertEqual(queue.data['jobs/old.json']['status'], 'expired')

    def test_expiry_skips_error_notifier_and_still_collects_new_job(self):
        queue = Memory([job()])
        with patch.object(bridge, 'notify_error', side_effect=RuntimeError('SMTP mock failure')) as notify:
            self.collect(queue)
        notify.assert_not_called()
        self.assertEqual(queue.data['jobs/old.json']['status'], 'expired')
        self.assertEqual(queue.data['jobs/risk-production-hardening-test.json']['status'], 'pending')

    def test_uncertain_delivery_never_expires_or_alerts(self):
        for status in ('sending', 'uncertain', 'delivery_complete'):
            with self.subTest(status=status), patch.object(bridge, 'notify_error') as notify:
                queue = Memory([job(status=status)])
                with self.assertRaisesRegex(RuntimeError, '미종결'):
                    bridge.expire_and_check(queue)
                self.assertEqual(queue.data['jobs/old.json']['status'], status)
                notify.assert_not_called()

    def test_expiration_persistence_failure_does_not_emit_success_alert(self):
        queue = Memory([job()])
        queue.put = Mock(side_effect=RuntimeError('queue failed'))
        with patch.object(bridge, 'notify_error') as notify, self.assertRaises(RuntimeError):
            bridge.expire_and_check(queue)
        notify.assert_not_called()

    def test_http200_broken_json_three_times_stops_partial_collection(self):
        # Exercise the real crawler's retries, not a replica of its error handling.
        from naver_news_monitor import crawl_naver_news
        import naver_news_monitor as native
        self.bot.requests = native.requests
        self.bot.crawl_naver_news = crawl_naver_news
        response = Mock()
        response.json.side_effect = ValueError('broken JSON')
        queue = Memory()
        with patch.object(native.requests, 'get', return_value=response) as get, patch.object(native.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '일부 검색 수집 실패'):
                self.collect(queue)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(queue.commits, [])
        self.assertEqual(queue.data['index.json']['jobs'], [])

    def test_json_retry_recovers_and_clears_previous_failure(self):
        from naver_news_monitor import crawl_naver_news
        import naver_news_monitor as native
        self.bot.requests = native.requests
        self.bot.crawl_naver_news = crawl_naver_news
        bad, good = Mock(), Mock()
        bad.json.side_effect = ValueError('broken JSON')
        good.json.return_value = {'items': []}
        queue = Memory()
        with patch.object(native.requests, 'get', side_effect=[bad, good]) as get, patch.object(native.time, 'sleep'):
            self.collect(queue)
        self.assertEqual(get.call_count, 2)
        self.assertIn('jobs/risk-production-hardening-test.json', queue.data)

    def test_another_successful_query_cannot_hide_failed_query(self):
        from naver_news_monitor import crawl_naver_news
        import naver_news_monitor as native
        self.bot.requests, self.bot.crawl_naver_news = native.requests, crawl_naver_news
        self.bot.KEYWORDS = ['bad', 'good']
        good, bad = Mock(), Mock()
        good.json.return_value = {'items': []}
        bad.json.side_effect = ValueError('broken JSON')
        with patch.object(native.requests, 'get', side_effect=lambda url, **k: bad if k['params']['query'] == 'bad' else good), patch.object(native.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '일부 검색 수집 실패'):
                self.collect(Memory())

    def test_cleanup_exception_does_not_block_collection_or_send(self):
        queue = Memory()
        queue.list_input_entries = Mock(side_effect=RuntimeError('tree unavailable'))
        with patch.object(bridge, 'notify_error', side_effect=RuntimeError('SMTP mock failure')) as notify:
            self.collect(queue)
        notify.assert_called_once()
        self.assertIn('jobs/risk-production-hardening-test.json', queue.data)


class Retention(unittest.TestCase):
    def fixture(self, status='completed', age=8):
        metadata = job(status=status, age=age)
        metadata.update(receipts={'owner': 'accepted'}, expected_receipts=['owner'], ack=True, history_saved=True)
        if status in ('expired', 'cancelled'):
            metadata.update(receipts={}, expected_receipts=[])
        queue = Memory([metadata])
        queue.data.update({'inputs/old/items-000.json': [{'article_id': 'a'}],
                           'inputs/old/body_manifest-000.json': [{'body': 'fixture'}],
                           'results/old.json': {'analysis': 'preserved'},
                           'previews/old.json': {'html': 'preserved'},
                           'logs/old.json': {'log': 'preserved'}})
        return queue

    def test_all_old_terminal_states_prune_atomically_preserving_metadata_and_receipts(self):
        for status in bridge.TERMINAL:
            with self.subTest(status=status):
                queue = self.fixture(status)
                original = copy.deepcopy(queue.data)
                bridge.prune_terminal_inputs(queue)
                self.assertEqual(len(queue.commits), 1)
                values, deleted = queue.commits[0]
                self.assertEqual(set(values), {'jobs/old.json'})
                self.assertEqual(set(deleted), {'inputs/old/items-000.json', 'inputs/old/body_manifest-000.json'})
                for key, value in original['jobs/old.json'].items():
                    self.assertEqual(queue.data['jobs/old.json'][key], value)
                for path in ('index.json', 'results/old.json', 'previews/old.json', 'logs/old.json'):
                    self.assertEqual(queue.data[path], original[path])
                self.assertEqual(queue.data['jobs/old.json']['inputs_pruned_files'], 2)
                bridge.prune_terminal_inputs(queue)
                self.assertEqual(len(queue.commits), 1)

    def test_old_terminal_outside_index_is_still_pruned(self):
        queue = self.fixture()
        queue.data['index.json']['jobs'] = []
        bridge.prune_terminal_inputs(queue)
        self.assertFalse(any(p.startswith('inputs/old/') for p in queue.data))
        self.assertIn('jobs/old.json', queue.data)

    def test_nonterminal_states_never_pruned_even_if_old(self):
        for status in ('pending', 'sending', 'uncertain', 'delivery_complete'):
            with self.subTest(status=status):
                queue = self.fixture(status)
                original = copy.deepcopy(queue.data)
                bridge.prune_terminal_inputs(queue)
                self.assertEqual(queue.data, original)
                self.assertEqual(queue.commits, [])

    def test_recent_completion_is_preserved_regardless_of_old_creation(self):
        queue = self.fixture(age=60)
        queue.data['jobs/old.json']['completed_at'] = now().isoformat()
        original = copy.deepcopy(queue.data)
        bridge.prune_terminal_inputs(queue)
        self.assertEqual(queue.data, original)

    def test_missing_or_malformed_terminal_time_is_preserved(self):
        for value in (None, 'not-a-date', '2020-01-01T00:00:00'):
            with self.subTest(value=value):
                queue = self.fixture()
                queue.data['jobs/old.json']['completed_at'] = value
                original = copy.deepcopy(queue.data)
                bridge.prune_terminal_inputs(queue)
                self.assertEqual(queue.data, original)

    def test_uncertain_receipts_and_attempted_expirations_are_preserved(self):
        for status in ('completed', 'expired', 'cancelled'):
            with self.subTest(status=status):
                queue = self.fixture(status)
                if status == 'completed':
                    queue.data['jobs/old.json']['receipts']['owner'] = 'uncertain'
                else:
                    queue.data['jobs/old.json']['attempted_count'] = 1
                original = copy.deepcopy(queue.data)
                bridge.prune_terminal_inputs(queue)
                self.assertEqual(queue.data, original)

    def test_job_state_race_prevents_all_deletions(self):
        queue = self.fixture()
        listing = queue.list_input_entries
        def race(jid=None):
            rows = listing(jid)
            if jid:
                queue.data['jobs/old.json']['status'] = 'uncertain'
            return rows
        queue.list_input_entries = race
        with self.assertRaises(RuntimeError):
            bridge.prune_terminal_inputs(queue)
        self.assertIn('inputs/old/items-000.json', queue.data)
        self.assertIn('inputs/old/body_manifest-000.json', queue.data)
        self.assertEqual(queue.commits, [])

    def test_only_owned_directory_is_pruned_even_with_cross_job_descriptor(self):
        queue = self.fixture()
        queue.data['jobs/old.json']['input_parts']['items'] = [{'path': 'inputs/live/items-000.json'}]
        queue.data['jobs/live.json'] = job('live', status='uncertain')
        queue.data['inputs/live/items-000.json'] = ['must survive']
        bridge.prune_terminal_inputs(queue)
        self.assertEqual(queue.data['inputs/live/items-000.json'], ['must survive'])
        self.assertEqual(queue.data['jobs/live.json']['status'], 'uncertain')

    def test_retention_is_configurable_but_not_zero_or_negative(self):
        queue = self.fixture(age=8)
        bridge.prune_terminal_inputs(queue, retention_days=10)
        self.assertEqual(queue.commits, [])
        bridge.prune_terminal_inputs(queue, retention_days=2)
        self.assertEqual(len(queue.commits), 1)
        for days in (0, -1, True, '7'):
            with self.assertRaises(ValueError):
                bridge.prune_terminal_inputs(queue, days)


class AtomicQueue(unittest.TestCase):
    def queue(self, changed=False, fail_patch=False):
        queue = object.__new__(Queue)
        calls = []
        def request(path, method='GET', data=None):
            calls.append((path, method, data))
            if path == '/git/ref/heads/main': return {'object': {'sha': 'head'}}
            if path == '/git/commits/head': return {'tree': {'sha': 'base'}}
            if path == '/git/trees' and method == 'POST': return {'sha': 'tree'}
            if path == '/git/commits' and method == 'POST': return {'sha': 'commit'}
            if method == 'PATCH':
                if fail_patch: raise RuntimeError('non-fast-forward')
                return {}
            raise AssertionError('unexpected request')
        queue.request = request
        queue.get = lambda p, ref='main': ({}, 'changed' if changed else 'sha')
        return queue, calls

    def test_delete_and_metadata_share_one_commit_and_nonforce_ref(self):
        queue, calls = self.queue()
        queue.put_many({'jobs/old.json': {'status': 'completed'}},
                       {'jobs/old.json': 'sha', 'inputs/old/items-000.json': 'sha'},
                       deletions=['inputs/old/items-000.json'])
        tree = next(c[2] for c in calls if c[0] == '/git/trees')
        self.assertEqual(tree['base_tree'], 'base')
        self.assertEqual(tree['tree'][1], {'path': 'subscription/risk/inputs/old/items-000.json',
                                         'mode': '100644', 'type': 'blob', 'sha': None})
        self.assertEqual(calls[-1], ('/git/refs/heads/main', 'PATCH', {'sha': 'commit', 'force': False}))
        self.assertEqual(sum(method == 'PATCH' for _, method, _ in calls), 1)

    def test_changed_lease_prevents_any_write(self):
        queue, calls = self.queue(changed=True)
        with self.assertRaisesRegex(RuntimeError, 'SHA 변경'):
            queue.put_many({}, {'inputs/old/items-000.json': 'sha'}, ['inputs/old/items-000.json'])
        self.assertTrue(all(method == 'GET' for _, method, _ in calls))

    def test_failed_ref_update_is_never_retried(self):
        queue, calls = self.queue(fail_patch=True)
        with self.assertRaises(RuntimeError):
            queue.put_many({}, {'inputs/old/items-000.json': 'sha'}, ['inputs/old/items-000.json'])
        self.assertEqual(sum(method == 'PATCH' for _, method, _ in calls), 1)

    def test_deleting_metadata_other_prefix_or_without_sha_is_rejected(self):
        for path, sha in [('jobs/old.json', 'sha'), ('results/old.json', 'sha'),
                          ('../inputs/old/a.json', 'sha'), ('inputs/old/../../a.json', 'sha'),
                          ('inputs/old/items-000.json', None)]:
            with self.subTest(path=path):
                queue, calls = self.queue()
                with self.assertRaises(ValueError):
                    queue.put_many({}, {path: sha}, [path])
                self.assertEqual(calls, [])

    def test_input_tree_listing_is_scoped_and_includes_unindexed_jobs(self):
        queue = object.__new__(Queue)
        trees = {'root': [{'path': 'subscription', 'type': 'tree', 'sha': 'sub'}],
                 'sub': [{'path': 'risk', 'type': 'tree', 'sha': 'risk'}],
                 'risk': [{'path': 'inputs', 'type': 'tree', 'sha': 'inputs'}],
                 'inputs': [{'path': 'old', 'type': 'tree', 'sha': 'old'}],
                 'old': [{'path': 'items-000.json', 'type': 'blob', 'sha': 'blob', 'size': 8}]}
        def request(path):
            if path == '/git/ref/heads/main': return {'object': {'sha': 'head'}}
            if path == '/git/commits/head': return {'tree': {'sha': 'root'}}
            return {'tree': trees[path.removeprefix('/git/trees/')], 'truncated': False}
        queue.request = request
        self.assertEqual(queue.list_input_entries(), trees['inputs'])
        self.assertEqual(queue.list_input_entries('old'), trees['old'])
        trees['risk'] = []
        self.assertEqual(queue.list_input_entries(), [])

    def test_truncated_tree_is_not_treated_as_complete_inventory(self):
        queue = object.__new__(Queue)
        queue.request = lambda p: ({'object': {'sha': 'head'}} if p == '/git/ref/heads/main'
                                   else {'tree': {'sha': 'root'}} if p == '/git/commits/head'
                                   else {'tree': [], 'truncated': True})
        with self.assertRaisesRegex(RuntimeError, '목록 불완전'):
            queue.list_input_entries()


if __name__ == '__main__':
    unittest.main()
