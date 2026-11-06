"""Extraction failures and market validation: fixed sources, native paths, no network."""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from risk_body_quality import body_quality
from risk_result_validator import validate
from risk_queue import digest
from risk_shards import hydrate
from risk_owner_verification import ERROR_EMAIL
import risk_continuity as continuity
import risk_subscription as bridge
import test_risk_continuity as replay_cases
import test_risk_hardening as collection_cases
import test_risk_subscription as native_cases

CASES = json.loads((Path(__file__).parent / 'tests/fixtures/risk_body_deferred_excerpts.json').read_text())


class ExtractionQuality(unittest.TestCase):
    def test_structured_body_must_match_article_url_and_preserve_truncation(self):
        bot = bridge.import_bot()
        url = 'https://example.invalid/this-article'
        body = '회사는 생산을 일시 중단했다고 밝혔다. ' * 710
        nodes = [{'@type':'NewsArticle', 'url':url + '/other', 'articleBody':'다른 기사다.'},
                 {'@type':'NewsArticle', 'mainEntityOfPage':{'@id':url}, 'articleBody':body}]
        response = SimpleNamespace(headers={}, text='<script type="application/ld+json">' +
                                   json.dumps({'@graph':nodes}) + '</script>', raise_for_status=lambda: None)
        with patch.dict(os.environ, RISK_ENGINE='subscription'), patch.object(bot.requests.Session, 'get', return_value=response):
            diagnostics = {}
            self.assertEqual(bot.fetch_article_body(url, diagnostics), body[:12000])
            self.assertTrue(diagnostics['truncated'])
            self.assertEqual(bot.fetch_article_body(url + '/unrelated'), '')

    def test_observed_failure_excerpts_and_two_readable_sources(self):
        for row in CASES:
            with self.subTest(article_id=row['article_id'], title=row['title']):
                self.assertEqual(body_quality(row['body'])[0], row['expected_status'])

    def test_short_factual_prose_and_english_do_not_need_padding(self):
        for text in ('회사가 공장 가동을 중단했다.',
                     '회사는 사고 발생 이후 공장 가동을 중단했다고 밝혔다. (사진=연합뉴스)',
                     'The company has suspended production at its factory.'):
            self.assertEqual(body_quality(text)[0], 'verified')
        self.assertEqual(body_quality('')[0], 'missing')
        self.assertEqual(body_quality('홍길동 기자')[0], 'unreadable')
        self.assertEqual(body_quality('관련기사 회사는 파산을 신청했다.')[0], 'unreadable')
        self.assertEqual(body_quality('등록일 2026.10.26 ⓒ 기사 무단 전재를 금지합니다.')[0], 'unreadable')

    def test_http_latin1_default_uses_html_charset_for_utf8_and_cp949(self):
        bot = bridge.import_bot()
        body = '해당 기업은 사고 발생 이후 공장 가동을 중단했다고 밝혔다.'
        for charset in ('utf-8', 'cp949'):
            raw = ('<meta charset="' + charset + '"><article>' + body + '</article>').encode(charset)
            response = SimpleNamespace(content=raw, encoding='ISO-8859-1', headers={},
                                       text=raw.decode('latin1'), raise_for_status=lambda: None)
            with self.subTest(charset=charset), patch.dict(os.environ, RISK_ENGINE='subscription'), \
                    patch.object(bot.requests.Session, 'get', return_value=response):
                actual = bot.fetch_article_body('https://example.invalid/news')
                self.assertEqual(actual, body)
                self.assertEqual(body_quality(actual)[0], 'verified')

    def test_bad_first_container_does_not_hide_later_article(self):
        bot = bridge.import_bot()
        body = '해당 기업은 사고 발생 이후 공장 가동을 중단했다고 밝혔다.'
        response = SimpleNamespace(headers={}, text='<div id="newsView">홍길동 기자</div>'
                                   '<article>' + body + '</article><footer>관련기사</footer>',
                                   raise_for_status=lambda: None)
        with patch.dict(os.environ, RISK_ENGINE='subscription'), \
                patch.object(bot.requests.Session, 'get', return_value=response):
            self.assertEqual(bot.fetch_article_body('https://example.invalid/news'), body)


class CollectionQuality(unittest.TestCase):
    setUp = collection_cases.Collection.setUp
    seed = replay_cases.ReplayCollection.seed

    def collect(self, queue):
        # Extraction invariants now run in the post-delivery review route.
        bridge.collect({'job_ttl_minutes': 180}, queue, reviews_only=True, review_ids=queue.data['review/index.json']['pending'])

    def test_older_snapshot_without_status_is_preserved_without_inventing_verified(self):
        queue = collection_cases.Memory()
        body = {'article_id': 'held', 'body': '이전 회차의 원본 내용'}
        job = {'job_id': 'old', 'input': {'items': [{'article_id': 'held'}]}, 'body_manifest': [body]}
        values, _, _ = continuity.prepare_reviews(queue, job, {'held': None})
        self.assertEqual(values['review/held.json']['body'], body)
        self.assertEqual(values['review/held.json']['last_body_status'], 'unconfirmed')

    def test_failed_fetch_does_not_reanalyze_saved_failures_or_delete_originals(self):
        queue = self.seed()
        queue.data.pop('review/held.json')
        queue.data['review/index.json']['pending'] = []
        queue.data['review/index.json']['sources'] = {}
        items = [dict(article_id=r['article_id'], title=r['title'], url=r['url'],
                      desc='', description='', pubDate=(bridge.now()-bridge.timedelta(hours=1)).isoformat()) for r in CASES]
        bodies = [dict(article_id=r['article_id'], body=r['body'], body_status='verified',
                       source_url=r['url']) for r in CASES]
        old = {'job_id': 'old', 'input': {'items': items}, 'body_manifest': bodies}
        values, expected, _ = continuity.prepare_reviews(queue, old, {r['article_id']: None for r in CASES})
        queue.put_many(values, expected)
        originals = {p: copy.deepcopy(v) for p, v in queue.data.items() if p.startswith('review/')}
        self.bot.is_hard_excluded = lambda *a: (False, '')
        self.bot.fetch_article_body = lambda *a: ''
        with patch.object(bridge, 'policy', return_value={'action_quality_version': 1}):
            self.collect(queue)
        self.assertNotIn('jobs/risk-production-hardening-test.json', queue.data)
        self.assertEqual({p: queue.data[p] for p in originals}, originals)
        for row in CASES:
            self.assertEqual(body_quality(row['body'])[0], row['expected_status'])

    def test_new_menu_does_not_resurrect_or_reanalyze_old_prose(self):
        for saved, expected_status in [('회사가 가동 중단 사실을 공시했다.', 'verified'),
                                       ('홍길동 기자', 'unreadable')]:
            queue = self.seed()
            record = queue.data['review/held.json']
            record['body']['body'] = saved
            record['source_hash'] = digest({'item': record['item'], 'body': record['body']})
            self.bot.fetch_article_body = lambda *a: '관련기사 메뉴'
            self.collect(queue)
            self.assertNotIn('jobs/risk-production-hardening-test.json', queue.data)
            self.assertEqual(queue.data['review/held.json']['body']['body'], saved)

    def test_saved_unreadable_prose_is_not_promoted_without_a_new_verified_fetch(self):
        queue = self.seed()
        record = queue.data['review/held.json']
        record['body'].update(body='이 문단은 다른 사건에 관한 내용이라고 확인했다.', body_status='unreadable')
        record['source_hash'] = digest({'item': record['item'], 'body': record['body']})
        self.collect(queue)
        self.assertNotIn('jobs/risk-production-hardening-test.json', queue.data)
        self.assertEqual(queue.data['review/held.json']['body']['body_status'], 'unreadable')


class NativeValidation(unittest.TestCase):
    setUp = native_cases.Integration.setUp
    tearDown = native_cases.Integration.tearDown
    fixture = native_cases.Integration.fixture
    run_consume = native_cases.Integration.run_consume

    def market(self, title, body):
        queue = self.fixture()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        item = job['input']['items'][0]
        item.update(title=title, description='', desc='')
        job['body_manifest'][0].update(body=body)
        analysis = result['analyses']['one']
        analysis.update(entity='', entities=[], event_type='기타리스크', base_grade='주의',
                        fact_summary=title, action='거래일과 지수 변동을 확인한다.', customer_notice=None,
                        evidence=[{'source_segment': 'body', 'source_hash': hashlib.sha256(body.encode()).hexdigest(),
                                   'quote': body[:160]}])
        analysis['judgment'].update(당사연관='무관', 핵심사건='시장 지수 하락')
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        return queue

    def test_observed_market_source_and_decline_variants_pass_contract(self):
        row = next(r for r in CASES if r['article_id'] == 'fd89969abf0c40757c39b1d1')
        for title, body in [(row['title'], row['body']),
                            ('급락한 코스피', '코스피는 전 거래일보다 하락했다.'),
                            ('국내 지수 하락', '코스닥은\n전 거래일보다 낮은 수준으로 내려 마감했다.')]:
            with self.subTest(title=title):
                q = self.market(title, body)
                j = q.data['jobs/test.json']
                self.assertTrue(validate(q.data['results/test.json'], j, j['input'], j['body_manifest'])['consume_ready'])

    def test_company_fall_or_separate_sentence_does_not_make_index_event(self):
        for title, body in [('기업 실적 발표', '예시기업 주가가 급락했다.'),
                            ('지수와 종목 동향', '코스피는 상승했다. 예시기업 주가가 급락했다.'),
                            ('지수와 종목 동향', '코스피는 보합으로 마감했으나 예시기업 주가는 급락했다.'),
                            ('지수와 종목 동향', '코스피는 보합, 예시기업 주가는 하락했다.'),
                            ('지수와 종목 동향', '예시기업 급락에도 코스피는 보합으로 마감했다.')]:
            with self.subTest(body=body):
                q = self.market(title, body)
                j = q.data['jobs/test.json']
                with self.assertRaisesRegex(ValueError, '빈 주체'):
                    validate(q.data['results/test.json'], j, j['input'], j['body_manifest'])

    def test_forged_verified_menu_in_new_job_is_rejected_before_smtp(self):
        queue, smtp = self.fixture(), native_cases.SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        job['body_quality_version'] = 1
        job['body_manifest'][0]['body'] = '홍길동 기자'
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        with self.assertRaisesRegex(ValueError, '본문 품질 미검증'):
            self.run_consume(queue, smtp)
        self.assertEqual(smtp.calls, [])
        self.assertEqual(Path('seen_news.json').read_bytes(), self.before)

    def test_menu_recorded_as_verified_can_be_held_without_blocking_verified_peer(self):
        queue, smtp = self.fixture(), native_cases.SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        job.update(body_quality_version=1, review_pass_version=1)
        item = copy.deepcopy(job['input']['items'][0])
        item.update(article_id='menu', url='https://example.invalid/menu')
        job['input']['items'].append(item)
        job['input']['raw_articles'] = copy.deepcopy(job['input']['items'])
        menu = '관련기사 메뉴 홍길동 기자'
        job['body_manifest'].append({'article_id':'menu', 'body':menu,
                                    'body_status':'verified', 'source_url':item['url']})
        result['classifications'].append({'article_id':'menu', 'relevant':None,
            'reason':'수집된 텍스트가 실제 기사 본문이 아닌 메뉴임을 확인했다.',
            'recheck':{'blocker':'body_unusable', 'quote':menu}})
        job['input_hash'] = result['input_hash'] = digest(job['input'])
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        queue.data.update(continuity.freeze_exposure(job, Path('exposure_data.csv').read_bytes(), bridge.now().isoformat()))
        original_body = copy.deepcopy(job['body_manifest'])
        hold = result['classifications'][-1]
        for recheck in ({'blocker':'entity', 'quote':menu},
                        {'blocker':'body_unusable', 'quote':'다른 기사에서 가져온 인용문'}):
            hold['recheck'] = recheck
            with self.assertRaises(ValueError):
                validate(result, job, job['input'], job['body_manifest'])
        hold['recheck'] = {'blocker':'body_unusable', 'quote':menu}
        self.run_consume(queue, smtp)
        self.assertEqual(queue.data['jobs/test.json']['deferred_ids'], ['menu'])
        document = queue.data['previews/test.json']['html']
        self.assertIn(job['input']['items'][0]['url'], document)
        self.assertNotIn(item['url'], document)
        self.assertNotIn(item['url'], self.bot.load_seen_urls())
        self.assertEqual(queue.data['review/menu.json']['body'], original_body[1])

    def test_date_conflict_remains_pending_and_records_specific_reason_without_seen(self):
        queue, smtp = self.fixture(), native_cases.SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        queue.data.update(continuity.freeze_exposure(job, Path('exposure_data.csv').read_bytes(), bridge.now().isoformat()))
        row = next(r for r in CASES if r['article_id'] == '88682231ceabb38e1b291163')
        job['body_manifest'][0]['body'] = row['body']
        job['body_quality_version'] = 1
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        result['classifications'][0].update(relevant=None, reason=row['classification_reason'])
        result['analyses'] = {}
        self.run_consume(queue, smtp)
        record = queue.data['review/one.json']
        self.assertEqual(record['status'], 'pending')
        self.assertEqual(record['last_review_reason'], row['classification_reason'])
        self.assertEqual(record['last_body_status'], 'verified')
        self.assertEqual(queue.data['jobs/test.json']['body_deferred_ids'], [])
        self.assertNotIn(job['input']['items'][0]['url'], self.bot.load_seen_urls())
        self.assertEqual([call[0] for call in smtp.calls], [[ERROR_EMAIL]])
        self.assertFalse(queue.data['jobs/test.json']['analysis_complete'])

    def test_bad_body_is_deferred_as_body_failure_and_never_sent_as_an_article(self):
        queue, smtp = self.fixture(), native_cases.SMTP()
        job, result = queue.data['jobs/test.json'], queue.data['results/test.json']
        queue.data.update(continuity.freeze_exposure(job, Path('exposure_data.csv').read_bytes(), bridge.now().isoformat()))
        job['body_quality_version'] = 1
        job['body_manifest'][0].update(body='홍길동 기자', body_status='unreadable',
                                       body_quality_reason='body_no_article_prose')
        job['body_manifest_hash'] = result['body_manifest_hash'] = digest(job['body_manifest'])
        result['classifications'][0].update(relevant=None, reason='기자명만 확보되어 실제 사건 본문 확인 보류')
        result['analyses'] = {}
        self.run_consume(queue, smtp)
        self.assertEqual(queue.data['jobs/test.json']['body_deferred_ids'], ['one'])
        self.assertEqual(queue.data['review/one.json']['last_body_quality_reason'], 'body_no_article_prose')
        self.assertNotIn(job['input']['items'][0]['url'], self.bot.load_seen_urls())
        self.assertEqual([call[0] for call in smtp.calls], [[ERROR_EMAIL]])
        self.assertNotIn('검토 보류', queue.data['previews/test.json']['html'])


if __name__ == '__main__':
    unittest.main()
