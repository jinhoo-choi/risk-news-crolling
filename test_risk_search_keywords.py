"""Offline recall bounds and source-bound reference alert regressions."""
import copy
import hashlib
import os
import unittest

from risk_search_keywords import COMMON_KEYWORDS, OVERSEAS_KEYWORDS, portfolio_keywords
from risk_reference_watch import reference_kind, verified_reference_watch


class SearchKeywords(unittest.TestCase):
    def test_replacements_and_requested_supervision_query(self):
        self.assertIn('금감원 증권사', COMMON_KEYWORDS)
        self.assertIn('반대매매', COMMON_KEYWORDS)
        for vague in ('부실 리스크', '신용 리스크', '유동성 리스크', '디폴트 리스크',
                      '미매각', '발행어음', '추가담보', '한국투자증권 이슈'):
            self.assertNotIn(vague, COMMON_KEYWORDS)
        self.assertEqual(len(COMMON_KEYWORDS), len(set(COMMON_KEYWORDS)))
        self.assertIn('미국 기업 파산', OVERSEAS_KEYWORDS)
        self.assertIn('파산보호 신청', OVERSEAS_KEYWORDS)

    def test_channel_aggregation_local_names_and_fund_bounds(self):
        row = lambda balance, kind='해외주식': {'종목유형':kind, '잔고(억)':balance}
        data = {'A':[row(6),row(6,'해외담보')], 'B':[row(10)],
                'F':[row(9)], 'K':[row(999,'주식')], 'Z':[row('NaN'),row(-2)]}
        names = {'A':'보유기업', 'B':'다른기업', 'F':'보유 ETF'}
        self.assertEqual(portfolio_keywords(data, names, 1),
                         ['보유기업 급락','보유기업 실적 쇼크','보유기업 가이던스 하향'])
        queries = portfolio_keywords(data, names)
        self.assertIn('보유 ETF 급락', queries)
        self.assertNotIn('보유 ETF 실적 쇼크', queries)
        self.assertFalse(any('K ' in q or 'Z ' in q for q in queries))
        many = {str(i):[row(100-i)] for i in range(40)}
        self.assertEqual(len(portfolio_keywords(many, {}, 30)), 30)
        self.assertEqual(portfolio_keywords(many, {}, 0), [])
        self.assertEqual(portfolio_keywords({'A':[row(3)], 'B':[row(4)]},
                                           {'A':'동일기업','B':'동일기업'}),
                         ['동일기업 급락','동일기업 실적 쇼크','동일기업 가이던스 하향'])


class ReferenceWatch(unittest.TestCase):
    regulatory = '금융감독원은 증권사의 주식정보 제공서비스 운영 실태를 점검하고 개선방안을 마련했다고 밝혔다.'
    service = '한국투자증권 고객센터 대기 순번이 길어 한 시간 기다리다 통화가 끊겼다는 주장이 올라왔다.'

    def kind(self, quote, entity='증권업계', core='감독 제도 개선'):
        return reference_kind(entity, '기타리스크', {'핵심사건':core}, [quote])

    def test_body_proof_and_stage_are_required(self):
        self.assertEqual(self.kind(self.regulatory), 'supervisory_notice')
        self.assertEqual(self.kind('금감원은 증권사에 제재 예고를 사전통지했다.'), 'supervisory_notice')
        for quote in ('금감원이 증권사 제재를 예고할 가능성이 있다는 관측이다.',
                      '금감원은 증권사 제재 예고 계획이 없다고 밝혔다.',
                      '금감원은 은행 수수료 제도 개선방안을 마련했다고 밝혔다.',
                      '증권업계는 금감원에 제도 개선이 필요하다고 제안했다.'):
            self.assertEqual(self.kind(quote), '', quote)
        self.assertEqual(self.kind(self.service, '한국투자증권', '전화 접수 차질'), 'owner_service')
        self.assertEqual(self.kind('한국투자증권은 전화 접수에 문제가 없다고 밝혔다.',
                                   '한국투자증권','전화 접수 차질'), '')
        self.assertEqual(self.kind('삼성증권 전화가 불통이었다. 한국투자증권은 원활했다.',
                                   '한국투자증권','전화 접수 차질'), '')
        self.assertEqual(reference_kind('한국투자증권','시스템장애',
                                        {'핵심사건':'전화 접수 장애'},[self.service]), '')

    def test_source_binding_cannot_be_reused_after_changes(self):
        article = {'entity':'증권업계','event_type':'기타리스크',
                   '_judgment':{'핵심사건':'감독 제도 개선'}, 'body':self.regulatory,
                   'url':'https://example.invalid/source', '_body_source_url':'https://example.invalid/source',
                   '_body_risk_confirmed':True, '_body_source_hash':hashlib.sha256(self.regulatory.encode()).hexdigest(),
                   '_reference_watch_quotes':[self.regulatory], '_subscription_reference_watch':'supervisory_notice'}
        self.assertTrue(verified_reference_watch(article))
        for key, value in [('body',self.regulatory+'변경'),('_body_source_url','https://example.invalid/other'),
                           ('_body_risk_confirmed',False),('_reference_watch_quotes',['가짜 근거'])]:
            changed = copy.deepcopy(article)
            changed[key] = value
            self.assertFalse(verified_reference_watch(changed))

    def test_metadata_cannot_exclude_requested_article_in_new_contract(self):
        for key in ('EMAIL_SENDER','EMAIL_PASSWORD','EMAIL_RECEIVER','ANTHROPIC_API_KEY','NAVER_CLIENT_ID','NAVER_CLIENT_SECRET'):
            os.environ.setdefault(key, 'fake')
        from naver_news_monitor import is_hard_excluded
        title = "증권사 주식정보 서비스 이용료 '깜깜이'…금감원, 제도 개선 나선다"
        self.assertTrue(is_hard_excluded(title)[0])
        self.assertFalse(is_hard_excluded(title, '', '', True)[0])
        self.assertTrue(is_hard_excluded('은행 서비스…금감원, 제도 개선 나선다', '', '', True)[0])
        from risk_review_evidence import bind_sources, validate_decision, source_hash
        article = {'article_id':'a', 'title':title,'description':'','url':'https://example.invalid/source'}
        body = {'article_id':'a','body':self.regulatory,'body_status':'verified'}
        checkpoint = {'classifications':[{'article_id':'a','relevant':False,'reason':'제도 개선',
                                          'decision_basis':{'kind':'metadata_rule'}}]}
        job = {'input':{'items':[article]},'body_manifest':[body]}
        legacy = bind_sources(checkpoint, job)
        validate_decision(legacy['classifications'][0],article,body)
        job['reference_watch_version'] = 1
        with self.assertRaises(ValueError):
            bind_sources(checkpoint, job)
        with self.assertRaises(ValueError):
            validate_decision(legacy['classifications'][0],article,body,reference_watch=True)


if __name__ == '__main__':
    unittest.main()
