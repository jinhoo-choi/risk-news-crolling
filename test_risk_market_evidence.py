import hashlib
import unittest
import risk_analysis_guard as guard
from risk_market_evidence import has_market_event_evidence as evidence

class ContractTests(unittest.TestCase):

    def entry(self):
        return {'id': 1, 'relevant': True, 'grade': '주의', 'confidence': 0.9, 'entity': '', 'entities': [], 'event_type': '기타리스크'}

    def check(self, title, expected, desc='', **kwargs):
        a = {'title': title, 'desc': desc, 'url': 'https://example.invalid/source', **kwargs}
        if expected:
            guard.validate_grade_map([self.entry()], [a])
        else:
            with self.assertRaises(guard.AnalysisIncompleteError):
                guard.validate_grade_map([self.entry()], [a])

    def test_ordinary_index_declines(self):
        for s in ['코스피는 1.82% 내린 5580.09로 출발했다', '코스닥이 2% 하락했다', '코스피가 내렸다', '코스피가 내려 5500선', '코스피\n1.82% 하락', '사이드카 발동', '서킷 브레이커 발동', '코스피는 전 거래일보다 242.97포인트(4.27%) 내린 5440.83', '코스닥도 29.29포인트(4.09%) 하락한 685.66']:
            with self.subTest(s=s):
                self.check(s, True)

    def test_company_only_or_rising_index(self):
        for s in ['삼성전자 급락', '코스피 상승했지만 삼성전자 하락', '코스피 상승, 한편 삼성전자 하락', '코스피 상승과 달리 삼성전자 하락', '코스피 상승에도 삼성전자 주가는 하락했다', '코스닥 상승 그러나 현대차 하락', '코스피 상승 반대로 기아 하락', '코스피 상장사 삼성전자 주가는 하락했다', '코스피는 올랐고 삼성전자만 내렸다', '코스피가 하락하지 않았다', '코스닥 하락한 것은 아니다', '코스피가 내려가지 않았다', '코스피가 내린 것이 아니다', '코스피가 하락세는 아니다', '코스피가 하락하지는 않았다', '주가가 하락한 코스피 상장사 삼성전자는 반등했다', '하락한 코스피 상장사 삼성전자', '코스피가 2% 하락했다고 알려졌으나 사실이 아니다', '코스피가 하락할 수 있다', '코스피 하락 가능성이 제기됐다', '삼성전자는 코스피보다 2% 하락했다', '삼성전자는 코스피 대비 2% 하락했다', '현대차는 코스닥지수보다 2% 내렸다', '삼성전자는 코스피(0.5%) 대비 2% 하락했다', '삼성전자는 코스피 지수(1%)보다 2% 하락했다', '삼성전자는 코스피 종가 대비 2% 하락했다']:
            with self.subTest(s=s):
                self.check(s, False)

    def test_segments_cannot_cross_match(self):
        self.check('코스피 상승', False, desc='삼성전자 급락')

    def test_unverified_body_rejected(self):
        self.check('회사 동향', False, body='코스피 2% 하락')

    def test_source_hash_and_url_required(self):
        body = '코스피 2% 하락'
        kw = {'body': body, '_body_risk_confirmed': True, '_body_source_hash': hashlib.sha256(body.encode()).hexdigest(), '_body_source_url': 'https://example.invalid/source'}
        self.check('회사 동향', True, **kw)
        self.check('회사 동향', False, **{**kw, '_body_source_hash': 'bad'})
        self.check('회사 동향', False, **{**kw, '_body_source_url': 'https://other.invalid/'})
        self.check('회사 동향', False, **{**kw, '_body_failed': True})

    def test_entity_array_cannot_disagree(self):
        with self.assertRaises(guard.AnalysisIncompleteError):
            guard.validate_grade_map([{**self.entry(), 'entities': ['삼성전자']}], [{'title': '코스피 급락', 'desc': ''}])

    def test_summary_is_not_evidence(self):
        self.check('회사 동향', False, summary='코스피 급락')

    def test_non_market_event_rejected(self):
        self.assertFalse(evidence('파산부도', ['코스피 급락']))

    def test_forced_liquidation_unchanged(self):
        self.assertTrue(evidence('반대매매', ['미수금 반대매매 증가']))
if __name__ == '__main__':
    unittest.main(verbosity=2)
