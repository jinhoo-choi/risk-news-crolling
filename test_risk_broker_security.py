"""Broker source attribution and unchanged delivery gates; no network or SMTP."""
import copy
import hashlib
import unittest
from unittest.mock import patch

from risk_broker_security import confirmed_security_evidence, verified_broker_security
from risk_subscription import import_bot


class BrokerSecurity(unittest.TestCase):
    def article(self):
        body = '미래에셋증권은 고객정보 유출 사고가 발생한 사실을 확인했다.'
        return dict(title='미래에셋증권 고객정보 유출 확인', entity='미래에셋증권',
                    entities=['미래에셋증권'], grade='긴급', _ai_confidence=0.95,
                    _risk_score=9, event_type='시스템장애', url='https://example.invalid/security',
                    body=body, _body_failed=False, _body_risk_confirmed=True,
                    _body_source_url='https://example.invalid/security',
                    _body_source_hash=hashlib.sha256(body.encode()).hexdigest(),
                    _judgment={'확정여부':'확정'}, _subscription_broker_security=True,
                    _broker_security_quotes=[body], customer_notice=None)

    def test_unconfirmed_prevention_negation_and_other_entity_do_not_qualify(self):
        self.assertTrue(confirmed_security_evidence('미래에셋증권',
                        ['미래에셋증권은 개인(신용)정보 유출 사실을 확인했다.']))
        for text in ('미래에셋증권은 해킹 예방 훈련을 실시했다.',
                     '미래에셋증권의 고객정보 유출 가능성이 제기됐다.',
                     '미래에셋증권은 고객정보 유출이 발생하지 않았다고 확인했다.',
                     '삼성증권은 고객정보 유출 사고가 발생한 사실을 확인했다.'):
            with self.subTest(text=text):
                self.assertFalse(confirmed_security_evidence('미래에셋증권', [text]))

    def test_unverified_body_stale_hash_and_legacy_round_cannot_enable_exception(self):
        original = self.article()
        self.assertTrue(verified_broker_security(original))
        for field, value in (('_body_source_hash','bad'), ('_body_risk_confirmed',False),
                             ('_body_failed',True), ('_subscription_broker_security',False),
                             ('_broker_security_quotes',['미래에셋증권은 해킹 피해를 입었다.'])):
            a = copy.deepcopy(original); a[field] = value
            self.assertFalse(verified_broker_security(a))

    def test_no_exposure_reference_survives_both_scopes_without_opening_full_send(self):
        bot = import_bot()
        a = self.article()
        rows = bot.regrade_by_score([a], {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(a['grade'], '참고')
        self.assertTrue(a['_grade_locked'])
        self.assertFalse(a['_force_urgent'])
        self.assertEqual(bot.notice_forbidden_reason(a), '타 증권사 기사')
        self.assertEqual(bot.filter_articles_for_scope(rows, {}, True), rows)
        self.assertEqual(bot.filter_articles_for_scope(rows, {}, False), rows)
        with patch.object(bot, 'build_price_alert_section', lambda *args: ''):
            self.assertTrue(bot.decide_send_scope(rows, {})['self_only'])
        legacy = self.article(); legacy.pop('_subscription_broker_security')
        self.assertEqual(bot.regrade_by_score([legacy], {}), [])

    def test_own_company_mention_does_not_promote_competitor_security(self):
        bot = import_bot()
        a = self.article()
        a['title'] = '미래에셋증권 해킹 확인, 한국투자증권 등 보안 점검'
        a['customer_notice'] = '고객 안내를 잘못 생성한 입력'
        rows = bot.regrade_by_score([a], {})
        self.assertEqual(rows[0]['entity'], '미래에셋증권')
        self.assertEqual(rows[0]['grade'], '참고')
        self.assertIsNone(rows[0]['customer_notice'])
        with patch.object(bot, 'build_price_alert_section', lambda *args: ''):
            self.assertTrue(bot.decide_send_scope(rows, {})['self_only'])


if __name__ == '__main__':
    unittest.main()
