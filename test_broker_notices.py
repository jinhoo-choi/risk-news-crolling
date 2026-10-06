"""공지 파서·부분 실패 회귀. 공개 응답 fixture와 mock만 사용하며 네트워크/메일 호출 없음."""
import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests
import broker_notices as b

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
        item = {'date': b.datetime.now(b.KST).strftime('%Y-%m-%d'), 'company': '삼성증권', 'title': '테스트 공지', 'url': 'https://example.com/1'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, 'DATA_DIR', tmp):
            b.save_csv(item['company'], [item])
            b.save_csv(item['company'], [dict(item, url='https://example.com/2')])
            with open(Path(tmp)/'삼성증권.csv', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ['date', 'company', 'title', 'url'])
                self.assertEqual(list(reader), [item])

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
        self.assertEqual(saved, 0)

    def test_single_failure_preserves_successful_sources(self):
        rc, report, _, saved = self.run_main({'miraeasset'})
        self.assertEqual((rc, report['failed_sources'], saved), (1, 1, 3))

    def test_healthy_run_and_legacy_empty_are_distinguished(self):
        rc, report, _, saved = self.run_main(set())
        self.assertEqual((rc, report['status'], saved), (0, 'success', 4))
        self.assertEqual(report['sources'][5]['status'], 'empty')
        self.assertEqual(report['sources'][5]['parser'], 'legacy_generic')


if __name__ == '__main__':
    unittest.main()
