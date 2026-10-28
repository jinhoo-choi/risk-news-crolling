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
from risk_result_validator import digest, validate, validate_action_plan

ROOT = Path(__file__).resolve().parent
CASES = json.loads((ROOT/'tests/fixtures/risk_care_cases.json').read_text())['cases']
OUTPUTS = json.loads((ROOT/'tests/fixtures/risk_care_generated.json').read_text())['outputs']


class Writing(unittest.TestCase):
    def action_plan_fixture(self):
        queue=self.integration.fixture()
        job,result=queue.data['jobs/test.json'],queue.data['results/test.json']
        body=('가상방산은 입찰제한 대상 사업과 적용 기간을 공시했다. '
              '집행정지 여부는 법원의 판단을 기다리고 있으며 신규 수주 영향은 아직 확정되지 않았다.')
        action=('가상방산 보유 주식의 평가손을 재산출 → 입찰제한 대상 사업·기간을 회사 공시와 대조 → '
                '법원의 집행정지 결정이 나오면 신규 수주에 미치는 영향을 재검토')
        plan={'event_anchor':'입찰제한','current_task':'가상방산 보유 주식의 평가손을 재산출',
              'check_source':'회사 공시','followup_condition':'법원의 집행정지 결정이 나오면',
              'followup_task':'신규 수주에 미치는 영향을 재검토'}
        item=job['input']['items'][0]
        item.update(title='가상방산 입찰제한 공시, 집행정지 법원 판단 대기',description=body,desc=body,keyword='입찰제한')
        job['input']['raw_articles']=[dict(item)]
        job['body_manifest'][0].update(body=body,body_status='verified')
        a=result['analyses']['one']
        a.update(entity='가상방산',entities=['가상방산'],event_type='금감원제재',base_grade='주의',
                 fact_summary=body,action=action,action_plan=plan,customer_notice=None,
                 judgment={'핵심사건':'입찰제한','손실주체':'주주','당사연관':'직접','확정여부':'확정'},
                 evidence=[{'source_segment':'body','source_hash':hashlib.sha256(body.encode()).hexdigest(),'quote':body}])
        job['action_quality_version']=1
        job['input_hash']=result['input_hash']=digest(job['input'])
        job['body_manifest_hash']=result['body_manifest_hash']=digest(job['body_manifest'])
        return queue

    def test_execution_plan_rejects_missing_generic_or_ungrounded_parts(self):
        import copy
        queue=self.action_plan_fixture()
        job,result=queue.data['jobs/test.json'],queue.data['results/test.json']
        validate(result,job,job['input'],job['body_manifest'])
        for fault in ('missing','generic','nominal','source','condition','final_trim'):
            a=copy.deepcopy(result['analyses']['one'])
            with self.subTest(fault=fault):
                if fault=='missing': a.pop('action_plan')
                elif fault=='generic':
                    a['action_plan']['followup_task']='평가 영향 점검'
                    a['action']+=' → 평가 영향 점검'
                elif fault=='nominal':
                    previous=a['action_plan']['current_task']
                    a['action_plan']['current_task']='가상방산 보유 주식의 평가 변동과 입찰제한 효력을 회사 공시로 대조'
                    a['action']=a['action'].replace(previous,a['action_plan']['current_task'])
                elif fault=='source':
                    a['action_plan']['event_anchor']='확정 부도'
                    a['action']+=' → 확정 부도'
                elif fault=='condition': a['action_plan']['followup_condition']='회사 공시'
                with self.assertRaises(ValueError):
                    validate_action_plan(a,job['body_manifest'][0]['body'],
                                         action=a['action'][:25] if fault=='final_trim' else None)

    def test_native_action_plan_survives_and_trimmed_action_blocks_smtp(self):
        queue=self.action_plan_fixture()
        exposure={'가상방산':[{'종목명':'가상방산','종목코드':'990001','종목유형':'주식','잔고(억)':'150','고객수':'1'}]}
        smtp=integration_cases.SMTP()
        with patch.object(self.bot,'load_exposure_data',return_value=exposure),contextlib.redirect_stdout(io.StringIO()):
            self.integration.run_consume(queue,smtp)
        action=queue.data['results/test.json']['analyses']['one']['action']
        document=queue.data['previews/test.json']['html']
        self.assertIn(html.escape(action,quote=True),document)
        self.assertNotIn('action_plan',document)
        self.assertTrue(smtp.calls)
        # Recreate pristine synthetic history and mutate the real sanitizer path.
        Path('seen_news.json').write_bytes(self.integration.before)
        queue2=self.action_plan_fixture()
        smtp2=integration_cases.SMTP()
        with patch.object(self.bot,'load_exposure_data',return_value=exposure), \
             patch.object(self.bot,'dedup_action_phrases',return_value=('가상방산 평가 영향 점검',[])), \
             contextlib.redirect_stdout(io.StringIO()), \
             self.assertRaisesRegex(ValueError,'최종 문구 불일치'):
            self.integration.run_consume(queue2,smtp2)
        self.assertEqual(smtp2.calls,[])
        self.assertEqual(Path('seen_news.json').read_bytes(),self.integration.before)

    def test_action_rows_preserve_asset_types_splits_and_deduplicate_alias_references(self):
        credit={'종목명':'가상방산','종목코드':'990001','종목유형':'여신','잔고(억)':'40'}
        stock={'종목명':'가상방산','종목코드':'990001','종목유형':'주식','잔고(억)':'150'}
        split=dict(stock, **{'잔고(억)':'30'})
        exposure={'가상방산':[credit,stock,split]}
        rows=self.bot.action_exposure_rows(['가상방산','(주)가상방산','가상방산'],exposure)
        self.assertEqual([id(r) for r in rows],[id(credit),id(stock),id(split)])
        self.assertEqual(sum(float(r['잔고(억)']) for r in rows if r['종목유형']=='주식'),180)

    def test_same_code_credit_first_retains_native_plan_and_html(self):
        queue=self.action_plan_fixture()
        analysis=queue.data['results/test.json']['analyses']['one']
        analysis['action']+='하고 고객 안내 준비'
        analysis['action_plan']['followup_task']+='하고 고객 안내 준비'
        rows=[{'종목명':'가상방산','종목코드':'990001','종목유형':kind,
               '잔고(억)':balance,'고객수':'1'} for kind,balance in [('여신','40'),('주식','150')]]
        smtp=integration_cases.SMTP()
        with patch.object(self.bot,'load_exposure_data',return_value={'가상방산':rows}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.integration.run_consume(queue,smtp)
        document=queue.data['previews/test.json']['html']
        for key in ('followup_condition','followup_task'):
            self.assertIn(html.escape(analysis['action_plan'][key],quote=True),document)
        self.assertEqual(len(smtp.calls),3)

    def test_notice_removal_preserves_condition_and_unrelated_task_but_guard_still_blocks(self):
        action=('가상방산 평가손을 재산출하고 회사 공시와 대조 → '
                '추가 어닝쇼크가 공시되면 담보부족 대응 대상을 재분류하고 고객 안내 준비')
        plan={'event_anchor':'어닝쇼크','current_task':'가상방산 평가손을 재산출',
              'check_source':'회사 공시','followup_condition':'추가 어닝쇼크가 공시되면',
              'followup_task':'담보부족 대응 대상을 재분류하고 고객 안내 준비'}
        credit=[{'종목유형':'여신','잔고(억)':'40'}]
        cleaned,removed=self.bot.strip_unsupported_action_clauses(action,credit)
        self.assertIn(plan['followup_condition'],cleaned)
        self.assertIn('담보부족 대응 대상을 재분류',cleaned)
        self.assertNotIn('고객 안내 준비',cleaned)
        self.assertTrue(removed)
        analysis={'action':action,'action_plan':plan,'body_status':'verified','base_grade':'주의'}
        with self.assertRaisesRegex(ValueError,'followup_task'):
            validate_action_plan(analysis,'추가 어닝쇼크가 공시됐다',action=cleaned)
        stock=credit+[{'종목유형':'주식','잔고(억)':'150'}]
        self.assertEqual(self.bot.strip_unsupported_action_clauses(action,stock),(action,[]))

    def test_unheld_hsbc_native_reference_path_does_not_invent_exposure(self):
        queue=self.action_plan_fixture()
        job,result=queue.data['jobs/test.json'],queue.data['results/test.json']
        # Synthetic event text; exercise the real no-exposure downgrade before
        # action generation, rather than treating an isolated sanitizer as send scope.
        old=json.dumps({'job':job,'result':result},ensure_ascii=False)
        pair=json.loads(old.replace('가상방산','HSBC'))
        job,result=pair['job'],pair['result']
        body=job['body_manifest'][0]['body']
        result['analyses']['one']['evidence'][0]['source_hash']=hashlib.sha256(body.encode()).hexdigest()
        job['input_hash']=result['input_hash']=digest(job['input'])
        job['body_manifest_hash']=result['body_manifest_hash']=digest(job['body_manifest'])
        queue.data.update({'jobs/test.json':job,'results/test.json':result})
        smtp=integration_cases.SMTP()
        with patch.object(self.bot,'load_exposure_data',return_value={}),contextlib.redirect_stdout(io.StringIO()):
            self.integration.run_consume(queue,smtp)
        final=queue.data['filter-logs/test.json']['final']
        self.assertTrue(final)
        self.assertEqual(final[0]['grade'],'참고')
        self.assertTrue(final[0]['_grade_locked'])
        self.assertNotIn(html.escape(result['analyses']['one']['action'],quote=True),
                         queue.data['previews/test.json']['html'])
        self.assertEqual(queue.data['metrics/test.json']['provider_route_calls'].get('action',0),0)
        self.assertEqual(self.bot.find_exposure('HSBC',{}),[])

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
