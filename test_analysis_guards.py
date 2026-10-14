"""Offline regressions for complete analysis, ledger integrity and KIS evidence.

All model responses are mocks; no SMTP or paid model is called.
"""
import ast
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, Mock

for name in ('EMAIL_SENDER','EMAIL_PASSWORD','EMAIL_RECEIVER','ANTHROPIC_API_KEY',
             'NAVER_CLIENT_ID','NAVER_CLIENT_SECRET'):
    os.environ.setdefault(name, 'offline@example.invalid' if 'EMAIL' in name else 'offline-placeholder')

import naver_news_monitor as bot
from event_history import HISTORY_KEY
from risk_analysis_guard import (AnalysisIncompleteError, HistoryIntegrityError, strict_json,
    validate_grade_map, read_seen_data, validate_seen_data, model_label,
    apply_verified_direct_incident, verified_direct_incident, KST)

NOW = datetime(2026,10,14,10,0,tzinfo=KST)
BATCH = [{'title':'코스피 급락에 이틀간 반대매매 증가','desc':'미수 거래 강제청산 집계'},
         {'title':'제이알글로벌리츠 전자단기사채 미상환','desc':'회생절차 개시 신청'}]
GRADES = [{'id':1,'relevant':True,'grade':'주의','confidence':0.9,'entity':'','entities':[],
           'event_type':'반대매매','reason':'시장 전체 미수 반대매매'},
          {'id':2,'relevant':False}]


class BatchTests(unittest.TestCase):
    def test_complete_market_batch(self):
        self.assertEqual(set(validate_grade_map(GRADES,BATCH)),{1,2})

    def test_all_false_is_complete(self):
        self.assertEqual(len(validate_grade_map([{'id':i,'relevant':False} for i in (1,2)],BATCH)),2)

    def test_legacy_null_action_remains_supported(self):
        entries=[{**GRADES[0],'action':None},GRADES[1]]
        with patch.object(bot.requests,'post',return_value=self.api_response(json.dumps(entries))), contextlib.redirect_stdout(io.StringIO()):
            result=bot.ai_filter_batch(copy.deepcopy(BATCH))
        self.assertEqual(result[0]['action'],'')

    def test_missing_id(self):
        with self.assertRaises(AnalysisIncompleteError): validate_grade_map(GRADES[:1],BATCH)

    def test_duplicate_id(self):
        with self.assertRaises(AnalysisIncompleteError): validate_grade_map([GRADES[0],GRADES[0]],BATCH)

    def test_out_of_range_and_bool_ids(self):
        for identifier in (0,3,True,'1',None):
            with self.subTest(identifier=identifier), self.assertRaises(AnalysisIncompleteError):
                validate_grade_map([{**GRADES[0],'id':identifier},GRADES[1]],BATCH)

    def test_offset_and_alias(self):
        entries=[{**g,'id':g['id']+100} for g in GRADES]
        self.assertEqual(set(validate_grade_map(entries,BATCH,100)),{101,102})
        self.assertEqual(set(validate_grade_map([{'news_id':1,'relevant':False},{'id':2,'relevant':False}],BATCH)),{1,2})
        with self.assertRaises(AnalysisIncompleteError):
            validate_grade_map([{**GRADES[0],'news_id':2},GRADES[1]],BATCH)

    def test_relevant_must_be_boolean(self):
        for value in ('false',1,None):
            with self.subTest(value=value), self.assertRaises(AnalysisIncompleteError):
                validate_grade_map([{**GRADES[0],'relevant':value},GRADES[1]],BATCH)

    def test_non_finite_confidence(self):
        for value in (float('nan'),float('inf'),-0.1,1.1,True,'0.9',None):
            with self.subTest(value=value), self.assertRaises(AnalysisIncompleteError):
                validate_grade_map([{**GRADES[0],'confidence':value},GRADES[1]],BATCH)

    def test_empty_entity_only_for_source_backed_market_events(self):
        for event in ('기업회생','시스템장애','파산부도',''):
            with self.subTest(event=event), self.assertRaises(AnalysisIncompleteError):
                validate_grade_map([{**GRADES[0],'event_type':event},GRADES[1]],BATCH)
        with self.assertRaises(AnalysisIncompleteError):
            validate_grade_map(GRADES,[{'title':'평범한 전망','desc':''},BATCH[1]])
        with self.assertRaises(AnalysisIncompleteError):
            validate_grade_map([{**GRADES[0],'entities':['삼성전자']},GRADES[1]],BATCH)

    def test_strict_json_does_not_repair_incomplete_data(self):
        for value in ('[{"id":1,"id":2}]','[{"x":NaN}]','[{"x":Infinity}]','[{"id":1}', '[{"id":1,}]'):
            with self.subTest(value=value), self.assertRaises(ValueError): strict_json(value)

    def api_response(self, raw):
        response=Mock(status_code=200)
        response.json.return_value={'content':[{'type':'text','text':raw}], 'stop_reason':'end_turn'}
        return response

    def test_actual_parser_keeps_market_entity_empty(self):
        with patch.object(bot.requests,'post',return_value=self.api_response(json.dumps(GRADES))), contextlib.redirect_stdout(io.StringIO()):
            result=bot.ai_filter_batch(copy.deepcopy(BATCH))
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['entity'],'')
        self.assertEqual(result[0]['entities'],[])

    def test_actual_parser_rejects_missing_duplicate_and_truncation(self):
        for raw in (json.dumps(GRADES[:1]),json.dumps([GRADES[0],GRADES[0]]),'[{"id":1}'):
            with self.subTest(raw=raw), patch.object(bot.requests,'post',return_value=self.api_response(raw)), \
                    patch.object(bot.time,'sleep'), contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(bot.ai_filter_batch(copy.deepcopy(BATCH)))

    def test_failed_later_batch_cannot_send_partial_result(self):
        articles=[{'title':'article'} for _ in range(101)]
        with patch.object(bot,'ai_filter_batch',side_effect=[[{'title':'kept'}],None]), \
                patch.object(bot,'regrade_by_score') as regrade, patch.object(bot.time,'sleep'), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(AnalysisIncompleteError):
            bot.ai_filter_and_grade(articles,{})
        regrade.assert_not_called()

    def test_main_stops_before_mail_and_seen_on_incomplete_analysis(self):
        # Existing main is exercised with an incomplete classifier response.
        fixture={'title':'리츠 전자단기사채 미상환','url':'https://example.invalid/risk',
                 'keyword':'부도','pubDate':'Wed, 14 Oct 2026 09:30:00 +0900','desc':'신규 부도 공시','body':''}
        with patch.object(bot,'crawl_naver_news',return_value=[fixture]), \
                patch.object(bot,'KEYWORDS',['부도']), patch.object(bot,'get_overseas_keywords',return_value=[]), \
                patch.object(bot,'is_hard_excluded',return_value=(False,None)), \
                patch.object(bot,'load_exposure_data',return_value={}), \
                patch.object(bot,'ai_filter_and_grade',side_effect=AnalysisIncompleteError('incomplete')), \
                patch.object(bot,'send_email') as smtp, patch.object(bot,'send_email_no_result') as empty, \
                patch.object(bot,'save_seen_urls') as save, contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(AnalysisIncompleteError):
            bot.main()
        smtp.assert_not_called();empty.assert_not_called();save.assert_not_called()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.file=Path(self.tmp.name)/'seen.json'
        self.patcher=patch.object(bot,'SEEN_FILE',str(self.file));self.patcher.start()

    def tearDown(self):
        self.patcher.stop();self.tmp.cleanup()

    def test_valid_current_and_legacy_slots(self):
        key=datetime.now(KST).strftime('%Y-%m-%d %H')
        self.file.write_text(json.dumps({key:['legacy-url']}))
        self.assertEqual(bot.load_seen_urls(),{'legacy-url'})
        self.file.write_text(json.dumps({key:{'urls':['new'],'combos':[['회사','사건']]},
                                         HISTORY_KEY:{'version':1,'records':[]}}))
        self.assertEqual(bot.load_seen_urls(),{'new'})
        self.assertEqual(bot.load_seen_combos(),{('회사','사건')})

    def test_corrupt_file_blocks_all_readers_and_save(self):
        self.file.write_text('{broken')
        before=self.file.read_bytes()
        for function in (bot.load_seen_urls,bot.load_seen_combos,bot.load_seen_stages,
                         bot.load_seen_context,bot.load_seen_entities_today,bot.load_event_history,bot.load_known_entities):
            with self.subTest(reader=function.__name__),self.assertRaises(HistoryIntegrityError):function()
        with self.assertRaises(HistoryIntegrityError):bot.save_seen_urls({'new'})
        self.assertEqual(self.file.read_bytes(),before)
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()],['seen.json'])

    def test_missing_file_blocks_read_but_explicit_initial_save_supported(self):
        with self.assertRaises(HistoryIntegrityError):bot.load_seen_urls()
        bot.save_seen_urls({'new'})
        self.assertEqual(bot.load_seen_urls(),{'new'})

    def test_wrong_slot_types_and_ledger_records(self):
        key='2026-10-14 10'
        bad=[[],{key:{'urls':'url'}},{key:{'combos':[['one']]}},
             {HISTORY_KEY:{'version':1,'records':'bad'}},
             {HISTORY_KEY:{'version':1,'records':[{}]}}]
        for value in bad:
            self.file.write_text(json.dumps(value))
            with self.subTest(value=value),self.assertRaises(HistoryIntegrityError):read_seen_data(self.file)

    def test_duplicate_json_key_is_not_silently_overwritten(self):
        self.file.write_text('{"2026-10-14 10":{},"2026-10-14 10":{}}')
        with self.assertRaises(HistoryIntegrityError):read_seen_data(self.file)

    def test_main_stops_before_collection_on_corrupt_history(self):
        self.file.write_text('{broken')
        with patch.object(bot,'crawl_naver_news') as crawl,patch.object(bot,'send_email') as smtp, \
                patch.object(bot,'send_email_no_result') as empty,contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(HistoryIntegrityError):bot.main()
        crawl.assert_not_called();smtp.assert_not_called();empty.assert_not_called()


class DirectIncidentTests(unittest.TestCase):
    def article(self,body=None,title='증권사 MTS 잇단 장애'):
        body=body or '오늘 오전 한국투자증권 일부 MTS에서 계좌잔고 조회 서비스 장애가 발생했다.'
        return {'title':title,'url':'https://example.invalid/news','body':body,'entity':'한국투자증권',
                'entities':['한국투자증권'],'grade':'참고','_ai_confidence':0.95,'_grade_locked':True,
                '_body_failed':False,'_body_source_url':'https://example.invalid/news',
                '_body_source_hash':hashlib.sha256(body.encode()).hexdigest(),
                '_body_risk_confirmed':True,'_judgment':{'당사연관':'당사이슈','확정여부':'확정'}}

    def test_body_only_dated_kis_incident_unlocks_urgent_without_exposure(self):
        article=self.article()
        with contextlib.redirect_stdout(io.StringIO()):result=bot.regrade_by_score([article],{},NOW)
        self.assertEqual(result[0]['grade'],'긴급')
        self.assertTrue(result[0]['_force_urgent'])
        self.assertNotIn('_grade_locked',result[0])
        self.assertIn(result[0]['_verified_direct_incident']['quote'],article['body'])

    def test_real_march_article_anchored_to_its_date(self):
        # YNA 2026-03-06: https://www.yna.co.kr/view/AKR20260305170400002
        body='6일 금융투자업계와 금융당국에 따르면 금감원은 전날 오전 한국투자증권 일부 모바일트레이딩시스템(MTS)에서 계좌잔고 조회 서비스 장애가 생겨 해당 발생 원인을 파악 중이다.'
        article=self.article(body)
        then=datetime(2026,3,6,7,0,tzinfo=KST)
        self.assertTrue(apply_verified_direct_incident(article,then))
        self.assertEqual(article['grade'],'긴급')
        # Replaying an old article today must not reopen it as a new incident.
        article=self.article('지난 3월 5일 한국투자증권 MTS에서 잔고 조회 오류가 발생했다.')
        self.assertFalse(apply_verified_direct_incident(article,NOW))
        self.assertEqual(article['grade'],'참고')

    def test_full_name_retrospective_cannot_force_urgent(self):
        body='지난 3월 5일 한국투자증권 MTS에서 계좌 잔고 표시 오류가 발생했다.'
        article=self.article(body,'한국투자증권 과거 전산사고 재조명')
        article.update(grade='긴급',_force_urgent=True)
        with contextlib.redirect_stdout(io.StringIO()):result=bot.regrade_by_score([article],{},NOW)
        self.assertEqual(result[0]['grade'],'참고')
        self.assertFalse(result[0]['_force_urgent'])

    def test_kis_alias_exact_and_other_company_boundaries(self):
        article=self.article('오늘 오전 한투증권 MTS에서 계좌잔고 조회 장애가 발생했다.')
        article['entity']='한투증권'
        self.assertTrue(apply_verified_direct_incident(article,NOW))
        for name in ('신한투자증권','대한투자증권'):
            article=self.article(f'오늘 오전 {name} MTS에서 잔고 조회 장애가 발생했다.')
            with self.subTest(name=name):self.assertIsNone(verified_direct_incident(article,NOW))

    def test_missing_unverified_or_changed_body_cannot_unlock(self):
        for changes in ({'_body_failed':True},{'_body_source_url':'https://example.invalid/other'},
                        {'_body_source_hash':'0'*64},{'_body_risk_confirmed':False},
                        {'_judgment':{'당사연관':'무관','확정여부':'확정'}},
                        {'body':'오늘 오전 한국투자증권 MTS 오류 발생. 추가 텍스트'}):
            article=self.article();article.update(changes)
            with self.subTest(changes=changes):self.assertIsNone(verified_direct_incident(article,NOW))

    def test_unknown_future_negated_and_mention_only_cases(self):
        cases=['한국투자증권 일부 MTS에서 잔고 조회 장애가 발생했다.',
               '내일 한국투자증권 MTS에서 모의 장애 훈련이 예정돼 있다.',
               '오늘 한국투자증권 MTS에는 장애가 없었다.',
               '오늘 신한투자증권 MTS 장애에 대해 한국투자증권은 정상 운영 중이라고 설명했다.',
               '오늘 한국투자증권은 신한투자증권 MTS 장애를 리서치에 언급했다.']
        for body in cases:
            with self.subTest(body=body):self.assertIsNone(verified_direct_incident(self.article(body),NOW))

    def test_new_event_after_recap_is_still_new(self):
        body='지난 3월 5일 한국투자증권 MTS에서 오류가 발생했다. 오늘 오전 한국투자증권 MTS에서 새로운 잔고 조회 장애가 발생했다.'
        article=self.article(body)
        self.assertTrue(apply_verified_direct_incident(article,NOW))

    def test_cached_annotation_cannot_survive_source_change(self):
        article=self.article();self.assertTrue(apply_verified_direct_incident(article,NOW))
        article['body']='제목 외 본문 확인 불가'
        self.assertFalse(apply_verified_direct_incident(article,NOW))
        self.assertNotIn('_verified_direct_incident',article)
        self.assertFalse(article['_force_urgent'])

    def test_real_runtime_hooks_apply_evidence_after_body_review(self):
        tree=ast.parse(Path(bot.__file__).read_text())
        main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
        calls=[n for n in ast.walk(main) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
               and n.func.id=='apply_verified_direct_incident']
        self.assertEqual(len(calls),1)
        self.assertIn('now_kst',ast.unparse(calls[0]))


class ModelTests(unittest.TestCase):
    def test_existing_claude_labels(self):
        self.assertEqual(model_label('claude-opus-4-6'),'Claude Opus')
        self.assertEqual(model_label('claude-sonnet-4-6'),'Claude Sonnet')

    def test_subscription_unconfirmed_and_actual_model(self):
        self.assertEqual(model_label('unconfirmed','chatgpt_subscription'),'ChatGPT 구독 · 모델 미확인')
        self.assertEqual(model_label('confirmed-name','chatgpt_subscription'),'ChatGPT 구독 · confirmed-name')

    def test_actual_label_cannot_fabricate_claude(self):
        with patch.object(bot,'_LAST_VERIFY_MODEL','unconfirmed'):
            self.assertEqual(bot._model_label(),'AI 모델 미확인')
        with patch.object(bot,'_LAST_VERIFY_MODEL','unconfirmed'), \
                patch.object(bot,'_LAST_ANALYSIS_ENGINE','chatgpt_subscription',create=True):
            self.assertEqual(bot._model_label(),'ChatGPT 구독 · 모델 미확인')


if __name__=='__main__':unittest.main(verbosity=2)
