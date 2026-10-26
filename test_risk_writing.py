"""Frozen generation contract and final native HTML regression; no LLM or SMTP."""
import contextlib
import hashlib
import html
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_risk_subscription as integration_cases
from risk_result_validator import digest, validate

ROOT = Path(__file__).resolve().parent
CASES = json.loads((ROOT/'tests/fixtures/risk_care_cases.json').read_text())['cases']
OUTPUTS = json.loads((ROOT/'tests/fixtures/risk_care_generated.json').read_text())['outputs']


class Writing(unittest.TestCase):
    def setUp(self):
        self.integration = integration_cases.Integration()
        self.integration.setUp()
        self.bot = self.integration.bot

    def tearDown(self):
        self.integration.tearDown()

    def article(self, case):
        output = OUTPUTS[case['id']]
        return {'id':1,'article_id':'care','title':case['title'],
                'body':case['body'],'url':'https://example.invalid/care',
                'keyword':case['event_type'],'event_type':case['event_type'],
                'entity':case['entity'],'entities':[case['entity']],
                'grade':case['grade'],'_risk_score':8.2 if case['grade']=='긴급' else 2.0,
                '_ai_confidence':0.95,'pub_str':'가상 검수',**output}

    def render(self, article):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.bot.build_email_html([article],1,'가상 검수',exposure_data={},
                                            ref_date='',today_str='가상 검수')

    def test_frozen_generation_covers_every_fixed_source_and_keeps_uncertainty(self):
        self.assertEqual(set(OUTPUTS),{c['id'] for c in CASES})
        for case in CASES:
            with self.subTest(case=case['id']):
                out=OUTPUTS[case['id']]
                self.assertLessEqual(len(out['action']),120)
                self.assertLessEqual(out['action'].count('→'),2)
                if case.get('must_suppress_notice'):
                    self.assertIsNone(out['customer_notice'])
                    continue
                notice=out['customer_notice']
                self.assertLessEqual(len(notice),190)
                self.assertLessEqual(len(notice.splitlines()),5)
                for required in case['notice_required']:
                    self.assertIn(required,notice)
                for forbidden in case['notice_forbidden']:
                    self.assertNotIn(forbidden,notice)
                self.assertIn(case['entity'],out['action'])
                self.assertNotRegex(out['action']+notice,r'담보비율|신용융자|OB 진행|추가 납입')

    def test_native_html_keeps_complete_customer_care_and_action(self):
        for case in CASES:
            if case.get('must_suppress_notice'):
                continue
            with self.subTest(case=case['id']):
                article=self.article(case)
                document=self.render(article)
                # Check exact payload, including the final customer instruction;
                # a badge or a successful HTML build is insufficient.
                self.assertTrue(html.escape(article['action'],quote=True) in document,
                                case['id']+': complete action payload missing from native HTML')
                self.assertTrue(html.escape(article['customer_notice'],quote=True) in document,
                                case['id']+': complete customer care/final instruction missing from native HTML')
                self.assertIn('고객케어 안내 추천 문구',document)

    def test_notice_suppression_and_html_escaping_are_preserved(self):
        for case in CASES:
            if not case.get('must_suppress_notice'):
                continue
            document=self.render(self.article(case))
            self.assertNotIn('고객케어 안내 추천 문구',document)
        article=self.article(CASES[0])
        article['customer_notice']+='<script>bad()</script>'
        document=self.render(article)
        self.assertNotIn('<script>bad()',document)
        self.assertIn('&lt;script&gt;',document)

    def test_source_contract_and_native_consumer_keep_care_payload(self):
        case=CASES[0]
        queue=self.integration.fixture()
        job,result=queue.data['jobs/test.json'],queue.data['results/test.json']
        item=job['input']['items'][0]
        item.update(title=case['title'],description=case['body'],desc=case['body'],keyword=case['event_type'])
        job['input']['raw_articles']=[dict(item)]
        job['body_manifest'][0].update(body=case['body'],body_status='verified')
        out=OUTPUTS[case['id']]
        analysis=result['analyses']['one']
        analysis.update(entity=case['entity'],entities=[case['entity']],event_type=case['event_type'],
                        fact_summary=case['body'],action=out['action'],customer_notice=out['customer_notice'],
                        judgment={'핵심사건':case['judgment_event'],'손실주체':'주주','당사연관':'직접','확정여부':'확정'},
                        evidence=[{'source_segment':'body','source_hash':hashlib.sha256(case['body'].encode()).hexdigest(),'quote':case['body']}])
        job['input_hash']=result['input_hash']=digest(job['input'])
        job['body_manifest_hash']=result['body_manifest_hash']=digest(job['body_manifest'])
        validate(result,job,job['input'],job['body_manifest'])
        # Use a matching synthetic stock holding; there are no credit/loan rows.
        exposure={case['entity']:[{'종목명':case['entity'],'종목코드':'990001','종목유형':'주식','잔고(억)':'150','고객수':'1'}]}
        with patch.object(self.bot,'load_exposure_data',return_value=exposure):
            smtp=integration_cases.SMTP()
            with contextlib.redirect_stdout(io.StringIO()):
                self.integration.run_consume(queue,smtp)
        document=queue.data['previews/test.json']['html']
        self.assertTrue(html.escape(out['customer_notice'],quote=True) in document,
                        'native consume removed or truncated the complete customer care payload')
        self.assertIn(case['entity'],document)
        self.assertEqual(queue.data['jobs/test.json']['status'],'delivery_complete')

    def test_action_prompt_cannot_restore_the_known_over_compression_instruction(self):
        # This is a policy regression, not a semantic quality score.
        prompt=(ROOT/'action_prompt.txt').read_text()
        self.assertFalse('한 조치는 25자 안팎으로 끊는다' in prompt,
                         'known over-compression instruction was restored')
        self.assertFalse("'무슨 일 + 무엇을 확인하라'만 담는다" in prompt,
                         'customer impact/method explanation was removed from writing instructions')
        policy=json.loads((ROOT/'risk_policy.json').read_text())
        self.assertEqual(policy['action_rules']['sha256'],hashlib.sha256(prompt.encode()).hexdigest())


if __name__=='__main__':
    unittest.main()
