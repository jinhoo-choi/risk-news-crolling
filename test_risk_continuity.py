"""Frozen balances and unresolved replay through native pipeline, with mocked I/O."""
import base64
import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

import risk_continuity as continuity
import risk_subscription as bridge
from risk_queue import digest, now
from risk_shards import hydrate
import test_risk_subscription as native_fixtures
import test_risk_hardening as memory_fixtures


class NativeContinuity(unittest.TestCase):
    setUp = native_fixtures.Integration.setUp
    tearDown = native_fixtures.Integration.tearDown
    fixture = native_fixtures.Integration.fixture
    run_consume = native_fixtures.Integration.run_consume

    def frozen(self, queue):
        job = queue.data['jobs/test.json']
        queue.data.update(continuity.freeze_exposure(job, Path('exposure_data.csv').read_bytes(), now().isoformat()))
        return job

    def mixed(self):
        queue = self.fixture()
        job = self.frozen(queue)
        row = copy.deepcopy(job['input']['items'][0])
        row.update(article_id='held', title='사건 주체 확인이 필요한 기사', url='https://example.invalid/held')
        job['input']['items'].append(row)
        job['input']['raw_articles'].append(copy.deepcopy(row))
        job['body_manifest'].append({'article_id': 'held', 'body': '이번 사건의 직접 주체를 확인할 문단이 부족하다.',
                                     'body_status': 'verified', 'source_url': row['url']})
        result = queue.data['results/test.json']
        result['classifications'].append({'article_id': 'held', 'relevant': None, 'reason': '행위 주체 확인 보류'})
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        return queue

    def seed_review(self, queue, aid='one'):
        job = queue.data['jobs/test.json']
        values, expected, _ = continuity.prepare_reviews(queue, job, {aid: None})
        queue.put_many(values, expected)

    def ack(self, queue):
        queue.remote_seen = Path('seen_news.json').read_bytes()
        bridge.acknowledge(queue, 'test')

    def test_current_balance_change_uses_snapshot_and_preserves_main_csv(self):
        queue = self.fixture()
        self.frozen(queue)
        original = Path('exposure_data.csv').read_bytes()
        Path('exposure_data.csv').write_bytes(original.replace(b'150', b'999'))
        current = Path('exposure_data.csv').read_bytes()
        observed = []
        load = self.bot.load_exposure_data
        def capture():
            observed.append(Path(self.bot.EXPOSURE_FILE).read_bytes())
            return load()
        with patch.object(self.bot, 'load_exposure_data', capture):
            self.run_consume(queue, native_fixtures.SMTP())
        self.assertTrue(observed)
        self.assertTrue(all(raw == original for raw in observed))
        self.assertEqual(Path('exposure_data.csv').read_bytes(), current)
        self.assertEqual(self.bot.EXPOSURE_FILE, 'exposure_data.csv')

    def test_legacy_balance_change_still_stops_before_smtp(self):
        queue, smtp = self.fixture(), native_fixtures.SMTP()
        Path('exposure_data.csv').write_text('changed')
        with self.assertRaisesRegex(RuntimeError, '잔고 변경'):
            self.run_consume(queue, smtp)
        self.assertEqual(smtp.calls, [])

    def test_frozen_job_still_blocks_changed_main_history(self):
        queue, smtp = self.fixture(), native_fixtures.SMTP()
        self.frozen(queue)
        queue.remote_seen = b'{"changed":true}'
        with self.assertRaisesRegex(RuntimeError, '이력'):
            self.run_consume(queue, smtp)
        self.assertEqual(smtp.calls, [])

    def test_missing_changed_or_cross_job_snapshot_never_falls_back(self):
        for mutation in ('missing', 'changed', 'other_job'):
            with self.subTest(mutation=mutation):
                queue, smtp = self.fixture(), native_fixtures.SMTP()
                job = self.frozen(queue)
                path = job['exposure_snapshot']['path']
                if mutation == 'missing': del queue.data[path]
                if mutation == 'changed': queue.data[path]['content'] = base64.b64encode(b'changed').decode()
                if mutation == 'other_job': job['exposure_snapshot']['path'] = 'inputs/other/exposure.json'
                with self.assertRaises(RuntimeError): self.run_consume(queue, smtp)
                self.assertEqual(smtp.calls, [])

    def test_mixed_null_is_persisted_before_smtp_without_changing_answers_or_seen(self):
        queue, smtp = self.mixed(), native_fixtures.SMTP()
        original = copy.deepcopy(queue.data['results/test.json'])
        send = smtp.sendmail
        def checked(*args):
            self.assertEqual(queue.data['review/held.json']['status'], 'pending')
            self.assertEqual(queue.data['review/index.json']['pending'], ['held'])
            return send(*args)
        smtp.sendmail = checked
        self.run_consume(queue, smtp)
        self.assertEqual(len(smtp.calls), 3)
        self.assertEqual(queue.data['results/test.json'], original)
        self.assertNotIn('https://example.invalid/held', self.bot.load_seen_urls())
        self.assertFalse(queue.data['jobs/test.json']['analysis_complete'])
        html = queue.data['previews/test.json']['html']
        self.assertIn('확인 보류 1건', html)
        self.assertIn('<table', html)
        self.ack(queue)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'completed')
        self.assertEqual(queue.data['review/held.json']['status'], 'pending')

    def test_all_null_sends_owner_status_without_claiming_no_risk(self):
        queue, smtp = self.fixture(), native_fixtures.SMTP()
        self.frozen(queue)
        result = queue.data['results/test.json']
        result['classifications'][0]['relevant'] = None
        result['analyses'] = {}
        self.run_consume(queue, smtp)
        self.assertEqual([r[0] for r in smtp.calls], [['owner@example.invalid']])
        html = queue.data['previews/test.json']['html']
        self.assertIn('확인 보류 기사가 있습니다.', html)
        self.assertNotIn('해당하는 뉴스가 없습니다.', html)
        self.assertEqual(queue.data['review/index.json']['pending'], ['one'])
        self.assertNotIn(queue.data['jobs/test.json']['input']['items'][0]['url'], self.bot.load_seen_urls())

    def test_repeated_review_over_one_day_is_visible_without_grade_escalation(self):
        queue, smtp = self.mixed(), native_fixtures.SMTP()
        self.seed_review(queue, 'held')
        queue.data['review/held.json']['first_seen_at'] = (now()-timedelta(days=2)).isoformat()
        self.run_consume(queue, smtp)
        self.assertIn('24시간 이상 미확인 1건', queue.data['previews/test.json']['html'])
        self.assertIsNone(queue.data['results/test.json']['classifications'][-1]['relevant'])
        self.assertEqual(queue.data['jobs/test.json']['overdue_review_count'], 1)

    def test_failed_private_review_commit_prevents_all_smtp(self):
        queue, smtp = self.mixed(), native_fixtures.SMTP()
        queue.put_many = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('private write failed'))
        with self.assertRaisesRegex(RuntimeError, 'private write'):
            self.run_consume(queue, smtp)
        self.assertEqual(smtp.calls, [])
        self.assertEqual(queue.data['jobs/test.json']['status'], 'pending')

    def test_uncertain_delivery_keeps_review_and_never_acks(self):
        queue, smtp = self.mixed(), native_fixtures.SMTP(fail_at=1)
        with self.assertRaises(RuntimeError): self.run_consume(queue, smtp)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'uncertain')
        self.assertEqual(queue.data['review/held.json']['status'], 'pending')
        with self.assertRaises(RuntimeError): self.ack(queue)
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_verified_true_and_false_resolve_only_with_history_ack(self):
        for verdict in (True, False):
            with self.subTest(verdict=verdict):
                Path('seen_news.json').write_bytes(self.before)
                queue, smtp = self.fixture(), native_fixtures.SMTP()
                self.frozen(queue)
                self.seed_review(queue)
                if verdict is False:
                    queue.data['results/test.json']['classifications'][0]['relevant'] = False
                    queue.data['results/test.json']['analyses'] = {}
                self.run_consume(queue, smtp)
                self.assertEqual(queue.data['review/one.json']['status'], 'pending')
                self.ack(queue)
                self.assertEqual(queue.data['review/one.json']['status'], 'resolved')
                self.assertIs(queue.data['review/one.json']['relevant'], verdict)
                self.assertEqual(queue.data['review/one.json']['resolved_by_job'], 'test')
                self.assertEqual(queue.data['review/index.json']['pending'], [])

    def test_changed_review_after_delivery_prevents_resolution_and_completion(self):
        queue, smtp = self.fixture(), native_fixtures.SMTP()
        self.frozen(queue)
        self.seed_review(queue)
        self.run_consume(queue, smtp)
        queue.data['review/one.json']['reason'] = 'changed after delivery'
        with self.assertRaisesRegex(RuntimeError, '보류 원본 변경'):
            self.ack(queue)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'delivery_complete')
        self.assertEqual(queue.data['review/one.json']['status'], 'pending')
        self.assertEqual(len(smtp.calls), 3)

    def test_owner_verification_null_does_not_write_production_review(self):
        queue, smtp = self.fixture('verification'), native_fixtures.SMTP()
        self.frozen(queue)
        queue.data['results/test.json']['classifications'][0]['relevant'] = None
        queue.data['results/test.json']['analyses'] = {}
        with self.assertRaisesRegex(RuntimeError, '확인 보류'):
            self.run_consume(queue, smtp, self_test=True)
        self.assertNotIn('review/index.json', queue.data)
        self.assertEqual(smtp.calls, [])

    def test_expired_unprocessed_input_is_copied_before_terminal_state(self):
        queue = self.fixture()
        queue.data['jobs/test.json']['expires_at'] = (now()-timedelta(seconds=1)).isoformat()
        with patch.object(bridge, 'notify_error'):
            bridge.expire_and_check(queue)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'expired')
        self.assertEqual(queue.data['review/index.json']['pending'], ['one'])
        self.assertEqual(queue.data['review/one.json']['reason'], 'expired_unprocessed')

    def test_expired_copy_failure_leaves_job_pending(self):
        queue = self.fixture()
        queue.data['jobs/test.json']['expires_at'] = (now()-timedelta(seconds=1)).isoformat()
        queue.put_many = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('atomic failure'))
        with self.assertRaises(RuntimeError), patch.object(bridge, 'notify_error') as notify:
            bridge.expire_and_check(queue)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'pending')
        notify.assert_not_called()


class ReplayCollection(unittest.TestCase):
    setUp = memory_fixtures.Collection.setUp
    collect = memory_fixtures.Collection.collect

    def seed(self):
        queue = memory_fixtures.Memory()
        item = {'article_id': 'held', 'title': '실제 사건 주체 확인 기사', 'desc': '보류 원문 설명',
                'description': '보류 원문 설명', 'url': 'https://example.invalid/held',
                'pubDate': (now()-timedelta(days=60)).isoformat()}
        body = {'article_id': 'held', 'body': '본문에 확인 가능한 사실을 보존한다.',
                'body_status': 'verified', 'source_url': item['url']}
        job = {'job_id': 'old', 'input': {'items': [item]}, 'body_manifest': [body]}
        values, expected, _ = continuity.prepare_reviews(queue, job, {'held': None})
        queue.put_many(values, expected)
        self.bot.is_hard_excluded = lambda *a: (False, '')
        self.bot.fetch_article_body = lambda *a: ''
        return queue

    def test_sixty_day_old_review_replays_with_original_body_outside_collection_window(self):
        queue = self.seed()
        self.collect(queue)
        small = queue.data['jobs/risk-production-hardening-test.json']
        full = hydrate(queue, small)
        self.assertEqual([a['article_id'] for a in full['input']['items']], ['held'])
        self.assertTrue(full['input']['items'][0]['_review_replay'])
        self.assertEqual(full['body_manifest'][0]['body'], queue.data['review/held.json']['body']['body'])
        self.assertEqual(queue.data['review/held.json']['status'], 'pending')
        self.assertIn('exposure_snapshot', small)

    def test_exposure_is_captured_once_and_not_refreshed_after_filter(self):
        queue = self.seed()
        source, reads = queue.source, []
        def counted(path):
            reads.append(path)
            return source(path)
        queue.source = counted
        self.collect(queue)
        self.assertEqual(reads.count('exposure_data.csv'), 1)
        self.assertEqual(reads.count('seen_news.json'), 2)

    def test_corrupt_pending_source_blocks_collection_instead_of_dropping_it(self):
        queue = self.seed()
        queue.data['review/held.json']['body']['body'] = 'tampered'
        with self.assertRaisesRegex(RuntimeError, '영속 보류 원본'):
            self.collect(queue)
        self.assertNotIn('jobs/risk-production-hardening-test.json', queue.data)


class SnapshotRules(unittest.TestCase):
    def test_malformed_review_index_is_not_replaced_with_empty_backlog(self):
        for value in ([], {}, {'schema_version': 1, 'pending': ['a', 'a']}):
            queue = memory_fixtures.Memory()
            queue.data[continuity.REVIEW_INDEX] = value
            with self.assertRaises(RuntimeError):
                continuity.pending_reviews(queue)
            self.assertEqual(queue.data[continuity.REVIEW_INDEX], value)

    def test_duplicate_input_or_missing_body_blocks_defer_without_losing_source(self):
        for items, bodies in [([{'article_id': 'a'}]*2, [{'article_id': 'a'}]),
                              ([{'article_id': 'a'}], [])]:
            queue = memory_fixtures.Memory()
            job = {'job_id': 'test', 'input': {'items': items}, 'body_manifest': bodies}
            with self.assertRaisesRegex(RuntimeError, 'ID 중복·누락'):
                continuity.prepare_reviews(queue, job, {'a': None})
            self.assertNotIn(continuity.REVIEW_INDEX, queue.data)

    def test_repeated_null_does_not_replace_saved_original_with_a_worse_fetch(self):
        queue = memory_fixtures.Memory()
        job = {'job_id': 'first', 'input': {'items': [{'article_id': 'a', 'title': 'source'}]},
               'body_manifest': [{'article_id': 'a', 'body': 'original verified source'}]}
        values, expected, _ = continuity.prepare_reviews(queue, job, {'a': None})
        queue.put_many(values, expected)
        job['job_id'] = 'second'
        job['body_manifest'][0]['body'] = 'menu only'
        values, expected, _ = continuity.prepare_reviews(queue, job, {'a': None})
        queue.put_many(values, expected)
        self.assertEqual(continuity.pending_reviews(queue)[0]['body']['body'], 'original verified source')

    def test_pruning_old_inputs_preserves_unresolved_original_copy(self):
        queue = memory_fixtures.Retention().fixture()
        full = {'job_id': 'old', 'input': {'items': [{'article_id': 'held', 'title': 'source'}]},
                'body_manifest': [{'article_id': 'held', 'body': 'preserved original'}]}
        values, expected, _ = continuity.prepare_reviews(queue, full, {'held': None})
        queue.put_many(values, expected)
        bridge.prune_terminal_inputs(queue)
        self.assertFalse(any(p.startswith('inputs/old/') for p in queue.data))
        self.assertEqual(continuity.pending_reviews(queue)[0]['body']['body'], 'preserved original')

    def test_same_job_retry_does_not_increment_attempts_or_reset_first_seen(self):
        queue = memory_fixtures.Memory()
        job = {'job_id': 'run', 'input': {'items': [{'article_id': 'a'}]},
               'body_manifest': [{'article_id': 'a', 'body': 'source'}]}
        for i in range(2):
            values, expected, _ = continuity.prepare_reviews(queue, job, {'a': None})
            queue.put_many(values, expected)
            if not i: first = queue.data['review/a.json']['first_seen_at']
        self.assertEqual(queue.data['review/a.json']['attempts'], 1)
        self.assertEqual(queue.data['review/a.json']['first_seen_at'], first)

    def test_future_stale_and_mixed_dates_fail_closed(self):
        for offset in (-1, 8):
            ref = now().astimezone(continuity.timezone(timedelta(hours=9))).date()-timedelta(days=offset)
            raw = ('기준일,종목명\n'+ref.isoformat()+',test\n').encode()
            job = {'job_id': 'test', 'input': {'exposure_hash': hashlib.sha256(raw).hexdigest()}}
            values = continuity.freeze_exposure(job, raw, now().isoformat())
            queue = memory_fixtures.Memory(); queue.data.update(values)
            with self.assertRaisesRegex(RuntimeError, '기준일'):
                continuity.read_exposure(queue, job, {})
        with self.assertRaises(RuntimeError):
            continuity.exposure_date('기준일,종목명\n2026-10-13,a\n2026-10-14,b'.encode())

    def test_repeated_null_has_no_attempt_limit_or_automatic_downgrade(self):
        queue = memory_fixtures.Memory()
        item = {'article_id': 'a', 'title': 'source'}
        job = {'job_id': 'run0', 'input': {'items': [item]},
               'body_manifest': [{'article_id': 'a', 'body': 'source'}]}
        for i in range(12):
            job['job_id'] = 'run'+str(i)
            values, expected, _ = continuity.prepare_reviews(queue, job, {'a': None})
            queue.put_many(values, expected)
        self.assertEqual(queue.data['review/a.json']['attempts'], 12)
        self.assertEqual(queue.data['review/a.json']['status'], 'pending')
        self.assertEqual(queue.data['review/index.json']['pending'], ['a'])


if __name__ == '__main__':
    unittest.main()
