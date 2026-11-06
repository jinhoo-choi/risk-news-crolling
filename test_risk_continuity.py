"""Frozen balances and unresolved replay through native pipeline, with mocked I/O."""
import base64
import copy
import hashlib
import io
import json
import threading
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

    def streamlined(self):
        queue = self.mixed()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        job.update(review_pass_version=2, action_quality_version=2, exclusion_review_version=2)
        job['body_manifest'][1] = dict(job['body_manifest'][0], article_id='held')
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        result['classifications'][1]['relevant'] = True
        result['analyses']['held'] = copy.deepcopy(result['analyses']['one'])
        return queue

    def test_missing_reason_isolated_but_missing_identity_still_blocks(self):
        queue, smtp = self.streamlined(), native_fixtures.SMTP()
        del queue.data['results/test.json']['classifications'][1]['reason']
        self.run_consume(queue, smtp)
        self.assertEqual(queue.data['jobs/test.json']['deferred_ids'], ['held'])
        self.assertEqual(len(smtp.calls), 3)

    def test_explicit_unprocessed_partition_delivers_ready_without_seen_for_unread(self):
        queue, smtp = self.streamlined(), native_fixtures.SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        job['delivery_focus_version'] = 1
        result['classifications'].pop()
        result['analyses'].pop('held')
        result['unprocessed_ids'] = ['held']
        original = copy.deepcopy(result)
        self.run_consume(queue, smtp)
        state = queue.data['jobs/test.json']
        self.assertEqual(state['unprocessed_ids'], ['held'])
        self.assertFalse(state['analysis_complete'])
        self.assertEqual(queue.data['review/held.json']['analysis_state'], 'unprocessed')
        self.assertEqual(queue.data['results/test.json'], original)
        self.assertNotIn('https://example.invalid/held', Path('seen_news.json').read_text())
        self.assertEqual(len(smtp.calls), 3)

    def test_unprocessed_partition_rejects_overlap_missing_ids_and_no_ready_candidate(self):
        from risk_result_validator import validate
        for mutation in ('overlap', 'missing', 'no-ready', 'legacy'):
            queue = self.streamlined()
            job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
            job['delivery_focus_version'] = 1
            result['classifications'].pop()
            result['analyses'].pop('held')
            result['unprocessed_ids'] = ['held']
            if mutation == 'overlap':
                result['unprocessed_ids'].append('one')
            elif mutation == 'missing':
                result['unprocessed_ids'] = []
            elif mutation == 'legacy':
                job.pop('delivery_focus_version')
            else:
                result['classifications'] = []
                result['analyses'] = {}
                result['unprocessed_ids'].append('one')
            with self.assertRaises(ValueError):
                validate(result, job, job['input'], job['body_manifest'])

    def test_saved_subscription_adapter_error_never_retries_identical_data(self):
        queue = self.streamlined()
        article = queue.data['jobs/test.json']['input']['raw_articles'][0]
        with patch.object(self.bot, 'RISK_PROVIDER', object()), \
                patch.object(self.bot, '_ai_post', side_effect=RuntimeError('adapter shape')) as request, \
                patch.object(self.bot.time, 'sleep') as wait:
            with self.assertRaises(self.bot.AnalysisIncompleteError):
                self.bot.ai_filter_batch([article])
        self.assertEqual(request.call_count, 1)
        wait.assert_not_called()

    def test_native_no_cards_with_unprocessed_never_sends_a_digest(self):
        queue, smtp = self.streamlined(), native_fixtures.SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        job['delivery_focus_version'] = 1
        result['classifications'].pop()
        result['analyses'].pop('held')
        result['unprocessed_ids'] = ['held']
        with patch.object(self.bot, 'filter_articles_for_scope', return_value=[]), \
                patch.object(self.bot, 'build_price_alert_section', self.crash_digest()):
            with self.assertRaisesRegex(RuntimeError, '미처리 분석'):
                self.run_consume(queue, smtp)
        self.assertEqual(smtp.calls, [])
        self.assertEqual(queue.data['jobs/test.json']['status'], 'pending')
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_native_narrative_targets_stop_before_generation_and_do_not_write(self):
        from risk_review_context import narrative_targets, ready_result
        queue = self.streamlined()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        job['delivery_focus_version'] = 1
        result['event_groups'] = [{'article_ids': ['one', 'held'], 'reason': '동일 당사 장애', 'reviewed': True}]
        job['event_grouping_version'] = 1
        result['analyses']['held']['confidence'] = 0.8
        before = Path('seen_news.json').read_bytes()
        targets = narrative_targets(job, result, self.bot)
        self.assertEqual(targets, ['one'])
        self.assertEqual(Path('seen_news.json').read_bytes(), before)
        checkpoint = {key: job[key] for key in ('job_id', 'input_hash', 'policy_hash', 'body_manifest_hash')}
        checkpoint.update(covered_ids=['one'], classifications=result['classifications'][:1],
                          analyses={'one': result['analyses']['one']})
        partial = ready_result(job, [checkpoint])
        self.assertEqual(partial['unprocessed_ids'], ['held'])
        self.assertEqual(partial['analyses'], {'one': result['analyses']['one']})

    def test_v2_bad_fact_or_schema_holds_only_article_without_mutating_saved_answer(self):
        for field, value in [('fact_summary', '손실 987654321원 발생.'),
                             ('entity', '한국투자증권이 아닌 주체'), ('evidence', []),
                             ('body_status', 'invalid'), ('analysis', None)]:
            with self.subTest(field=field):
                Path('seen_news.json').write_bytes(self.before)
                queue, smtp = self.streamlined(), native_fixtures.SMTP()
                result = queue.data['results/test.json']
                if field == 'analysis':
                    result['analyses']['held'] = value
                else:
                    result['analyses']['held'][field] = value
                original = copy.deepcopy(result)
                self.run_consume(queue, smtp)
                state = queue.data['jobs/test.json']
                self.assertEqual(state['deferred_ids'], ['held'])
                self.assertEqual(len(smtp.calls), 3)
                self.assertEqual(queue.data['results/test.json'], original)
                self.assertNotIn('https://example.invalid/held', Path('seen_news.json').read_text())
                self.assertIn('https://example.invalid/test/one', queue.data['previews/test.json']['html'])

    def test_v2_optional_text_shape_omitted_and_missing_action_plan_does_not_block(self):
        queue, smtp = self.streamlined(), native_fixtures.SMTP()
        result = queue.data['results/test.json']
        result['analyses']['held'].update(action=['bad format'], customer_notice='안내\n'*6,
                                         action_plan={'incomplete': True})
        original = copy.deepcopy(result)
        self.run_consume(queue, smtp)
        self.assertEqual(queue.data['jobs/test.json']['deferred_ids'], [])
        self.assertEqual(set(queue.data['jobs/test.json']['omitted_fields']['held']), {'action', 'customer_notice'})
        self.assertEqual(queue.data['results/test.json'], original)
        self.assertEqual(len(smtp.calls), 3)

    def test_v2_missing_optional_text_and_invalid_group_member_keep_valid_peer(self):
        queue, smtp = self.streamlined(), native_fixtures.SMTP()
        result = queue.data['results/test.json']
        del result['analyses']['one']['action']
        del result['analyses']['one']['customer_notice']
        result['analyses']['held']['evidence'][0]['quote'] = '원문에 없는 근거 문장'
        result['event_groups'] = [{'article_ids': ['one', 'held'], 'reason': '같은 장애 사건', 'reviewed': True}]
        self.run_consume(queue, smtp)
        self.assertEqual(queue.data['jobs/test.json']['deferred_ids'], ['held'])
        self.assertEqual(len(smtp.calls), 3)

    def test_v2_bad_classification_reason_is_individual_not_batch_failure(self):
        queue, smtp = self.streamlined(), native_fixtures.SMTP()
        queue.data['results/test.json']['classifications'][1]['reason'] = '긴 사유'*1000
        self.run_consume(queue, smtp)
        self.assertEqual(queue.data['jobs/test.json']['deferred_ids'], ['held'])
        self.assertEqual(len(smtp.calls), 3)

    def test_v2_input_corruption_still_prevents_every_smtp_call(self):
        for mutation in ['hash', 'missing-id', 'duplicate-id']:
            with self.subTest(mutation=mutation):
                queue, smtp = self.streamlined(), native_fixtures.SMTP()
                result = queue.data['results/test.json']
                if mutation == 'hash':
                    result['input_hash'] = '0'*64
                elif mutation == 'missing-id':
                    result['classifications'].pop()
                else:
                    result['classifications'].append(copy.deepcopy(result['classifications'][0]))
                with self.assertRaises(ValueError):
                    self.run_consume(queue, smtp)
                self.assertEqual(smtp.calls, [])

    def test_v2_risky_unreadable_exclusion_is_held_and_null_needs_no_recheck(self):
        queue = self.streamlined()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        result['classifications'][0]['relevant'] = None
        result['classifications'][1]['relevant'] = False
        result['analyses'] = {}
        job['input']['items'][1]['title'] = '횡령 발생 의혹'
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest'][1].update(body='', body_status='missing')
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        info = bridge.validate(result, job, job['input'], job['body_manifest'])
        self.assertEqual(set(info['held_articles']), {'held'})
        self.assertEqual(info['needs_review'], 2)

    def test_review_aging_after_collection_is_held_before_smtp(self):
        queue = self.fixture()
        job = self.frozen(queue)
        item = job['input']['items'][0]
        item.update(_review_replay=True, published_at=(now()-timedelta(hours=12, seconds=1)).isoformat())
        job['input_hash'] = queue.data['results/test.json']['input_hash'] = digest(job['input'])
        smtp = native_fixtures.SMTP()
        self.run_consume(queue, smtp)
        self.assertEqual(queue.data['jobs/test.json']['deferred_ids'], ['one'])
        self.assertEqual(queue.data['review/one.json']['status'], 'pending')
        self.assertNotIn(item['url'], self.bot.load_seen_urls())
        self.assertNotIn(item['url'], queue.data['previews/test.json']['html'])

    def test_same_round_recheck_requires_frozen_quote_and_routes_verified_peer(self):
        queue = self.mixed()
        job = queue.data['jobs/test.json']
        job['review_pass_version'] = 1
        result = queue.data['results/test.json']
        row = result['classifications'][1]
        for review in [None, {'blocker':'entity', 'quote':'다른 기사에서 가져온 문장'}]:
            if review:
                row['recheck'] = review
            with self.assertRaisesRegex(ValueError, '재판정'):
                bridge.validate(result, job, job['input'], job['body_manifest'])
        row['recheck'] = {'blocker':'body_unusable', 'quote':'직접 주체를 확인할 문단이 부족하다'}
        self.run_consume(queue, native_fixtures.SMTP())
        state = queue.data['jobs/test.json']
        self.assertEqual(state['body_deferred_ids'], ['held'])
        self.assertEqual(state['deferred_ids'], ['held'])
        self.assertNotIn('https://example.invalid/held', Path('seen_news.json').read_text())
        self.assertIn('https://example.invalid/test/one', queue.data['previews/test.json']['html'])
        self.assertEqual(state['status'], 'delivery_complete')

    def test_owner_exclusion_removes_only_requested_article_without_rewriting_result(self):
        queue, smtp = self.mixed(), native_fixtures.SMTP()
        self.seed_review(queue, 'held')
        original = copy.deepcopy(queue.data['results/test.json'])
        source = queue.source
        def excluded_source(path):
            raw, sha = source(path)
            if path == bridge.CONFIG:
                config = json.loads(raw)
                config['excluded_article_ids'] = ['held']
                raw = json.dumps(config).encode()
            return raw, sha
        queue.source = excluded_source
        self.run_consume(queue, smtp)
        job = queue.data['jobs/test.json']
        self.assertEqual(job['owner_excluded_ids'], ['held'])
        self.assertEqual(job['deferred_ids'], [])
        self.assertEqual(queue.data['results/test.json'], original)
        self.assertNotIn('https://example.invalid/held', self.bot.load_seen_urls())
        self.assertNotIn('held', [a['article_id'] for a in queue.data['filter-logs/test.json']['raw']])
        self.ack(queue)
        self.assertEqual(queue.data['review/index.json']['pending'], [])

    def test_exclusion_added_between_recipients_stops_remaining_smtp(self):
        queue, smtp = self.fixture(), native_fixtures.SMTP()
        excluded, source, send = [], queue.source, smtp.sendmail
        def live_source(path):
            raw, sha = source(path)
            if path == bridge.CONFIG:
                config = json.loads(raw)
                config['excluded_article_ids'] = list(excluded)
                raw = json.dumps(config).encode()
            return raw, sha
        def first_only(*args):
            result = send(*args)
            excluded.append('one')
            return result
        queue.source, smtp.sendmail = live_source, first_only
        with self.assertRaisesRegex(RuntimeError, '담당자가 제외한 기사'):
            self.run_consume(queue, smtp)
        self.assertEqual(len(smtp.calls), 1)
        self.assertEqual(list(queue.data['jobs/test.json']['receipts'].values()), ['accepted'])
        self.assertEqual(queue.data['jobs/test.json']['status'], 'uncertain')
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_expired_input_does_not_reopen_owner_excluded_review(self):
        queue = self.fixture()
        self.frozen(queue)
        self.seed_review(queue)
        queue.data['review/one.json'].update(status='resolved', resolution_kind='owner_excluded')
        queue.data['review/index.json']['pending'] = []
        original = copy.deepcopy(queue.data['review/one.json'])
        values, expected, resolutions = continuity.prepare_reviews(
            queue, queue.data['jobs/test.json'], {'one':None}, reason='expired_unprocessed')
        self.assertEqual((values, expected, resolutions), ({}, {}, {}))
        self.assertEqual(queue.data['review/one.json'], original)

    def ack(self, queue, smtp=None):
        queue.remote_seen = Path('seen_news.json').read_bytes()
        with patch.object(bridge.smtplib, 'SMTP_SSL', return_value=smtp or native_fixtures.SMTP()):
            bridge.acknowledge(queue, 'test')


    def replay(self, queue):
        job = self.frozen(queue)
        job['input']['snapshot_mode'] = 'review_replay'
        result = queue.data['results/test.json']
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        return job

    def crash_digest(self):
        from unittest.mock import Mock
        price = Mock(return_value='<div>MARKET_DIGEST_ONLY</div>')
        price.last_alerted_count = 99
        price.last_alerted_rbal = 9999
        price.last_alerted_big = (99, 9999)
        return price

    def test_review_no_positive_cannot_send_market_digest_to_groups(self):
        queue, smtp = self.fixture(), native_fixtures.SMTP()
        self.replay(queue)
        self.seed_review(queue)
        result = queue.data['results/test.json']
        result['classifications'][0]['relevant'] = False
        result['analyses'] = {}
        price = self.crash_digest()
        with patch.object(self.bot, 'build_price_alert_section', price), \
                patch.object(self.bot, 'load_competitor_notices') as notices:
            self.run_consume(queue, smtp)
        self.assertEqual([r[0] for r in smtp.calls], [['myboy3743@gmail.com']])
        price.assert_not_called()
        notices.assert_not_called()
        self.assertNotIn('MARKET_DIGEST_ONLY', queue.data['outbox/test.json']['html'])
        self.ack(queue)
        self.assertEqual(queue.data['review/one.json']['status'], 'resolved')
        self.assertEqual(queue.data['jobs/test.json']['status'], 'completed')

    def test_review_native_removes_all_cards_without_group_digest(self):
        queue, smtp = self.fixture(), native_fixtures.SMTP()
        self.replay(queue)
        price = self.crash_digest()
        with patch.object(self.bot, 'build_price_alert_section', price), \
                patch.object(self.bot, 'load_competitor_notices') as notices, \
                patch.object(self.bot, 'filter_articles_for_scope', return_value=[]):
            self.run_consume(queue, smtp)
        self.assertEqual([r[0] for r in smtp.calls], [['myboy3743@gmail.com']])
        price.assert_not_called()
        notices.assert_not_called()
        self.assertNotIn('MARKET_DIGEST_ONLY', queue.data['outbox/test.json']['html'])

    def test_review_verified_urgent_news_still_delivers_and_acks(self):
        queue, smtp = self.fixture(), native_fixtures.SMTP()
        self.replay(queue)
        self.seed_review(queue)
        price = self.crash_digest()
        with patch.object(self.bot, 'build_price_alert_section', price), \
                patch.object(self.bot, 'load_competitor_notices') as notices:
            self.run_consume(queue, smtp)
        self.assertEqual([r[0] for r in smtp.calls],
                         [['sender@example.invalid'], ['group@example.invalid'], ['cc@example.invalid']])
        self.assertIn('https://example.invalid/test/one', queue.data['outbox/test.json']['html'])
        price.assert_not_called()
        notices.assert_not_called()
        self.ack(queue)
        self.assertEqual(queue.data['review/one.json']['status'], 'resolved')
        self.assertTrue(queue.data['jobs/test.json']['ack'])

    def test_natural_no_news_market_alert_still_reaches_existing_groups(self):
        queue, smtp = self.fixture(), native_fixtures.SMTP()
        job = self.frozen(queue)
        job['input']['snapshot_mode'] = 'natural_collection'
        result = queue.data['results/test.json']
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        result['classifications'][0]['relevant'] = False
        result['analyses'] = {}
        price = self.crash_digest()
        with patch.object(self.bot, 'build_price_alert_section', price):
            self.run_consume(queue, smtp)
        self.assertEqual([r[0] for r in smtp.calls],
                         [['sender@example.invalid'], ['group@example.invalid'], ['cc@example.invalid']])
        price.assert_called()
        self.assertIn('MARKET_DIGEST_ONLY', queue.data['outbox/test.json']['html'])

    def test_review_loan_only_frozen_packet_is_rejected(self):
        queue = self.fixture()
        job = self.replay(queue)
        with self.assertRaisesRegex(RuntimeError, '검증 기사 없는 전체 발송 금지'):
            bridge.make_packet(job, queue.data['results/test.json'], 'market digest',
                               '<html><body>MARKET_DIGEST_ONLY</body></html>',
                               ['group@example.invalid'], False, Path('seen_news.json').read_text())


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
        self.assertEqual(len(queue.data['jobs/test.json']['receipts']), 3)
        self.assertEqual(queue.data['results/test.json'], original)
        self.assertNotIn('https://example.invalid/held', self.bot.load_seen_urls())
        self.assertFalse(queue.data['jobs/test.json']['analysis_complete'])
        html = queue.data['previews/test.json']['html']
        self.assertNotIn('확인 보류', html)
        self.assertIn('<table', html)
        self.ack(queue)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'completed')
        self.assertEqual(queue.data['review/held.json']['status'], 'pending')

    def test_missing_body_positive_is_held_without_blocking_verified_peer(self):
        queue, smtp = self.mixed(), native_fixtures.SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        item = job['input']['items'][-1]
        analysis = copy.deepcopy(result['analyses']['one'])
        analysis.update(body_status='missing', base_grade='참고', confidence=0.6,
                        fact_summary='[본문 미수집] 제목으로만 확인한 참고 사실.', customer_notice=None,
                        evidence=[{'source_segment':'title', 'source_hash':hashlib.sha256(item['title'].encode()).hexdigest(),
                                   'quote':item['title']}])
        result['analyses']['held'] = analysis
        result['classifications'][-1]['relevant'] = True
        job['body_manifest'][-1].update(body='', body_status='missing')
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        original = copy.deepcopy(result)
        with patch.object(bridge, 'notify_best_effort') as notify:
            self.run_consume(queue, smtp)
        notify.assert_not_called()
        self.assertEqual(len(smtp.calls), 3)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'delivery_complete')
        self.assertEqual(queue.data['jobs/test.json']['deferred_ids'], ['held'])
        self.assertEqual(queue.data['jobs/test.json']['body_deferred_ids'], ['held'])
        self.assertEqual(queue.data['review/index.json']['pending'], ['held'])
        self.assertNotIn(item['url'], self.bot.load_seen_urls())
        self.assertEqual(queue.data['results/test.json'], original)
        final = queue.data['filter-logs/test.json']['final']
        self.assertTrue(final)
        self.assertNotIn('held', [a['article_id'] for a in final])
        self.ack(queue)
        self.assertEqual(queue.data['jobs/test.json']['status'], 'completed')
        self.assertFalse(queue.data['jobs/test.json']['analysis_complete'])
        self.assertEqual(queue.data['review/held.json']['status'], 'pending')

    def test_analyst_unreadable_body_is_excluded_from_semantic_hold_status_count(self):
        queue, smtp = self.mixed(), native_fixtures.SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        item = job['input']['items'][0]
        result['analyses']['one'].update(body_status='unreadable', base_grade='참고', confidence=0.6,
            fact_summary='[본문 미수집] 제목에서만 확인한 사건.', customer_notice=None,
            evidence=[{'source_segment':'title', 'source_hash':hashlib.sha256(item['title'].encode()).hexdigest(),
                       'quote':item['title']}])
        with patch.object(bridge, 'notify_best_effort') as notify:
            self.run_consume(queue, smtp)
        notify.assert_not_called()  # The owner status already covers the semantic hold.
        self.assertEqual(len(smtp.calls), 1)
        preview = queue.data['previews/test.json']['html']
        self.assertIn('확인 보류 1건', preview)
        self.assertNotIn('확인 보류 2건', preview)
        self.assertEqual(queue.data['jobs/test.json']['body_deferred_ids'], ['one'])
        self.assertEqual(set(queue.data['review/index.json']['pending']), {'one', 'held'})

    def test_all_missing_body_keeps_routine_owner_status_without_error_alert(self):
        queue, smtp = self.fixture(), native_fixtures.SMTP()
        job = self.frozen(queue)
        result = queue.data['results/test.json']
        result['classifications'][0]['relevant'] = None
        result['analyses'] = {}
        job['body_manifest'][0].update(body='', body_status='missing')
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        with patch.object(bridge, 'notify_best_effort') as notify:
            self.run_consume(queue, smtp)
        notify.assert_not_called()
        self.assertEqual([r[0] for r in smtp.calls], [['myboy3743@gmail.com']])
        preview = queue.data['previews/test.json']['html']
        self.assertNotIn('확인 보류', preview)
        self.assertIn('본문 확인된 기사에서', preview)
        self.assertNotIn('리스크봇 오류', queue.data['outbox/test.json']['subject'])
        self.assertEqual(queue.data['review/index.json']['pending'], ['one'])
        self.assertNotIn(job['input']['items'][0]['url'], self.bot.load_seen_urls())
        self.ack(queue)
        self.assertFalse(queue.data['jobs/test.json']['analysis_complete'])

    def test_all_null_sends_owner_status_without_claiming_no_risk(self):
        queue, smtp = self.fixture(), native_fixtures.SMTP()
        self.frozen(queue)
        result = queue.data['results/test.json']
        result['classifications'][0]['relevant'] = None
        result['analyses'] = {}
        self.run_consume(queue, smtp)
        self.assertEqual([r[0] for r in smtp.calls], [['myboy3743@gmail.com']])
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
        self.assertNotIn('미확인', queue.data['previews/test.json']['html'])
        self.assertEqual(len(smtp.calls), 3)
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
        self.assertEqual(len(queue.data['jobs/test.json']['receipts']), 3)

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
        parent = memory_fixtures.job('regular', status='completed')
        parent.update(ack=True, history_saved=True, policy_hash='previous-policy',
                      created_at=now().isoformat(), expected_receipts=['recipient'],
                      receipts={'recipient':'accepted'})
        parent['input']['snapshot_mode'] = 'natural_collection'
        queue.data['jobs/regular.json'] = parent
        queue.data['index.json']['jobs'] = ['regular']
        item = {'article_id': 'held', 'title': '실제 사건 주체 확인 기사', 'desc': '보류 원문 설명',
                'description': '보류 원문 설명', 'url': 'https://example.invalid/held',
                'pubDate': (now()-timedelta(hours=1)).isoformat()}
        body = {'article_id': 'held', 'body': '본문에 확인 가능한 사실을 보존한다.',
                'body_status': 'verified', 'source_url': item['url']}
        job = {'job_id': 'old', 'input': {'items': [item]}, 'body_manifest': [body]}
        values, expected, _ = continuity.prepare_reviews(queue, job, {'held': None})
        queue.put_many(values, expected)
        self.bot.is_hard_excluded = lambda *a: (False, '')
        self.bot.fetch_article_body = lambda *a: ''
        return queue

    def test_owner_excluded_backlog_is_not_collected_again(self):
        queue = self.seed()
        source = queue.source
        def excluded_source(path):
            raw, sha = source(path)
            if path == bridge.CONFIG:
                config = json.loads(raw)
                config['excluded_article_ids'] = ['held']
                raw = json.dumps(config).encode()
            return raw, sha
        queue.source = excluded_source
        with patch.object(bridge, 'prune_terminal_inputs'):
            with patch('sys.stdout', io.StringIO()):
                bridge.collect({'job_ttl_minutes': 180}, queue, reviews_only=True, review_ids=['held'])
        self.assertNotIn('jobs/risk-production-hardening-test.json', queue.data)

    def test_regular_collection_leaves_backlog_for_post_delivery_without_mutation(self):
        queue = self.seed()
        before = copy.deepcopy(queue.data['review/held.json'])
        self.collect(queue)
        small = queue.data['jobs/risk-production-hardening-test.json']
        full = hydrate(queue, small)
        self.assertEqual(full['input']['items'], [])
        self.assertEqual(full['review_dispatch']['eligible_count'], 1)
        self.assertEqual(full['review_dispatch']['phase'], 'regular')
        self.assertEqual(queue.data['review/held.json'], before)
        self.assertIn('exposure_snapshot', small)

    def test_pending_same_id_in_fresh_search_is_deferred_before_body_fetch(self):
        queue = self.seed()
        record = queue.data['review/held.json']
        item = record['item']
        aid = hashlib.sha256((item['url']+'\0'+item['title']).encode()).hexdigest()[:24]
        item['article_id'] = record['article_id'] = record['body']['article_id'] = aid
        record['source_hash'] = digest({'item': item, 'body': record['body']})
        queue.data['review/'+aid+'.json'] = queue.data.pop('review/held.json')
        queue.data['review/index.json']['pending'] = [aid]
        self.bot.crawl_naver_news.return_value = [copy.deepcopy(item)]
        self.bot.fetch_article_body = lambda *a: self.fail('backlog body before delivery')
        self.collect(queue)
        full = hydrate(queue, queue.data['jobs/risk-production-hardening-test.json'])
        self.assertEqual(full['input']['items'], [])
        self.assertEqual(full['input']['raw_articles'], [])

    def test_expired_or_unknown_review_never_replays_and_original_is_preserved(self):
        for value in [(now()-timedelta(hours=12, seconds=1)).isoformat(),
                      (now()-timedelta(days=60)).isoformat(), '', 'bad', '2026-11-06T10:00:00']:
            with self.subTest(value=value):
                queue = self.seed()
                record = queue.data['review/held.json']
                record['item']['pubDate'] = value
                record['source_hash'] = digest({'item': record['item'], 'body': record['body']})
                queue.data['review/index.json'].pop('sources', None)  # Legacy catalog migration.
                record.pop('last_source', None)
                before = copy.deepcopy(record)
                continuity.review_catalog(queue, migrate=True)
                with patch('sys.stdout', io.StringIO()):
                    bridge.collect({'job_ttl_minutes': 180}, queue, reviews_only=True)
                self.assertEqual(queue.data['review/held.json'], before)
                self.assertEqual(queue.data['review/index.json']['pending'], [])

    def test_publication_age_boundary_and_timezones_do_not_use_first_seen(self):
        current = bridge.utc_time('2026-11-06T12:00:00Z')
        for value, expected in [('2026-11-06T00:00:00Z', 'eligible'),
                                ('Fri, 06 Nov 2026 09:00:00 +0900', 'eligible'),
                                ('2026-11-05T23:59:59Z', 'expired'),
                                ('2026-11-06T12:00:01Z', 'date_unconfirmed')]:
            self.assertEqual(continuity.review_age({'published_at': value, 'first_seen_at': current.isoformat()}, current), expected)

    def test_manual_review_collection_reads_only_backlog_without_new_search_or_smtp(self):
        queue = self.seed()
        self.bot.crawl_naver_news.side_effect = AssertionError('new search forbidden')
        self.bot.fetch_article_body = lambda *a: '공식 발표에서 사건의 직접 행위 주체와 발생 시각을 새롭게 확인했다.'
        with patch.object(bridge, 'prune_terminal_inputs'):
            bridge.collect({'job_ttl_minutes': 180}, queue, reviews_only=True, review_ids=['held'])
        full = hydrate(queue, queue.data['jobs/risk-production-hardening-test.json'])
        self.assertEqual([a['article_id'] for a in full['input']['items']], ['held'])
        self.assertEqual(full['input']['snapshot_mode'], 'review_replay')
        self.assertEqual(full['review_dispatch']['after_job_id'], 'regular')
        self.bot.crawl_naver_news.assert_not_called()
        self.assertEqual(queue.data['review/held.json']['status'], 'pending')
        self.assertEqual(queue.data['jobs/risk-production-hardening-test.json']['receipts'], {})

    def test_unchanged_review_does_not_create_analysis_round(self):
        for fetch in [lambda *a: '', lambda *a: '본문에 확인 가능한 사실을 보존한다.']:
            queue = self.seed()
            before = copy.deepcopy(queue.data)
            self.bot.fetch_article_body = fetch
            output = io.StringIO()
            with patch.object(bridge, 'prune_terminal_inputs'), patch('sys.stdout', output):
                bridge.collect({'job_ttl_minutes': 180}, queue, reviews_only=True, review_ids=['held'])
            self.assertEqual(json.loads(output.getvalue())['status'], 'no_change')
            self.assertEqual(queue.data, before)

    def test_changed_source_keeps_original_publication_deadline(self):
        queue = self.seed()
        original = queue.data['review/held.json']['item']
        self.bot.crawl_naver_news.return_value = [dict(original, title='수정된 제목', pubDate=now().isoformat())]
        self.bot.fetch_article_body = lambda *a: '기업의 공식 발표에서 새로운 사건 단계와 직접 주체를 확인했다.'
        self.collect(queue)
        job = hydrate(queue, queue.data['jobs/risk-production-hardening-test.json'])
        self.assertEqual(len(job['input']['items']), 1)
        self.assertTrue(job['input']['items'][0]['_review_replay'])
        self.assertEqual(job['input']['items'][0]['published_at'], original['pubDate'])

    def test_same_url_changed_title_does_not_reanalyze_identical_body(self):
        queue = self.seed()
        item = dict(queue.data['review/held.json']['item'], title='수정된 제목')
        self.bot.crawl_naver_news.return_value = [item]
        self.bot.fetch_article_body = lambda *a: queue.data['review/held.json']['body']['body']
        self.collect(queue)
        job = hydrate(queue, queue.data['jobs/risk-production-hardening-test.json'])
        self.assertEqual(job['input']['items'], [])
        self.assertEqual(queue.data['review/index.json']['pending'], ['held'])

    def test_normal_collection_reads_catalog_without_loading_originals(self):
        queue = self.seed()
        read = queue.get
        def guarded(path):
            if path == 'review/held.json':
                self.fail('normal collection loaded held source')
            return read(path)
        queue.get = guarded
        self.collect(queue)

    def test_cold_catalog_does_not_read_legacy_originals_during_regular_collection(self):
        queue = self.seed()
        queue.data['review/index.json'].pop('sources', None)
        read = queue.get
        def guarded(path):
            if path == 'review/held.json':
                self.fail('cold collection loaded legacy source')
            return read(path)
        queue.get = guarded
        self.collect(queue)

    def test_default_followup_only_resumes_unprocessed_and_reuses_saved_body(self):
        queue = self.seed()
        with patch.object(bridge, 'prune_terminal_inputs'), patch('sys.stdout', io.StringIO()):
            bridge.collect({'job_ttl_minutes': 180}, queue, reviews_only=True)
        self.assertNotIn('jobs/risk-production-hardening-test.json', queue.data)
        queue.data['review/index.json']['sources']['held']['analysis_state'] = 'unprocessed'
        queue.data['review/held.json']['analysis_state'] = 'unprocessed'
        self.bot.fetch_article_body = lambda *a: self.fail('first analysis refetched body')
        with patch.object(bridge, 'prune_terminal_inputs'):
            bridge.collect({'job_ttl_minutes': 180}, queue, reviews_only=True)
        job = hydrate(queue, queue.data['jobs/risk-production-hardening-test.json'])
        self.assertEqual([a['article_id'] for a in job['input']['items']], ['held'])
        self.assertEqual(job['body_manifest'][0], queue.data['review/held.json']['body'])

    def test_review_requires_completed_regular_history_ack_and_only_one_followup(self):
        for mutation in ('no-parent', 'no-ack', 'no-history', 'uncertain', 'missing-receipt',
                         'expired', 'already-collected'):
            with self.subTest(mutation=mutation):
                queue = self.seed()
                parent = queue.data['jobs/regular.json']
                if mutation == 'no-parent':
                    queue.data['index.json']['jobs'] = []
                elif mutation == 'no-ack':
                    parent['ack'] = False
                elif mutation == 'no-history':
                    parent['history_saved'] = False
                elif mutation == 'uncertain':
                    parent['receipts']['recipient'] = 'uncertain'
                elif mutation == 'missing-receipt':
                    parent['receipts'] = {}
                elif mutation == 'expired':
                    parent['status'] = 'expired'
                else:
                    previous = copy.deepcopy(parent)
                    previous.update(job_id='review-previous')
                    previous['input']['snapshot_mode'] = 'review_replay'
                    queue.data['jobs/review-previous.json'] = previous
                    queue.data['index.json']['jobs'].append('review-previous')
                with self.assertRaisesRegex(RuntimeError, '보류 후속'):
                    bridge.collect({'job_ttl_minutes': 180}, queue, reviews_only=True, review_ids=['held'])
                self.assertNotIn('jobs/risk-production-hardening-test.json', queue.data)

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

    def test_readable_sources_come_first_without_losing_missing_source_ids(self):
        queue = self.seed()
        saved = queue.data['review/held.json']['item']
        rows = [dict(saved, url='https://example.invalid/missing', title='미확보 사건 기사'),
                dict(saved, url='https://example.invalid/ready', title='확인된 사건 기사')]
        self.bot.crawl_naver_news.return_value = rows
        self.bot.fetch_article_body = lambda url, diagnostics=None: '원문에서 해당 기업의 실제 사건을 확인했다.' if url.endswith('/ready') else ''
        self.collect(queue)
        full = hydrate(queue, queue.data['jobs/risk-production-hardening-test.json'])
        items, bodies = full['input']['items'], full['body_manifest']
        self.assertEqual([a['url'] for a in items], [rows[1]['url'], rows[0]['url']])
        self.assertEqual([b['body_status'] for b in bodies], ['verified', 'missing'])
        self.assertEqual({a['article_id'] for a in items}, {b['article_id'] for b in bodies})
        self.assertEqual(len(full['input']['raw_articles']), 2)

    def test_one_body_fetch_error_does_not_interrupt_other_articles(self):
        queue = self.seed()
        item = queue.data['review/held.json']['item']
        self.bot.crawl_naver_news.return_value = [dict(item, url='https://example.invalid/'+name, title=name)
                                                 for name in ['error', 'ready']]
        def fetch(url, diagnostics):
            if url.endswith('/error'):
                raise TimeoutError('isolated article timeout')
            return '공식 발표로 확인한 기업의 실제 사건과 발생 시각을 전달한다.'
        self.bot.fetch_article_body = fetch
        with patch.object(bridge, 'record_errors'):
            self.collect(queue)
        job = hydrate(queue, queue.data['jobs/risk-production-hardening-test.json'])
        self.assertEqual(len(job['input']['items']), 2)
        self.assertEqual(sorted(b['body_status'] for b in job['body_manifest']), ['missing', 'verified'])

    def test_corrupt_source_is_not_read_on_normal_collection_but_blocks_review(self):
        queue = self.seed()
        queue.data['review/held.json']['body']['body'] = 'tampered'
        self.collect(queue)
        self.assertEqual(queue.data['jobs/risk-production-hardening-test.json']['status'], 'pending')
        queue = self.seed()
        queue.data['review/held.json']['body']['body'] = 'tampered'
        with self.assertRaisesRegex(RuntimeError, '영속 보류 원본'):
            bridge.collect({'job_ttl_minutes': 180}, queue, reviews_only=True, review_ids=['held'])
        self.assertNotIn('jobs/risk-production-hardening-test.json', queue.data)

    def test_optional_exclusion_reuse_failure_does_not_block_new_round(self):
        queue = self.seed()
        with patch.object(bridge, 'reusable_exclusions', side_effect=RuntimeError('optional read failed')), \
             patch.object(bridge, 'record_error'):
            self.collect(queue)
        job = queue.data['jobs/risk-production-hardening-test.json']
        self.assertEqual(job['status'], 'pending')
        self.assertEqual(job['receipts'], {})


class ExclusionReuse(unittest.TestCase):
    setUp = native_fixtures.Integration.setUp
    tearDown = native_fixtures.Integration.tearDown
    fixture = native_fixtures.Integration.fixture
    excluded = native_fixtures.ExclusionEvidence.excluded

    def pair(self):
        queue = self.excluded()
        old = queue.data['jobs/test.json']
        old.update(status='completed', created_at=now().isoformat())
        current = copy.deepcopy(old)
        current.update(job_id='next', status='pending')
        return queue, current

    def test_validated_unchanged_exclusion_is_copied_without_dropping_input(self):
        queue, current = self.pair()
        original = copy.deepcopy(current)
        rows = bridge.reusable_exclusions(queue, {'jobs': ['test']}, current)
        self.assertEqual(rows, queue.data['results/test.json']['classifications'])
        rows[0]['reason'] = 'local change'
        self.assertNotEqual(rows, queue.data['results/test.json']['classifications'])
        self.assertEqual(current, original)

    def test_v2_exclusion_reuse_does_not_require_removed_review_metadata(self):
        queue, current = self.pair()
        old = queue.data['jobs/test.json']
        old.update(review_pass_version=2, exclusion_review_version=2, action_quality_version=2)
        row = queue.data['results/test.json']['classifications'][0]
        row.pop('exclusion_review', None)
        row.pop('countercheck', None)
        rows = bridge.reusable_exclusions(queue, {'jobs': ['test']}, current)
        self.assertEqual(rows, [row])

    def test_changed_context_source_body_and_age_cannot_reuse(self):
        for mutation in ('policy', 'history', 'exposure', 'title', 'body', 'age'):
            with self.subTest(mutation=mutation):
                queue, current = self.pair()
                if mutation == 'policy':
                    current['policy_hash'] = 'different'
                elif mutation in ('history', 'exposure'):
                    current['input'][mutation+'_hash'] = 'different'
                elif mutation == 'title':
                    current['input']['items'][0]['title'] += ' 새로운 악화'
                elif mutation == 'body':
                    current['body_manifest'][0]['body'] += ' 새로운 악화'
                else:
                    queue.data['jobs/test.json']['created_at'] = (now()-timedelta(hours=12, seconds=1)).isoformat()
                self.assertEqual(bridge.reusable_exclusions(queue, {'jobs': ['test']}, current), [])

    def test_positive_or_pending_source_is_not_reused(self):
        queue = self.fixture()
        old = queue.data['jobs/test.json']
        old.update(status='completed', created_at=now().isoformat())
        self.assertEqual(bridge.reusable_exclusions(queue, {'jobs': ['test']}, copy.deepcopy(old)), [])
        old['status'] = 'pending'
        self.assertEqual(bridge.reusable_exclusions(queue, {'jobs': ['test']}, copy.deepcopy(old)), [])

    def test_invalid_previous_exclusion_is_not_trusted(self):
        queue, current = self.pair()
        queue.data['results/test.json']['classifications'][0]['exclusion_review']['evidence']['source_hash'] = '0'*64
        with self.assertRaisesRegex(ValueError, '해시'):
            bridge.reusable_exclusions(queue, {'jobs': ['test']}, current)


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


class ReviewReadConcurrency(unittest.TestCase):
    def fixture(self, count=12):
        queue = memory_fixtures.Memory()
        ids = ['review'+str(i) for i in range(count)]
        job = {'job_id': 'round', 'input': {'items': [
            {'article_id': aid, 'title': 'immutable source'} for aid in ids]},
            'body_manifest': [{'article_id': aid, 'body': 'immutable body'} for aid in ids]}
        values, expected, _ = continuity.prepare_reviews(queue, job, dict.fromkeys(ids))
        queue.put_many(values, expected)
        return queue, job, ids

    def concurrent_reads(self, queue):
        original = queue.get
        barrier, lock = threading.Barrier(6, timeout=3), threading.Lock()
        stats = {'active': 0, 'maximum': 0, 'calls': []}
        def get(path):
            if not path.startswith('review/') or path == continuity.REVIEW_INDEX:
                return original(path)
            with lock:
                stats['active'] += 1
                stats['maximum'] = max(stats['maximum'], stats['active'])
                stats['calls'].append(path)
            try:
                barrier.wait()
                return original(path)
            finally:
                with lock:
                    stats['active'] -= 1
        return get, stats

    def test_all_review_phases_bound_reads_preserve_order_and_original_shas(self):
        for phase in ('pending', 'prepare', 'finish'):
            with self.subTest(phase=phase):
                queue, job, ids = self.fixture()
                original = copy.deepcopy(queue.data)
                shas = {aid: queue.get('review/'+aid+'.json')[1] for aid in ids}
                get, stats = self.concurrent_reads(queue)
                with patch.object(queue, 'get', side_effect=get):
                    if phase == 'pending':
                        result = continuity.pending_reviews(queue)
                        self.assertEqual([r['article_id'] for r in result], ids)
                    elif phase == 'prepare':
                        _, expected, _ = continuity.prepare_reviews(queue, job, dict.fromkeys(ids))
                    else:
                        _, expected = continuity.finish_reviews(queue,
                            {aid: {'sha': shas[aid], 'relevant': False} for aid in ids}, job['job_id'])
                self.assertEqual(stats['maximum'], 6)
                self.assertEqual(len(stats['calls']), len(ids))
                self.assertEqual(len(set(stats['calls'])), len(ids))
                self.assertEqual(queue.data, original)
                if phase != 'pending':
                    self.assertEqual({aid: expected['review/'+aid+'.json'] for aid in ids}, shas)

    def test_prefetch_does_not_allow_changed_review_to_be_overwritten(self):
        queue, job, ids = self.fixture(2)
        values, expected, _ = continuity.prepare_reviews(queue, job, dict.fromkeys(ids))
        queue.data['review/'+ids[0]+'.json']['reason'] = 'concurrent owner update'
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            queue.put_many(values, expected)
        self.assertEqual(queue.data['review/'+ids[0]+'.json']['reason'], 'concurrent owner update')

    def test_missing_corrupt_or_failed_read_cannot_become_empty_pending(self):
        for failure in ('missing', 'corrupt', 'unavailable'):
            with self.subTest(failure=failure):
                queue, _, ids = self.fixture(2)
                path = 'review/'+ids[1]+'.json'
                if failure == 'missing':
                    del queue.data[path]
                elif failure == 'corrupt':
                    queue.data[path]['body']['body'] = 'changed source'
                original = queue.get
                def get(current):
                    if failure == 'unavailable' and current == path:
                        raise RuntimeError('GET unavailable')
                    return original(current)
                before = copy.deepcopy(queue.data)
                with patch.object(queue, 'get', side_effect=get), self.assertRaises(RuntimeError):
                    continuity.pending_reviews(queue)
                self.assertEqual(queue.data, before)


if __name__ == '__main__':
    unittest.main()
