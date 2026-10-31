"""Issuer identity, channel totals and reviewed co-subject rendering; no network."""
import copy
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

for key in ('EMAIL_SENDER','EMAIL_PASSWORD','EMAIL_RECEIVER','ANTHROPIC_API_KEY',
            'NAVER_CLIENT_ID','NAVER_CLIENT_SECRET'):
    os.environ.setdefault(key, 'offline')
import naver_news_monitor as bot


def row(name, code='000001', kind='주식', bank='20', branch='5'):
    return bot._synthesize_channel_totals({'종목명':name,'종목코드':code,'종목유형':kind,
        '뱅잔고':bank,'영잔고':branch,'뱅고객수':'2','영고객수':'1'})


class Exposure(unittest.TestCase):
    def test_confirmed_sanction_keeps_existing_exposure_floor_and_score(self):
        name = '테스트반도체'
        exp = {name: [row(name, bank='49', branch='10')]}
        article = {'entity': name, 'entities': [name], 'event_type': '금감원제재',
                   'title': f'코스닥 {name}, 거래처와 짜고 매출 부풀려…회사·전 대표 고발',
                   '_ai_confidence': 0.93, 'customer_notice': '기존 안내'}
        for initial in ('주의', '긴급'):
            with self.subTest(initial=initial), redirect_stdout(io.StringIO()):
                final = bot.regrade_by_score([dict(article, grade=initial)], exp)
            self.assertEqual(len(final), 1)
            self.assertEqual(final[0]['grade'], '참고')
            self.assertEqual(final[0]['_risk_score'], 3.5)
            self.assertEqual(bot.display_risk_score(final[0]), 1.8)
            self.assertTrue(final[0]['_grade_locked'])
            self.assertIsNone(final[0]['customer_notice'])
            self.assertFalse(bot.is_market_news(final[0]))
        for balance, expected in (('99', '참고'), ('100', '긴급')):
            with self.subTest(balance=balance), redirect_stdout(io.StringIO()):
                final = bot.regrade_by_score([dict(article, grade='긴급')],
                    {name: [row(name, bank=balance, branch='0')]})
            self.assertEqual(final[0]['grade'], expected)

    def test_listing_followup_below_floor_stays_excluded(self):
        name = '테스트반도체'
        article = {'entity': name, 'entities': [name], 'event_type': '거래정지',
                   'title': f'거래소, 거래정지 {name} 실질심사 대상 여부 내달 결정',
                   'grade': '주의', '_ai_confidence': 0.93}
        with redirect_stdout(io.StringIO()):
            final = bot.regrade_by_score([article],
                {name: [row(name, bank='49', branch='10')]})
        self.assertEqual(final, [])

    def test_market_statistics_without_issuer_omit_exposure_card(self):
        for entity in ("", [], ["", " "]):
            with self.subTest(entity=entity), patch.object(bot, "find_exposure",
                    side_effect=AssertionError("market statistics must not link balances")):
                self.assertEqual(bot.build_exposure_html(entity, {}, "2026-10-16",
                    article={"entity": "", "entities": [], "event_type": "반대매매"}), "")

    def test_named_issuer_keeps_card_even_when_balance_is_unknown(self):
        html = bot.build_exposure_html("테스트전자", {}, "2026-10-16",
            article={"entity": "테스트전자", "event_type": "반대매매"})
        self.assertIn("잔고 없음", html)

    def test_market_statistics_keep_fact_and_action_in_email(self):
        for grade in ("긴급", "주의"):
            article = {"entity": "", "entities": [], "event_type": "반대매매",
                       "grade": grade, "title": "시장 전체 반대매매 증가",
                       "url": "https://example.invalid/market",
                       "action": "집계기간과 실제 미수 고객의 상환 일정을 확인."}
            with self.subTest(grade=grade), patch.object(bot, "build_price_alert_section",
                    return_value=""), redirect_stdout(io.StringIO()):
                html = bot.build_email_html([article], exposure_data={})
            self.assertIn("시장 전체 반대매매 증가", html)
            self.assertIn(article["action"], html)
            self.assertNotIn("잔고 없음", html)

    def test_reviewed_korean_names_resolve_only_exact_snapshot_ticker(self):
        names = [("코어위브", "CRWV", "CoreWeave, Inc."),
                 ("네비우스 그룹", "NBIS", "Nebius Group N.V."),
                 ("어플라이드 디지털", "APLD", "Applied Digital Corporation")]
        for alias, ticker, canonical in names:
            with self.subTest(alias=alias):
                exp = {canonical: [row(canonical, ticker), row(canonical, ticker, "여신")],
                       alias: [row(alias, "WRONG")],
                       canonical + " Holdings": [row(canonical + " Holdings", ticker + "X")]}
                before = copy.deepcopy(exp)
                found = bot.find_exposure(alias, exp)
                self.assertEqual(len(found), 2)
                self.assertEqual({r["종목코드"] for r in found}, {ticker})
                self.assertEqual(bot.canonicalize_entity(alias, {}, exp), canonical)
                self.assertEqual(exp, before)
                self.assertEqual(bot.find_exposure(alias, {canonical: [row(canonical, ticker + "X")]}), [])
                self.assertEqual(bot.find_exposure(alias + " 계열사", {canonical: exp[canonical]}), [])


    def test_jr_legal_aliases_keep_all_bonds_but_not_similar_company(self):
        exp={'제이알글로벌리츠':[row('제이알글로벌리츠')],
             '(주)제이알글로벌위탁관리부동산투자회사':[
                 row('(주)제이알글로벌위탁관리부동산투자회사','000002','채권'),
                 row('(주)제이알글로벌위탁관리부동산투자회사','000003','채권')]}
        self.assertEqual(len(bot.find_exposure('제이알글로벌리츠',exp)),3)
        self.assertEqual(len(bot.find_exposure('제이알글로벌위탁관리',exp)),3)
        self.assertEqual(bot.find_exposure('제이알글로벌인베스트먼트',exp),[])

    def test_legal_marker_does_not_remove_issuer_letters(self):
        exp={'주성엔지니어링':[row('주성엔지니어링')],
             '성엔지니어링':[row('성엔지니어링','000002')]}
        self.assertEqual([r['종목명'] for r in bot.find_exposure('(주)주성엔지니어링',exp)],
                         ['주성엔지니어링'])

    def test_short_names_and_substrings_never_absorb_other_issuers(self):
        exp={n:[row(n,str(i).zfill(6))] for i,n in enumerate(
            ['마이크론','하나마이크론','에스엘','에스엘엘중앙','제이티','제이티비씨'])}
        for name in exp:
            self.assertEqual({r['종목명'] for r in bot.find_exposure(name,exp)},{name})

    def test_channel_amounts_and_customer_counts_are_preserved(self):
        r=bot._synthesize_channel_totals({'종목명':'테스트주식','종목코드':'000001',
            '종목유형':'여신','뱅잔고':'10.5','영잔고':'4.5','뱅고객수':'6','영고객수':'2',
            '뱅리스크고객수':'3','영리스크고객수':'1','뱅리스크잔고':'8','영리스크잔고':'2',
            '뱅유지담보비율':'1.408','영유지담보비율':'150','뱅최고리스크잔고':'4',
            '뱅최고리스크고객':'테*트'})
        self.assertEqual((r['잔고(억)'],r['고객수'],r['리스크잔고(억)'],r['리스크고객수']),
                         ('15','8','10','4'))
        self.assertEqual(r['유지담보비율'],'141')
        self.assertEqual((r['뱅잔고'],r['영잔고']),('10.5','4.5'))

    def test_related_parent_is_a_name_only_not_direct_parent_balance(self):
        exp={'한국금융지주':[row('한국금융지주',bank='1000',branch='500')]}
        html=bot.build_exposure_html('한국투자증권',exp,'2026-10-13',
                                    article={'event_type':'시스템장애'})
        self.assertIn('한국금융지주',html)
        self.assertIn('>관련주<',html)
        self.assertNotIn('1,000억',html)
        self.assertNotIn('500억',html)

    def test_related_filters_preserve_supported_candidate_without_duplicate(self):
        exp={n:[row(n)] for n in ['테스트주체','관련종목','삼성전자']}
        html=bot.build_exposure_html('테스트주체',exp,'2026-10-13',article={
            'related_stocks':['삼성전자','관련종목','없는종목']})
        self.assertIn('>관련주<',html)
        self.assertEqual(html.count('>관련종목</span>'),1)
        self.assertNotIn('>삼성전자</span>',html)
        self.assertNotIn('없는종목',html)

    def test_reviewed_joint_subjects_survive_while_background_affiliate_is_hidden(self):
        exp={n:[row(n)] for n in ['테스트전자','테스트전지','배경계열사']}
        article={'entity':'테스트전자','entities':list(exp),'event_type':'횡령배임',
                 'grade':'주의','title':'두 회사의 배임 혐의 공시','url':'https://example.invalid',
                 '_subscription_direct_entities':['테스트전자','테스트전지']}
        calls=[]
        def capture(entities,*args,**kwargs):
            calls.append(list(entities)); return ''
        with patch.object(bot,'GROUP_ENTITIES_MAP',{'테스트전자':['테스트전지','배경계열사']}), \
             patch.object(bot,'build_exposure_html',capture), \
             patch.object(bot,'build_price_alert_section',return_value=''), redirect_stdout(io.StringIO()):
            bot.build_email_html([article],exposure_data=exp)
            article.pop('_subscription_direct_entities')
            bot.build_email_html([article],exposure_data=exp)
        self.assertEqual(calls,[['테스트전자','테스트전지'],['테스트전자']])


if __name__ == '__main__':
    unittest.main()
