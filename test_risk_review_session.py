"""Native consume, frozen sources and atomic owner races; never external I/O."""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
from risk_queue import digest, now
from risk_review_evidence import source_hash, validate_decision, unavailable_basis
from risk_review_context import analysis_plan, ready_result, next_batch
from risk_review_session import begin, save_checkpoint, finalize, stop, verify_ready
from risk_body_quality import body_quality
import risk_subscription as bridge
import test_risk_continuity as fixtures
from test_risk_subscription import SMTP
import test_risk_hardening as hardening
import test_risk_delivery_recovery as recovery

MAIN='394e4c66581114b47e7c3fbcb1693cbfa679cbc1'


class Sessions(unittest.TestCase):
    def setUp(self):
        self.native=fixtures.NativeContinuity();self.native.setUp()
        self.q=self.native.streamlined();self.j=self.q.data['jobs/test.json']
        self.j.update(delivery_focus_version=2,review_evidence_version=1)
        self.j['created_at']=now().isoformat()
        self.answer=self.q.data.pop('results/test.json')
    def tearDown(self):self.native.tearDown()
    def checkpoint(self, ids=None):
        ids=set(ids or [x['article_id'] for x in self.answer['classifications']])
        cp={k:self.j[k] for k in ['job_id','input_hash','policy_hash','body_manifest_hash']}
        cp.update(covered_ids=[r['article_id'] for r in self.answer['classifications'] if r['article_id'] in ids],
                  classifications=[copy.deepcopy(r) for r in self.answer['classifications'] if r['article_id'] in ids],
                  analyses={k:copy.deepcopy(v) for k,v in self.answer['analyses'].items() if k in ids},event_groups=[])
        return cp
    def publish(self):
        begin(self.q,'test','owner',MAIN,'automation')
        save_checkpoint(self.q,'test','owner',self.checkpoint())
        return finalize(self.q,'test','owner')
    def consume(self,smtp):
        with contextlib.redirect_stdout(io.StringIO()):self.native.run_consume(self.q,smtp)
    def held(self,verdict,reason,basis=None):
        r=self.answer['classifications'][1];r.update(relevant=verdict,reason=reason)
        self.answer['analyses'].pop('held')
        if basis is not None:r['decision_basis']=basis
    def body_basis(self,blocker=None):
        b=self.j['body_manifest'][1]['body'];v={'kind':'body','source_hash':source_hash(b),'quote':b}
        if blocker:v['blocker']=blocker
        return v
    def narrative_checkpoint(self, writing):
        cp=self.checkpoint()
        cp.update(covered_ids=[],classifications=[],analyses={},event_groups=[],narratives=writing)
        return cp

    def test_representative_writing_is_appended_without_rewriting_decisions(self):
        self.j['caution_care_version']=1
        self.answer['analyses']['one'].update(action='',customer_notice=None)
        begin(self.q,'test','owner',MAIN,'automation')
        save_checkpoint(self.q,'test','owner',self.checkpoint())
        original=copy.deepcopy(self.q.data['analysis-checkpoints/test/owner-0001.json'])
        with self.assertRaisesRegex(ValueError,'문구 체크포인트 필요'):
            finalize(self.q,'test','owner')
        draft=ready_result(self.j,[original],attempt_id='owner',narrative_draft=True)
        self.assertEqual(draft['analyses']['one']['action'],'')
        action='당사 공식 장애 공지에서 영향 범위와 복구 상태를 확인한다.'
        cp=self.narrative_checkpoint({'one':{'action':action,'customer_notice':None}})
        save_checkpoint(self.q,'test','owner',cp)
        result=finalize(self.q,'test','owner')
        self.assertEqual(result['analyses']['one']['action'],action)
        self.assertEqual(self.q.data['analysis-checkpoints/test/owner-0001.json'],original)
        self.assertEqual(self.q.data['analysis-attempts/test.json']['processed_count'],2)
        before=copy.deepcopy(self.q.data)
        self.assertEqual(finalize(self.q,'test','owner'),result)
        self.assertEqual(self.q.data,before)

    def test_narratives_cannot_change_facts_duplicate_or_disguise_missing_writing(self):
        self.j['caution_care_version']=1
        self.answer['analyses']['one'].update(action='',customer_notice=None)
        begin(self.q,'test','owner',MAIN,'automation')
        save_checkpoint(self.q,'test','owner',self.checkpoint())
        for writing in ({'action':'','customer_notice':None},
                        {'action':'공식 공지 확인','customer_notice':None,'base_grade':'긴급'}):
            with self.subTest(writing=writing),self.assertRaises(ValueError):
                save_checkpoint(self.q,'test','owner',self.narrative_checkpoint({'one':writing}))
        cp=self.narrative_checkpoint({'one':{'action':'','customer_notice':None,
                      'omission_reason':'사건의 현재 조치를 뒷받침할 근거 부족'}})
        save_checkpoint(self.q,'test','owner',cp)
        with self.assertRaisesRegex(ValueError,'중복 오류'):
            save_checkpoint(self.q,'test','owner',self.narrative_checkpoint({}))
        self.assertEqual(finalize(self.q,'test','owner')['analyses']['one']['action'],'')

    def test_narratives_need_full_first_pass_and_do_not_apply_to_legacy_jobs(self):
        begin(self.q,'test','owner',MAIN,'automation')
        with self.assertRaisesRegex(ValueError,'계약'):
            save_checkpoint(self.q,'test','owner',self.narrative_checkpoint({}))
        self.j['caution_care_version']=1
        with self.assertRaisesRegex(ValueError,'전수 1차 처리'):
            save_checkpoint(self.q,'test','owner',self.narrative_checkpoint({}))

    def test_bad_optional_narrative_fields_do_not_block_valid_peers(self):
        self.j['caution_care_version']=1
        self.answer['analyses']['one'].update(action='',customer_notice=None)
        begin(self.q,'test','owner',MAIN,'automation')
        save_checkpoint(self.q,'test','owner',self.checkpoint())
        cp=self.narrative_checkpoint({'one':{'action':99,'customer_notice':'잘못된 문구'*40,
                                             'omission_reason':'선택 문구의 형식 오류를 기록한다'}})
        owner=save_checkpoint(self.q,'test','owner',cp)
        self.assertFalse(owner['validation_summary']['held_articles'])
        self.assertEqual(set(owner['validation_summary']['omitted_fields']['one']),{'action','customer_notice'})
        finalize(self.q,'test','owner')
        smtp=SMTP();self.consume(smtp)
        self.assertTrue(smtp.calls)
        self.assertFalse(self.q.data['jobs/test.json']['deferred_ids'])

    def test_narrative_draft_cli_does_not_publish_or_require_final_writing(self):
        import tempfile
        self.j['caution_care_version']=1
        cp=self.checkpoint()
        with tempfile.TemporaryDirectory() as tmp:
            snapshot=Path(tmp,'input.json');output=Path(tmp,'draft.json')
            snapshot.write_text(json.dumps({'job':self.j,'checkpoints':[cp]}))
            root=Path(__file__).parent
            cmd=[sys.executable,str(root/'risk_review_context.py'),str(snapshot),
                 '--narrative-draft','--attempt-id','owner','--output',str(output)]
            before=copy.deepcopy(self.q.data)
            done=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(done.returncode,0,done.stderr)
            draft=json.loads(output.read_text())
            self.assertEqual(draft['scope'],'local_narrative_draft_only_not_ready')
            self.assertEqual(draft['result']['review_attempt_id'],'owner')
            self.assertEqual(self.q.data,before)

    def test_normal_native_delivery_and_idempotent_finalize(self):
        result=self.publish();before=copy.deepcopy(self.q.data)
        self.assertEqual(finalize(self.q,'test','owner'),result);self.assertEqual(self.q.data,before)
        smtp=SMTP();self.consume(smtp)
        self.assertEqual(len(smtp.calls),3)
        self.assertEqual(self.q.data['jobs/test.json']['review_receipt']['result_hash'],digest(result))
        with contextlib.redirect_stdout(io.StringIO()):self.native.ack(self.q)
        self.assertTrue(self.q.data['jobs/test.json']['ack'])
        self.consume(smtp);self.assertEqual(len(smtp.calls),3)
    def test_null_time_shortage_cannot_claim_attempt(self):
        self.held(None,'시간 부족으로 본문 미열람·분석 미시도',self.body_basis('analysis_failed'))
        with self.assertRaisesRegex(ValueError,'미시도'):self.publish()
        self.assertNotIn('results/test.json',self.q.data)
    def test_bare_true_empty_analysis_cannot_disguise_unattempted_work(self):
        self.answer['analyses']['held']={}
        with self.assertRaisesRegex(ValueError,'근거 누락'):self.publish()
        self.assertNotIn('results/test.json',self.q.data)
    def test_attempted_fact_error_keeps_verified_peer(self):
        self.answer['analyses']['held']['fact_summary']='원문에 없는 피해액 987654321원이다.'
        self.publish()
        summary=self.q.data['analysis-attempts/test.json']['validation_summary']
        self.assertEqual(summary['held_articles'],{'held':'원문에 없는 수치: fact_summary'})
        smtp=SMTP();self.consume(smtp)
        self.assertEqual(len(smtp.calls),3);self.assertEqual(self.q.data['jobs/test.json']['deferred_ids'],['held'])
    def test_source_number_spelling_and_foreign_index_guard_remain_strict(self):
        from risk_result_validator import _validate_analysis
        analysis=copy.deepcopy(self.answer['analyses']['held'])
        analysis.update(entity='',entities=[],event_type='기타리스크',base_grade='참고',
                        action='',customer_notice=None)
        analysis['judgment'].update(당사연관='직접',핵심사건='시장 제도 및 지수 변동')
        def check(body, summary):
            analysis.update(fact_summary=summary,evidence=[{'source_segment':'body',
                'source_hash':source_hash(body),'quote':body}])
            return _validate_analysis(analysis,{'title':'시장 제도 및 지수 소식','description':''},
                                      {'body':body,'body_status':'verified'},self.j)
        policy='금융당국은 오는 9일부터 3개월간 증권사 신용융자 담보비율 유지 의무를 면제하기로 했다.'
        check(policy,policy)
        with self.assertRaisesRegex(ValueError,'원문에 없는 수치'):
            check(policy,policy.replace('오는 9일','11월 9일'))
        domestic='코스피는 5.52% 하락했다.'
        check(domestic,domestic)
        with self.assertRaisesRegex(ValueError,'원문에 없는 수치'):
            check(domestic,'코스피는 5.5%대 하락했다.')
        with self.assertRaisesRegex(ValueError,'빈 주체'):
            check('나스닥지수는 5.52% 하락했다.','나스닥지수는 5.52% 하락했다.')
    def test_candidate_analysis_failure_with_source_proof_is_individual_hold(self):
        self.answer['analyses'].pop('held')
        self.answer['classifications'][1]['decision_basis']=self.body_basis()
        self.publish();smtp=SMTP();self.consume(smtp)
        self.assertEqual(len(smtp.calls),3);self.assertEqual(self.q.data['jobs/test.json']['deferred_ids'],['held'])
    def test_title_only_false_without_proof_cannot_finalize(self):
        self.held(False,'제목과 설명에서 위험을 찾지 못해 제외')
        with self.assertRaisesRegex(ValueError,'근거 누락'):self.publish()
    def test_fake_metadata_rule_cannot_hide_generic_title_incident_body(self):
        a=self.j['input']['items'][1];a.update(title='서비스 운영 현황 안내',description='서비스 이용 현황입니다.')
        self.j['input_hash']=digest(self.j['input'])
        self.held(False,'위험 단어 없음',{'kind':'metadata_rule','source_hash':source_hash(a['title']+'\n'+a['description']),'rule':'위험 단어 없음'})
        with self.assertRaisesRegex(ValueError,'본문 판단 필요'):self.publish()
    def test_source_backed_individual_hold_keeps_verified_peer(self):
        self.held(None,'사건 날짜 추가 확인 필요',self.body_basis('date'));self.publish()
        smtp=SMTP();self.consume(smtp)
        self.assertEqual(len(smtp.calls),3);self.assertEqual(self.q.data['jobs/test.json']['deferred_ids'],['held'])
        self.assertTrue(self.q.data['jobs/test.json']['first_pass_complete'])
        self.assertEqual(self.q.data['jobs/test.json']['first_pass_count'],2)
        self.assertFalse(self.q.data['jobs/test.json']['analysis_complete'])
    def test_real_missing_body_is_automatically_checkpointed_once(self):
        self.j['body_manifest'][1].update(body='',body_status='missing');self.j['body_manifest_hash']=digest(self.j['body_manifest'])
        begin(self.q,'test','owner',MAIN,'automation')
        save_checkpoint(self.q,'test','owner',self.checkpoint(['one']))
        result=finalize(self.q,'test','owner')
        self.assertIsNone(result['classifications'][1]['relevant'])
        self.assertEqual(result['classifications'][1]['decision_basis'],unavailable_basis(self.j['body_manifest'][1]))
        verify_ready(self.q,self.j,result)
    def test_unattempted_id_blocks_finalization(self):
        begin(self.q,'test','owner',MAIN,'automation');save_checkpoint(self.q,'test','owner',self.checkpoint(['one']))
        with self.assertRaisesRegex(ValueError,'미처리'):finalize(self.q,'test','owner')
    def test_other_running_owner_blocks_begin_and_consume(self):
        result=self.publish();self.q.data['analysis-attempts/test.json'].update(attempt_id='other',state='running')
        with self.assertRaisesRegex(ValueError,'다른 분석'):begin(self.q,'test','new',MAIN,'automation')
        smtp=SMTP()
        with self.assertRaisesRegex(ValueError,'소유권'):self.consume(smtp)
        self.assertEqual(smtp.calls,[])
    def test_changed_checkpoint_blocks_consume(self):
        self.publish();ref=self.q.data['analysis-attempts/test.json']['checkpoint_refs'][0]
        self.q.data[ref['path']]['classifications'][0]['reason']='tampered'
        smtp=SMTP()
        with self.assertRaisesRegex(ValueError,'체크포인트 변경'):self.consume(smtp)
        self.assertEqual(smtp.calls,[])
    def test_owner_race_at_sending_claim_sends_nothing(self):
        self.publish();original=self.q.put_many
        def race(values,expected,deletions=()):
            if values.get('jobs/test.json',{}).get('status')=='sending':
                self.q.data['analysis-attempts/test.json']['attempt_id']='racing'
            return original(values,expected,deletions)
        self.q.put_many=race;smtp=SMTP()
        with self.assertRaises(RuntimeError):self.consume(smtp)
        self.assertEqual(smtp.calls,[])
    def test_explicit_stop_resume_reuses_immutable_checkpoint(self):
        begin(self.q,'test','owner',MAIN,'automation');cp=self.checkpoint(['one']);save_checkpoint(self.q,'test','owner',cp)
        before=copy.deepcopy(self.q.data)
        save_checkpoint(self.q,'test','owner',cp);self.assertEqual(self.q.data,before)
        stop(self.q,'test','owner','외부 종료 전 남은 입력을 후속 실행에 인계')
        resumed=begin(self.q,'test','next',MAIN,'automation')
        self.assertEqual(resumed['processed_count'],1)
        save_checkpoint(self.q,'test','next',self.checkpoint(['held']));result=finalize(self.q,'test','next')
        self.assertEqual(result['review_attempt_id'],'next');verify_ready(self.q,self.j,result)
        self.assertIn('audit/test/owner-ended.json',self.q.data)
    def test_false_source_hash_and_quote_corruption_blocks(self):
        for field,value in [('source_hash','0'*64),('quote','없는 원문 인용입니다.')]:
            basis=self.body_basis();basis[field]=value
            with self.assertRaises(ValueError):validate_decision({'article_id':'held','relevant':False,'reason':'비대상','decision_basis':basis},self.j['input']['items'][1],self.j['body_manifest'][1])
    def test_claimed_owner_change_cannot_block_accepted_history_recovery(self):
        self.publish();smtp=SMTP();self.consume(smtp)
        self.q.data['analysis-attempts/test.json']['attempt_id']='external-change'
        self.q.remote_seen=self.native.before
        with patch.object(bridge.smtplib,'SMTP_SSL',side_effect=AssertionError('no resend')),contextlib.redirect_stdout(io.StringIO()):
            bridge.recover(self.q,'test')
        self.assertEqual(len(smtp.calls),3)
    def test_next_batch_prioritizes_risk_without_classifying_or_truncating(self):
        first=self.j['input']['items'][0];first.update(title='서비스 운영 현황 안내',description='서비스 현황입니다.')
        self.j['body_manifest'][0]['body']='회사는 정기 주주총회 개최 일정을 공시했다.'
        self.j['body_manifest_hash']=digest(self.j['body_manifest'])
        self.j['input_hash']=digest(self.j['input'])
        plan=next_batch(self.j,limit=1)
        self.assertEqual(plan['priority_ids'],['held'])
        self.assertEqual(plan['pending_ids'],['one','held']);self.assertEqual(plan['classifications'],[])
        self.assertEqual(''.join(p['body'] for p in plan['pages']),self.j['body_manifest'][1]['body'])
    def test_generic_title_severe_body_moves_ahead_without_implicit_decision(self):
        first=self.j['input']['items'][0];first.update(title='서비스 운영 현황 안내',description='서비스 현황입니다.')
        second=self.j['input']['items'][1];second.update(title='다른 회사 운영 안내',description='다른 회사 안내입니다.')
        self.j['body_manifest'][1]['body']='다른 증권사 서비스에 장애가 발생했다.'
        self.j['input_hash']=digest(self.j['input']);self.j['body_manifest_hash']=digest(self.j['body_manifest'])
        plan=next_batch(self.j,limit=1)
        self.assertEqual(plan['priority_ids'],['one']);self.assertEqual(plan['classifications'],[])
        self.assertEqual(plan['pending_ids'],['one','held'])
    def test_offline_session_cli_emits_leased_private_plan_only(self):
        files={k:{'value':v,'sha':self.q.get(k)[1]} for k,v in self.q.data.items()}
        for k in ['analysis-attempts/test.json','results/test.json','outbox/test.json']:files[k]={'value':None,'sha':None}
        Path('snapshot.json').write_text(json.dumps({'queue_head':MAIN,'files':files}))
        script=Path(__file__).with_name('risk_review_session.py')
        run=subprocess.run([sys.executable,str(script),'snapshot.json','begin','--job-id','test','--attempt-id','owner',
                            '--source-main',MAIN,'--automation-id','automation','--output','transaction.json'],capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
        plan=json.loads(Path('transaction.json').read_text())
        self.assertEqual(plan['queue_head'],MAIN);self.assertEqual(len(plan['transactions']),1)
        self.assertEqual(plan['transactions'][0]['expected']['jobs/test.json'],self.q.get('jobs/test.json')[1])
        self.assertEqual(Path('transaction.json').stat().st_mode&0o777,0o600)
        self.assertNotIn('analysis-attempts/test.json',self.q.data)
    def test_offline_checkpoint_reports_holds_before_publication_and_local_correction(self):
        begin(self.q,'test','owner',MAIN,'automation')
        before=copy.deepcopy(self.q.data)
        files={k:{'value':v,'sha':self.q.get(k)[1]} for k,v in self.q.data.items()}
        for k in ('results/test.json','outbox/test.json'):files[k]={'value':None,'sha':None}
        Path('snapshot.json').write_text(json.dumps({'queue_head':MAIN,'files':files}))
        cp=self.checkpoint();original=cp['analyses']['held']['fact_summary']
        cp['analyses']['held']['fact_summary']='원문에 없는 피해액 987654321원이다.'
        script=Path(__file__).with_name('risk_review_session.py')
        plans=[]
        for i,text in enumerate((cp['analyses']['held']['fact_summary'],original)):
            cp['analyses']['held']['fact_summary']=text
            Path('checkpoint.json').write_text(json.dumps(cp))
            run=subprocess.run([sys.executable,str(script),'snapshot.json','checkpoint',
                '--job-id','test','--attempt-id','owner','--checkpoint','checkpoint.json',
                '--output',f'plan-{i}.json'],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(json.loads(run.stdout)['validation_hold_count'],1-i)
            plans.append(json.loads(Path(f'plan-{i}.json').read_text()))
        self.assertEqual(plans[0]['value']['validation_summary']['held_articles'],
                         {'held':'원문에 없는 수치: fact_summary'})
        self.assertEqual(plans[1]['value']['validation_summary']['held_articles'],{})
        self.assertEqual(self.q.data,before)
        self.assertNotIn('results/test.json',self.q.data)
    def test_optional_artifact_failure_cannot_block_native_delivery(self):
        self.publish();original=self.q.put_many
        def fail(values,expected,deletions=()):
            if any(k.startswith(('metrics/','filter-logs/','previews/','logs/')) for k in values):
                raise RuntimeError('diagnostic storage outage')
            return original(values,expected,deletions)
        self.q.put_many=fail;smtp=SMTP();self.consume(smtp)
        self.assertEqual(len(smtp.calls),3);self.assertEqual(self.q.data['jobs/test.json']['status'],'delivery_complete')
        self.assertTrue(Path('risk_audit_spool.jsonl').exists())
    def test_actions_defers_private_artifacts_until_after_ack(self):
        self.publish();smtp=SMTP()
        with patch.dict(os.environ,RISK_DEFER_DIAGNOSTICS='true'):self.consume(smtp)
        self.assertNotIn('metrics/test.json',self.q.data)
        with contextlib.redirect_stdout(io.StringIO()):self.native.ack(self.q)
        self.assertTrue(self.q.data['jobs/test.json']['ack'])
        from risk_diagnostics import persist
        document=json.loads(Path('risk_diagnostic_artifacts.json').read_text())
        self.assertTrue(persist(self.q,'test',document['values']))
        self.assertIn('metrics/test.json',self.q.data);self.assertEqual(len(smtp.calls),3)
    def test_verified_body_cannot_be_relabelled_unavailable(self):
        self.held(None,'본문 없음',unavailable_basis(self.j['body_manifest'][1]))
        with self.assertRaisesRegex(ValueError,'상태 근거'):self.publish()
    def test_checkpoint_binds_mechanical_hash_without_rewriting_wrong_hash(self):
        basis=self.body_basis();basis.pop('source_hash')
        self.held(False,'기존 기준의 정상 제외',basis)
        result=self.publish()
        self.assertEqual(result['classifications'][1]['decision_basis'],self.body_basis())
        from risk_review_evidence import bind_sources
        cp=self.checkpoint();cp['classifications'][1]['decision_basis']['source_hash']='0'*64
        with self.assertRaisesRegex(ValueError,'해시 불일치'):
            analysis_plan(self.j,[bind_sources(cp,self.j)],body_ids=[])
    def test_malformed_group_rejected_before_immutable_write_then_corrected(self):
        begin(self.q,'test','owner',MAIN,'automation');before=copy.deepcopy(self.q.data)
        cp=self.checkpoint();cp['event_groups']=[{'article_ids':['one','held'],'reason':'같은 원문 사건으로 확인했다.'}]
        with self.assertRaisesRegex(Exception,'reviewed'):save_checkpoint(self.q,'test','owner',cp)
        self.assertEqual(self.q.data,before)
        cp['event_groups'][0]['reviewed']=True
        save_checkpoint(self.q,'test','owner',cp);finalize(self.q,'test','owner')
        smtp=SMTP();self.consume(smtp);self.assertEqual(len(smtp.calls),3)
    def test_final_group_only_checkpoint_reuses_all_article_decisions(self):
        begin(self.q,'test','owner',MAIN,'automation')
        save_checkpoint(self.q,'test','owner',self.checkpoint(['one']))
        save_checkpoint(self.q,'test','owner',self.checkpoint(['held']))
        refs=copy.deepcopy(self.q.data['analysis-attempts/test.json']['checkpoint_refs'])
        before={r['path']:copy.deepcopy(self.q.data[r['path']]) for r in refs}
        cp=self.checkpoint();cp.update(covered_ids=[],classifications=[],analyses={},
                event_groups=[{'article_ids':['one','held'],'reason':'같은 원문 사건으로 확인했다.','reviewed':True}])
        save_checkpoint(self.q,'test','owner',cp);result=finalize(self.q,'test','owner')
        self.assertEqual(result['event_groups'],cp['event_groups'])
        self.assertEqual(before,{p:self.q.data[p] for p in before})
        self.assertEqual(self.q.data['analysis-attempts/test.json']['processed_count'],2)
    def test_duplicate_group_across_checkpoints_rejected_without_mutation(self):
        begin(self.q,'test','owner',MAIN,'automation');cp=self.checkpoint()
        cp['event_groups']=[{'article_ids':['one','held'],'reason':'같은 원문 사건으로 확인했다.','reviewed':True}]
        save_checkpoint(self.q,'test','owner',cp);before=copy.deepcopy(self.q.data)
        cp.update(covered_ids=[],classifications=[],analyses={})
        with self.assertRaisesRegex(ValueError,'중복 ID'):save_checkpoint(self.q,'test','owner',cp)
        self.assertEqual(self.q.data,before);finalize(self.q,'test','owner')
    def test_progress_with_only_exclusions_saves_but_never_passes_consume_gate(self):
        begin(self.q,'test','owner',MAIN,'automation');cp=self.checkpoint(['one'])
        body=self.j['body_manifest'][0]['body']
        cp['classifications'][0].update(relevant=False,reason='기존 제외 기준 확인',
                decision_basis={'kind':'body','source_hash':source_hash(body),'quote':body})
        cp['analyses']={}
        save_checkpoint(self.q,'test','owner',cp)
        from risk_review_context import validate_progress
        info=validate_progress(self.j,[cp],'owner')
        self.assertFalse(info['consume_ready']);self.assertEqual(info['unprocessed_ids'],['held'])
        with self.assertRaisesRegex(ValueError,'미처리'):finalize(self.q,'test','owner')
        save_checkpoint(self.q,'test','owner',self.checkpoint(['held']));finalize(self.q,'test','owner')


class ModernDelivery(unittest.TestCase):
    """Exercise existing grouped SMTP/recovery scenarios with the new owner contract."""
    def scenario(self, method):
        native=recovery.ExplicitRejections();native.setUp()
        original=native.grouped
        def grouped():
            queue,smtp,to,cc=original();job=queue.data['jobs/test.json']
            job.update(review_evidence_version=1,delivery_focus_version=2,review_pass_version=2,
                       action_quality_version=2,exclusion_review_version=2,continuity_version=1)
            queue.data.update(fixtures.continuity.freeze_exposure(job,Path('exposure_data.csv').read_bytes(),now().isoformat()))
            result=queue.data.pop('results/test.json')
            cp={k:job[k] for k in ('job_id','input_hash','policy_hash','body_manifest_hash')}
            cp.update(covered_ids=[r['article_id'] for r in result['classifications']],
                      classifications=result['classifications'],analyses=result['analyses'],event_groups=[])
            begin(queue,'test','owner',MAIN,'automation');save_checkpoint(queue,'test','owner',cp);finalize(queue,'test','owner')
            return queue,smtp,to,cc
        native.grouped=grouped
        try:
            with contextlib.redirect_stdout(io.StringIO()):getattr(native,method)()
        finally:native.tearDown()
    def test_grouped_success_with_pre_data_reconnect(self):
        self.scenario('test_mail_421_reconnects_once_and_records_both_attempts')
    def test_explicit_data_rejection_then_same_packet_recovery_ack(self):
        self.scenario('test_data_451_waits_then_recovers_same_packet_and_ack')
    def test_partial_acceptance_retries_only_rejected_recipient(self):
        self.scenario('test_partial_451_retries_only_rejected_recipient')
    def test_data_reply_loss_keeps_uncertain_lock(self):
        self.scenario('test_data_reply_loss_never_acquires_rejection_recovery')
    def test_missing_mandatory_receipt_never_implies_acceptance(self):
        self.scenario('test_receipt_write_failure_cannot_be_inferred_from_smtp_return')


class RetentionAndBody(unittest.TestCase):
    def test_checkpoint_batch_reads_one_head_with_native_queue_adapter(self):
        from risk_queue import Queue
        queue=Queue.__new__(Queue)
        with patch.object(queue,'request',return_value={'object':{'sha':MAIN}}) as request, \
             patch.object(queue,'get',side_effect=lambda path,ref:({'path':path,'ref':ref},'blob-'+path)) as get:
            rows=queue.get_many_fresh(['first','second','first'])
        self.assertEqual(request.call_count,1);self.assertEqual(get.call_count,2)
        self.assertEqual(set(rows),{'first','second'})
        self.assertTrue(all(value[0]['ref']==MAIN for value in rows.values()))
    def test_payloads_crossing_250_boundary_use_native_queue_delete_guard(self):
        from risk_queue import Queue
        queue=hardening.Retention().fixture()
        # 249 shards + 3 payloads straddle the 250-delete limit.
        for n in range(247):queue.data[f'inputs/old/body-{n:04}.json']={'synthetic':n}
        adapter=Queue.__new__(Queue);original=queue.put_many
        def put(values,expected,deletions=()):
            def api(suffix,method='GET',data=None):
                if suffix.startswith('/git/ref/'):return {'object':{'sha':MAIN}}
                if method=='GET':return {'tree':{'sha':'tree'}}
                return {'sha':'written'}
            with patch.object(adapter,'request',side_effect=api),patch.object(adapter,'file_shas',return_value=expected):
                self.assertEqual(adapter.put_many(values,expected,deletions),'written')
            return original(values,expected,deletions)
        queue.put_many=put
        bridge.prune_terminal_inputs(queue)
        self.assertEqual([len(deleted) for _,deleted in queue.commits],[249,3])
        self.assertNotIn('inputs_pruned_at',queue.commits[0][0]['jobs/old.json'])
        self.assertEqual(queue.data['jobs/old.json']['inputs_pruned_files'],252)
    def test_tip_board_is_not_article_and_mention_is_not_blocked(self):
        text='단박제보 단박제보 제목 작성자 날짜 결과 제보 내용이다. 단박접수 추가자료요청 게시완료'
        self.assertEqual(body_quality(text),('unreadable','body_tip_board_listing'))
        self.assertEqual(body_quality('회사는 단박제보 게시판 운영을 중단했다고 밝혔다.')[0],'verified')
    def test_large_terminal_cleanup_resumes_after_second_batch_failure(self):
        q=hardening.Retention().fixture();j=copy.deepcopy(q.data['jobs/old.json'])
        for n in range(501):q.data[f'inputs/old/body-{n:04}.json']={'synthetic':n}
        original=q.put_many;calls=[]
        def fail(values,expected,deletions=()):
            calls.append(len(deletions))
            if len(calls)==2:raise RuntimeError('middle failure')
            return original(values,expected,deletions)
        q.put_many=fail
        with self.assertRaises(RuntimeError):bridge.prune_terminal_inputs(q)
        self.assertNotIn('inputs_pruned_at',q.data['jobs/old.json'])
        self.assertEqual(q.data['jobs/old.json']['inputs_pruning']['files'],250)
        q.put_many=original;bridge.prune_terminal_inputs(q)
        self.assertEqual(q.data['jobs/old.json']['inputs_pruned_files'],506)
        self.assertTrue(all(len(d)<=250 for _,d in q.commits))
        for k in ['status','receipts','ack','history_saved','expected_receipts']:
            self.assertEqual(q.data['jobs/old.json'][k],j[k])
        count=len(q.commits);bridge.prune_terminal_inputs(q);self.assertEqual(len(q.commits),count)
    def test_ack_cleanup_is_bounded_and_failure_keeps_ack(self):
        native=fixtures.NativeContinuity();native.setUp()
        try:
            q=native.streamlined();smtp=SMTP()
            with contextlib.redirect_stdout(io.StringIO()):native.run_consume(q,smtp)
            with patch.object(bridge,'prune_articles',side_effect=RuntimeError('cleanup unavailable')) as prune,contextlib.redirect_stdout(io.StringIO()):native.ack(q)
            prune.assert_called_once_with(q,max_batches=1)
            self.assertTrue(q.data['jobs/test.json']['ack'])
        finally:native.tearDown()


if __name__=='__main__':unittest.main()
