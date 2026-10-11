"""공지 파서·부분 실패 회귀. 공개 응답 fixture와 mock만 사용하며 네트워크/메일 호출 없음."""
import csv
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests
import broker_notices as b
from notice_filter import NOTICE_KEYWORDS, matches_notice_title

FIXTURES = Path(__file__).parent / 'tests/fixtures/broker_notices'


def response(kind, *, status=200, body=None):
    res = requests.Response()
    res.status_code = status
    res.url = next(x['url'] for x in b.BROKERS if x['type'] == kind)
    if body is None:
        suffix = '.html' if kind in ('miraeasset', 'nhqv') else '.json'
        text = (FIXTURES / (kind + suffix)).read_text(encoding='utf-8')
        res._content = text.encode('euc-kr' if suffix == '.html' else 'utf-8')
    else:
        res._content = body.encode('utf-8')
    res.encoding = 'utf-8'
    return res


class BrokerNoticeTests(unittest.TestCase):
    def setUp(self):
        self.brokers = {x['type']: x for x in b.BROKERS}
        self.io_stack = ExitStack()
        self.addCleanup(self.io_stack.close)
        self.guards = [self.io_stack.enter_context(patch(target, side_effect=AssertionError(
            'Unexpected external I/O in offline test: ' + target))) for target in (
                'requests.sessions.Session.request', 'smtplib.SMTP', 'smtplib.SMTP_SSL',
                'socket.create_connection', 'socket.socket.connect')]

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()

    def test_public_html_lists_and_real_detail_links(self):
        for kind, count, title, key, value in [
            ('miraeasset', 10, '㈜엘리스그룹 기업공개 공모 안내', 'messageId', '2343188'),
            ('nhqv', 12, '홈페이지 내 국내주식 등 거래서비스 종료 안내', 'main_no', '12818'),
        ]:
            with self.subTest(kind=kind):
                items = b.parse_notices(response(kind), self.brokers[kind])
                self.assertEqual(len(items), count)
                self.assertEqual(items[0]['title'], title)
                self.assertEqual(parse_qs(urlparse(items[0]['url']).query)[key], [value])
                self.assertTrue(all(set(x) == {'date', 'company', 'title', 'url'} for x in items))
                self.assertTrue(all(x['url'].startswith('https://') for x in items))
                self.assertFalse(any('.pdf' in x['title'] for x in items))

    def test_public_json_lists_and_case_mixed_notice_types(self):
        for kind, count, title, key, value in [
            ('samsung', 10, '㈜엘리스그룹 코스닥시장 상장 공모 안내', 'MenuSeqNo', '24571'),
            ('toss', 20, '국내/해외 상장 단일종목 레버리지 상품 모의거래 이수 의무화 안내', 'id', '23890'),
        ]:
            with self.subTest(kind=kind):
                items = b.parse_notices(response(kind), self.brokers[kind])
                self.assertEqual(len(items), count)
                self.assertEqual(items[0]['title'], title)
                self.assertEqual(parse_qs(urlparse(items[0]['url']).query)[key], [value])
        self.assertIn('[국내] 종목등급 및 증거금률 변경 안내', [x['title'] for x in items])

    def test_samsung_fixed_rows_are_included_once(self):
        res = response('samsung')
        data = res.json()
        data['fixlist'] = [data['list'][0]]
        res._content = json.dumps(data).encode()
        self.assertEqual(len(b.parse_notices(res, self.brokers['samsung'])), 10)

    def test_explicit_zero_json_is_empty(self):
        for kind, data in [('samsung', {'list': [], 'fixlist': [], 'totalCount': 0}),
                           ('toss', {'result': {'list': [], 'pagingParam': {'totalRowSize': 0}}})]:
            self.assertEqual(b.parse_notices(response(kind, body=json.dumps(data)), self.brokers[kind]), [])

    def test_unexpected_structure_is_error_not_zero(self):
        for kind in ('miraeasset', 'nhqv', 'samsung', 'toss'):
            with self.subTest(kind=kind):
                state = {}
                with patch.object(b.requests, 'get', return_value=response(kind, body='<html>로그인</html>')):
                    self.assertEqual(b.crawl_broker(self.brokers[kind], state), [])
                self.assertEqual(state['status'], 'error')
                self.assertEqual(state['http_status'], 200)

    def test_nonempty_total_with_zero_rows_is_error(self):
        res = response('samsung', body='{"list":[],"fixlist":[],"totalCount":10}')
        with self.assertRaises(ValueError):
            b.parse_notices(res, self.brokers['samsung'])

    def test_malformed_html_detail_link_is_error(self):
        for kind in ('miraeasset', 'nhqv'):
            res = response(kind)
            res._content = res.content.replace(b'view(', b'changed(').replace(b'viewUp(', b'changed(')
            with self.assertRaises(ValueError):
                b.parse_notices(res, self.brokers[kind])

    def test_http_404_and_timeout_are_distinct(self):
        for effect, expected, code in [(response('samsung', status=404), 'HTTPError', 404),
                                       (requests.Timeout('timeout'), 'Timeout', None)]:
            state = {}
            args = {'side_effect': effect} if isinstance(effect, Exception) else {'return_value': effect}
            with patch.object(b.requests, 'get', **args):
                self.assertEqual(b.crawl_broker(self.brokers['samsung'], state), [])
            self.assertEqual((state['status'], state['error_type'], state['http_status']), ('error', expected, code))

    def test_legacy_paths_and_parser_are_preserved(self):
        urls = {'kb': 'https://www.kbsec.com/go.able?linkcd=s10503',
                'shinhan': 'https://www.shinhaninvest.com/siw/customer-service/notice/list.do',
                'kiwoom': 'https://www.kiwoom.com/h/customer/board/VNoticeTypeHView'}
        for kind, url in urls.items():
            self.assertEqual(self.brokers[kind]['url'], url)
            res = response(kind, body='<table><tbody><tr><td><a href="/notice/1">기존 공지사항 안내</a></td></tr></tbody></table>')
            self.assertEqual(b.parse_notices(res, self.brokers[kind]), b.parse_generic(res.text, url, self.brokers[kind]['company']))

    def test_csv_schema_and_existing_dedup_unchanged(self):
        item = {'date': b.datetime.now(b.KST).strftime('%Y-%m-%d'), 'company': '삼성증권', 'title': '신용 테스트 공지', 'url': 'https://example.com/1'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, 'DATA_DIR', tmp):
            b.save_csv(item['company'], [item])
            b.save_csv(item['company'], [dict(item, url='https://example.com/2')])
            with open(Path(tmp)/'삼성증권.csv', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ['date', 'company', 'title', 'url'])
                self.assertEqual(list(reader), [item])

    def test_exact_or_keywords_and_title_normalization(self):
        self.assertEqual(NOTICE_KEYWORDS, ('신용', '대출', '오류'))
        for title in ('신용 안내', '담보대출 조건 변경', '주문오류 안내',
                      '한국신용정보원 안내', '신용·대출·오류 안내',
                      '일반 행사 및 대출 변경 안내', '  신 용\n거래 안내  ',
                      '대&nbsp;출 안내', '오\t류 정정',
                      '전산 &lt;Error 대출&gt; 수정', 'API<Error 오류>안내',
                      '&#50724;&#47448; 정정'):
            with self.subTest(title=title):
                self.assertTrue(matches_notice_title(title))
        for title in (None, '', '  ', 0, 12, {}, '공모주 청약 안내',
                      '증거금률 변경', '담보비율 변경', '전산장애 복구 안내',
                      '한도 축소', '반대매매 급증', '오-류 안내'):
            with self.subTest(title=title):
                self.assertFalse(matches_notice_title(title))

    def test_legacy_html_extracts_title_text_before_filtering(self):
        html = '<tbody><tr><td><a href="/1"><b>오</b>류 정정 안내</a></td></tr>' \
               '<tr><td><a href="/2" title="신용">일반 행사 안내</a></td></tr>' \
               '<tr><td><a href="/3">대&nbsp;출 조건 안내</a></td></tr></tbody>'
        for kind in ('kb', 'shinhan', 'kiwoom'):
            state = {}
            with patch.object(b.requests, 'get', return_value=response(kind, body=html)):
                rows = b.crawl_broker(self.brokers[kind], state)
            self.assertEqual([r['title'] for r in rows], ['오류 정정 안내', '대\xa0출 조건 안내'])
            self.assertEqual((state['count'], state['retained_count']), (3, 2))

    def test_shared_crawl_filter_is_applied_to_all_seven_feeds(self):
        titles = ['신용융자 안내', '담보대출 안내', '주문오류 정정', '일반 이벤트',
                  '담보 변경', '전산장애 안내', None, '']
        for broker in b.BROKERS:
            with self.subTest(company=broker['company']):
                rows = [dict(date='2026-10-08', company=broker['company'], title=t,
                             url=f'https://example.invalid/{i}') for i, t in enumerate(titles)]
                state = {}
                with patch.object(b.requests, 'get', return_value=response(broker['type'], body='')), \
                     patch.object(b, 'parse_notices', return_value=rows):
                    result = b.crawl_broker(broker, state)
                self.assertEqual(result, rows[:3])
                self.assertEqual((state['status'], state['count'], state['retained_count']),
                                 ('ok', 8, 3))

    def test_public_fixtures_raw_and_retained_counts(self):
        for kind, raw_count, retained_count in [('miraeasset', 10, 3), ('samsung', 10, 1),
                                               ('nhqv', 12, 1), ('toss', 20, 1)]:
            with self.subTest(kind=kind):
                state = {}
                with patch.object(b.requests, 'get', return_value=response(kind)):
                    items = b.crawl_broker(self.brokers[kind], state)
                self.assertEqual((state['status'], state['count'], state['retained_count']),
                                 ('ok', raw_count, retained_count))
                self.assertEqual(len(items), retained_count)

    def test_valid_nonmatching_lists_stay_healthy(self):
        for broker in b.BROKERS:
            with self.subTest(company=broker['company']):
                rows = [dict(title='이벤트 공지', body='신용 대출 오류', company=broker['company'])]
                state = {}
                with patch.object(b.requests, 'get', return_value=response(broker['type'], body='')), \
                     patch.object(b, 'parse_notices', return_value=rows):
                    self.assertEqual(b.crawl_broker(broker, state), [])
                self.assertEqual((state['status'], state['count'], state['retained_count']),
                                 ('ok', 1, 0))

    def test_null_json_titles_remain_schema_errors(self):
        for kind in ('samsung', 'toss'):
            data = response(kind).json()
            row = data['list'][0] if kind == 'samsung' else data['result']['list'][0]
            row['ntcTitle1' if kind == 'samsung' else 'title'] = None
            state = {}
            with patch.object(b.requests, 'get', return_value=response(kind, body=json.dumps(data))):
                self.assertEqual(b.crawl_broker(self.brokers[kind], state), [])
            self.assertEqual(state['status'], 'error')
            self.assertEqual(state['error_type'], 'ValueError')

    def write_rows(self, path, rows):
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['date', 'company', 'title', 'url'])
            writer.writeheader()
            writer.writerows(rows)

    def test_csv_filters_new_and_existing_even_without_new_hits(self):
        today = b.datetime.now(b.KST).strftime('%Y-%m-%d')
        old = (b.datetime.now(b.KST) - b.timedelta(days=31)).strftime('%Y-%m-%d')
        base = dict(date=today, company='삼성증권', title='신용융자 안내', url='https://example.invalid/1')
        irrelevant = dict(base, title='일반 이벤트')
        matching = dict(base, title='시스템 오류 정정', url='https://example.invalid/2')
        for incoming in ([], [irrelevant], [base], [irrelevant, matching]):
            with self.subTest(incoming=incoming), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(b, 'DATA_DIR', tmp):
                path = Path(tmp)/'삼성증권.csv'
                self.write_rows(path, [base, irrelevant, dict(base, title='대출 만료', date=old)])
                b.save_csv('삼성증권', incoming)
                expected = [base] + ([matching] if matching in incoming else [])
                with open(path, encoding='utf-8-sig') as f:
                    self.assertEqual(list(csv.DictReader(f)), expected)
                before = path.read_bytes()
                b.save_csv('삼성증권', [])
                self.assertEqual(path.read_bytes(), before)

    def test_expired_matching_title_is_refreshed_on_recollection(self):
        today = b.datetime.now(b.KST).strftime('%Y-%m-%d')
        cutoff = (b.datetime.now(b.KST) - b.timedelta(days=30)).strftime('%Y-%m-%d')
        old = (b.datetime.now(b.KST) - b.timedelta(days=31)).strftime('%Y-%m-%d')
        base = dict(date=today, company='삼성증권', title='신용융자 안내', url='https://example.invalid/1')
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, 'DATA_DIR', tmp):
            path = Path(tmp)/'삼성증권.csv'
            boundary = dict(base, date=cutoff, title='대출 조건 안내')
            self.write_rows(path, [dict(base, date=old), boundary])
            b.save_csv('삼성증권', [base, dict(boundary, date=today)])
            with open(path, encoding='utf-8-sig') as f:
                self.assertEqual(list(csv.DictReader(f)), [boundary, base])

    def test_all_nonmatching_csv_becomes_header_only_on_healthy_save(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, 'DATA_DIR', tmp):
            path = Path(tmp)/'삼성증권.csv'
            self.write_rows(path, [dict(date='2026-10-08', company='삼성증권',
                                       title='일반 공지', url='https://example.invalid/1')])
            b.save_csv('삼성증권', [])
            with open(path, encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ['date', 'company', 'title', 'url'])
                self.assertEqual(list(reader), [])

    def test_main_cleans_healthy_no_match_but_preserves_failed_source_csv(self):
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                Path('data').mkdir()
                today = b.datetime.now(b.KST).strftime('%Y-%m-%d')
                for company in ('KB증권', '삼성증권'):
                    self.write_rows(Path('data')/(company + '.csv'), [dict(
                        date=today, company=company, title='일반 이벤트', url='https://example.invalid/1')])
                failed_path = Path('data/삼성증권.csv')
                before = failed_path.read_bytes()
                def get(url, **kwargs):
                    kind = next(x['type'] for x in b.BROKERS if x['url'] == url)
                    return response(kind, status=404, body='Not Found') if kind == 'samsung' else response(kind, body='')
                with patch.object(b.requests, 'get', side_effect=get), \
                     patch.object(b, 'parse_notices', return_value=[{'title': '일반 이벤트'}]), \
                     patch.object(b.time, 'sleep'), patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}):
                    self.assertEqual(b.main(), 1)
                self.assertEqual(failed_path.read_bytes(), before)
                with open('data/KB증권.csv', encoding='utf-8-sig') as f:
                    self.assertEqual(list(csv.DictReader(f)), [])
                report = json.loads(Path('broker_notices_status.json').read_text())
                self.assertEqual(report['failed_sources'], 1)
                self.assertEqual(report['sources'][3]['status'], 'ok')
                self.assertEqual(report['sources'][3]['retained_count'], 0)
            finally:
                os.chdir(old)

    def test_unreadable_existing_csv_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, 'DATA_DIR', tmp):
            path = Path(tmp)/'삼성증권.csv'
            path.write_bytes(b'\xff\xfe\x00')
            with self.assertRaises(UnicodeDecodeError):
                b.save_csv('삼성증권', [])
            self.assertEqual(path.read_bytes(), b'\xff\xfe\x00')

    def test_output_uses_same_title_filter_on_legacy_csvs(self):
        # Import the actual monitor with synthetic settings, without repository maps.
        env = {k: 'offline@example.invalid' for k in (
            'EMAIL_SENDER', 'EMAIL_PASSWORD', 'EMAIL_RECEIVER', 'ANTHROPIC_API_KEY',
            'NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET')}
        spec = importlib.util.spec_from_file_location('_offline_notice_monitor',
                                                     Path(__file__).with_name('naver_news_monitor.py'))
        monitor = importlib.util.module_from_spec(spec)
        real_exists = os.path.exists
        with patch.dict(os.environ, env), patch('os.path.exists', side_effect=lambda p:
                False if os.path.basename(p) in ('group_map.json', 'ticker_map.json') else real_exists(p)):
            spec.loader.exec_module(monitor)
        today = b.datetime.now(b.KST).strftime('%Y-%m-%d')
        yesterday = (b.datetime.now(b.KST) - b.timedelta(days=1)).strftime('%Y-%m-%d')
        stale = (b.datetime.now(b.KST) - b.timedelta(days=2)).strftime('%Y-%m-%d')
        base = dict(date=today, company='삼성증권', title='신용 안내', url='https://example.invalid/1')
        good = [base, dict(base, title='담보대출 안내', date=yesterday),
                dict(base, title='주문 오류 안내'), dict(base, title='신 용 거래 안내')]
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                Path('data').mkdir()
                bad = [dict(base, title=t) for t in ('이벤트 안내', '증거금률 변경', '담보 변경', '전산장애', None)]
                self.write_rows('data/legacy.csv', good + bad + [dict(base, title='대출 오래된 공지', date=stale)])
                self.write_rows('data/삼성증권.csv', [base])
                self.write_rows('data/broker_notices_merged.csv', [dict(base, title='신용 중복 합본')])
                notices = monitor.load_competitor_notices()
                self.assertEqual(notices, good)
                html = monitor.build_competitor_html(notices, today)
                self.assertIn('경쟁사 신용·대출·오류 공지', html)
                self.assertIn('주문 오류 안내', html)
                self.assertNotIn('증거금률 변경', html)
                self.assertNotIn('대출 오래된 공지', html)
            finally:
                os.chdir(old)

    def run_main(self, errors):
        def get(url, **kwargs):
            kind = next(x['type'] for x in b.BROKERS if x['url'] == url)
            if kind in errors:
                return response(kind, status=404, body='Not Found')
            if kind in ('kb', 'shinhan', 'kiwoom'):
                return response(kind, body='<tbody></tbody>')
            return response(kind)
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                summary = str(Path(tmp)/'summary.md')
                with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': summary}), patch.object(b.requests, 'get', side_effect=get), patch.object(b.time, 'sleep'), patch.object(b, 'save_csv') as save:
                    rc = b.main()
                return rc, json.loads(Path('broker_notices_status.json').read_text()), Path(summary).read_text(), save.call_count
            finally:
                os.chdir(old)

    def test_four_404s_are_degraded_after_all_seven_attempts(self):
        rc, report, summary, saved = self.run_main({'miraeasset', 'samsung', 'nhqv', 'toss'})
        self.assertEqual((rc, report['status'], report['failed_sources'], report['total_sources']), (1, 'degraded', 4, 7))
        self.assertEqual(len(report['sources']), 7)
        self.assertIn('degraded', summary)
        self.assertIn('404', summary)
        self.assertEqual(saved, 3)  # Healthy empty feeds may clean old nonmatching rows.

    def test_single_failure_preserves_successful_sources(self):
        rc, report, _, saved = self.run_main({'miraeasset'})
        self.assertEqual((rc, report['failed_sources'], saved), (1, 1, 6))

    def test_healthy_run_and_legacy_empty_are_distinguished(self):
        rc, report, summary, saved = self.run_main(set())
        self.assertEqual((rc, report['status'], saved), (0, 'success', 7))
        self.assertEqual(report['sources'][5]['status'], 'empty')
        self.assertEqual(report['sources'][5]['parser'], 'legacy_generic')
        self.assertIn('| 원본 | 키워드 일치 |', summary)
        self.assertEqual(report['sources'][0]['count'], 10)
        self.assertEqual(report['sources'][0]['retained_count'], 3)


if __name__ == '__main__':
    unittest.main()
