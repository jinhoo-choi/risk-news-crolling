"""Offline regressions for same-event recaps, scope and delivery-state safety."""
import ast
import contextlib
from datetime import datetime, timedelta
import io
import json
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

from event_history import EventHistory, HISTORY_KEY, KST, material_facts, is_background_recap

# No keys, SMTP, models or quote endpoints are used by this suite.
for key in ("EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER", "ANTHROPIC_API_KEY",
            "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"):
    os.environ.setdefault(key, "test@example.com" if "EMAIL" in key else "offline")
with contextlib.redirect_stdout(io.StringIO()):
    import naver_news_monitor as nm

NOW = datetime.now(KST)
GEUM_TITLE = "금양, 거래정지 속 1000억원 '주식매수청구권' 허들"
GEUM_DESC = ("2년 연속 감사의견 거절로 상장폐지 문턱에 선 금양의 소액주주들이 진퇴양난에 빠졌습니다. "
             "회사가 핵심사업을 떼어내는 물적분할을 강행하면서 주식매수청구권이라는 탈출구가 열렸지만")


class EvidenceTests(unittest.TestCase):
    def test_real_geum_background_does_not_open_new_stage(self):
        self.assertEqual(material_facts(GEUM_TITLE, GEUM_DESC), [])
        self.assertTrue(is_background_recap(GEUM_TITLE, GEUM_DESC))

    def test_background_variants_and_bare_keywords(self):
        for title, desc in [
            ("다원시스, 물적분할 추진", "2년 연속 의견거절로 상장폐지 문턱에 섰다"),
            ("금양, 소액주주 진퇴양난", "상장폐지 효력정지 가처분 계류 중이다"),
            ("금양, 전자투표 진행", "감사의견 거절 및 과징금과 관련한 설명이다"),
            ("금양, 과징금 여파", "지난 7일 과징금 76억원을 부과했다"),
            ("금양, 과징금 여파", "지난 7일 금융위, 금양 과징금 76억원을 부과했다"),
        ]:
            self.assertFalse(material_facts(title, desc), (title, desc))

    def test_new_decision_in_lead_survives_separate_background(self):
        facts = material_facts("금양 관련 속보", "지난해 감사의견 거절을 받았다. 법원은 오늘 효력정지 가처분을 기각했다.")
        self.assertIn("가처분기각", [f["stage"] for f in facts])

    def test_injunction_polarity(self):
        self.assertTrue(material_facts("금양, 상장폐지 효력정지 가처분 기각"))
        self.assertTrue(material_facts("다원시스, 이의신청 기각…정리매매 개시"))
        self.assertFalse(material_facts("금양, 상장폐지 효력정지 가처분 인용"))
        self.assertFalse(material_facts("금양, 과징금 부과 취소"))

    def test_resolved_clause_does_not_hide_other_adverse_decision(self):
        facts = material_facts("금양 거래정지 해제 곧바로 정리매매 개시")
        self.assertIn("정리매매개시", [f["stage"] for f in facts])
        self.assertNotIn("거래정지", [f["stage"] for f in facts])
        self.assertTrue(material_facts("금양 거래재개, 추가 과징금 120억원 부과"))

    def test_material_financial_change(self):
        for title in ["금양, 주식매수청구권 1000억원 지급의무 확정",
                      "금양, 1000억원 주식매수청구권 부담 현실화",
                      "금양, 채무 500억원 증가", "금양, 전단채 400억원 미상환"]:
            self.assertTrue(material_facts(title), title)
            self.assertFalse(is_background_recap(title), title)
        self.assertFalse(is_background_recap("금양, 신규 사업 유동성 위기"))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.history = EventHistory(now=NOW)

    def test_market_statistics_reference_dates_persist_without_company_attribution(self):
        statistic = {'entity':'', 'title':'시장 반대매매 통계',
                     '_subscription_stat_series':'market:금투협:미수금반대매매',
                     '_subscription_stat_date':'2026-10-19'}
        self.assertFalse(self.history.duplicate_statistic(statistic))
        self.history.record_statistic(statistic, 'full')
        restored = EventHistory(self.history.serialize(), now=NOW + timedelta(days=10))
        self.assertTrue(restored.duplicate_statistic(dict(statistic, title='다른 매체의 강제청산 보도')))
        self.assertTrue(restored.duplicate_statistic(dict(statistic, _subscription_stat_date='2026-10-16')))
        self.assertTrue(restored.duplicate_statistic(dict(statistic, _subscription_stat_date='')))
        self.assertFalse(restored.duplicate_statistic(dict(statistic, _subscription_stat_date='2026-10-20')))
        self.assertFalse(restored.duplicate_statistic(dict(statistic, _subscription_stat_series='other-statistic')))
        self.assertFalse(restored.duplicate_statistic({'entity':'개별기업', 'event_type':'반대매매'}))
        self.assertEqual(statistic['entity'], '')

    def test_event_type_drift_is_not_novelty(self):
        self.history.record("금양", GEUM_TITLE, GEUM_DESC, "거래정지")
        self.assertTrue(self.history.duplicate("금양", GEUM_TITLE, GEUM_DESC, "감사의견거절"))

    def test_owner_send_does_not_suppress_first_group_send(self):
        title = "금양, 금융위 과징금 76억원 부과"
        self.history.record("금양", title, event_type="금감원제재", scope="self")
        self.assertTrue(self.history.duplicate("금양", title, scope="self"))
        self.assertFalse(self.history.duplicate("금양", title, scope="full"))
        self.history.record("금양", title, scope="full")
        self.assertTrue(self.history.duplicate("금양", title, scope="full"))
        self.assertFalse(self.history.duplicate("금양", "금양, 추가 과징금 120억원 부과", scope="self"))

    def test_event_lives_beyond_url_cache_and_age_seven(self):
        old = EventHistory(now=NOW - timedelta(days=10))
        old.record("금양", GEUM_TITLE, GEUM_DESC)
        restored = EventHistory(old.serialize(), now=NOW)
        self.assertTrue(restored.duplicate("금양", GEUM_TITLE, GEUM_DESC))
        self.assertEqual(restored.age_days("금양", GEUM_TITLE, GEUM_DESC), 10)
        self.assertTrue(restored.new_material("금양", "금양, 법원 가처분 기각…정리매매 개시"))
        self.assertFalse(restored.duplicate("금양", "금양, 법원 가처분 기각…정리매매 개시"))

    def test_new_primary_risk_not_suppressed_by_old_listing_background(self):
        self.history.record("금양", "금양, 거래정지 속 물적분할 추진")
        for title in ["금양, 1000억원 주식매수청구권 부담 현실화",
                      "금양, 신규 사업 유동성 위기", "금양, 공급계약 해지로 새 위험",
                      "금양, 추가 과징금 120억원 부과"]:
            self.assertFalse(self.history.duplicate("금양", title, GEUM_DESC), title)

    def test_unknown_new_money_fails_open(self):
        self.history.record("금양", GEUM_TITLE, GEUM_DESC)
        self.assertFalse(self.history.duplicate("금양", GEUM_TITLE.replace("1000", "2000"), GEUM_DESC))

    def test_amount_equivalence_and_material_delta(self):
        title = "금양, 과징금 76억3300만원 부과"
        self.history.record("금양", title)
        self.assertFalse(self.history.new_material("금양", "금양, 과징금 76.33억원 부과"))
        self.assertTrue(self.history.new_material("금양", "금양, 추가 과징금 120억원 부과"))
        self.assertFalse(self.history.new_material("금양", "금양, 과징금 부과"))

    def test_same_amount_different_authority_or_period_is_new(self):
        self.history.record("금양", "금융위 금양 10억원 과징금 부과")
        self.assertFalse(self.history.duplicate("금양", "공정위 금양 10억원 과징금 부과"))
        self.assertTrue(self.history.new_material("금양", "공정위 금양 10억원 과징금 부과"))
        self.history.record("금양", "금양 2025사업연도 감사의견 거절")
        self.assertTrue(self.history.new_material("금양", "금양 2026사업연도 감사의견 거절"))
        self.assertTrue(self.history.new_material("금양", "금융위 금양 추가 과징금 10억원 부과"))

    def test_different_explicit_event_date_is_new(self):
        first = EventHistory(now=datetime(2026, 10, 7, 21, tzinfo=KST))
        first.record("금양", "금양, 10월 7일 과징금 76억원 부과")
        history = EventHistory(first.serialize(), now=datetime(2026, 10, 11, 21, tzinfo=KST))
        self.assertFalse(history.new_material("금양", "금양 10월7일 과징금 76억원 부과"))
        self.assertTrue(history.new_material("금양", "금양, 10월 11일 과징금 76억원 부과"))

    def test_expiration_and_invalid_state_fail_open(self):
        old = EventHistory(now=NOW - timedelta(days=181))
        old.record("금양", GEUM_TITLE)
        self.assertEqual(EventHistory(old.serialize(), now=NOW).records, [])
        for invalid in [[], "bad", {"records": "bad"}, {"records": [None, {}, {"facts": []}]}]:
            self.assertEqual(EventHistory(invalid).records, [])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(nm, "SEEN_FILE", os.path.join(self.tmp.name, "seen.json"))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_no_news_save_preserves_ledger_but_prunes_old_urls(self):
        history = EventHistory(now=NOW - timedelta(days=10))
        history.record("금양", GEUM_TITLE)
        old_key = (NOW - timedelta(days=10)).strftime("%Y-%m-%d %H")
        with open(nm.SEEN_FILE, "w") as f:
            json.dump({HISTORY_KEY: history.serialize(), old_key: {"urls": ["old"]}}, f)
        nm.save_seen_urls({"new"})
        with open(nm.SEEN_FILE) as f:
            data = json.load(f)
        self.assertNotIn(old_key, data)
        self.assertEqual(nm.load_seen_urls(), {"new"})
        self.assertTrue(nm.load_event_history().duplicate("금양", GEUM_TITLE))

    def test_atomic_save_failure_preserves_prior_history(self):
        history = EventHistory(now=NOW)
        history.record("금양", GEUM_TITLE, scope="self")
        nm.save_seen_urls({"old"}, event_history=history)
        with open(nm.SEEN_FILE) as f:
            before = f.read()
        with patch("os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                nm.save_seen_urls({"new"}, event_history=EventHistory())
        with open(nm.SEEN_FILE) as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.listdir(self.tmp.name), ["seen.json"])

    def test_actual_main_stage_exemption_and_known_case_cap(self):
        with open(nm.__file__, encoding="utf-8") as source:
            tree = ast.parse(source.read())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        stage = next(n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == "is_next_stage")
        env = dict(nm.__dict__, event_history=EventHistory(now=NOW), seen_stages={("금양", "과징금")})
        exec(compile(ast.fix_missing_locations(ast.Module(body=[stage], type_ignores=[])), "actual-stage", "exec"), env)
        self.assertFalse(env["is_next_stage"](GEUM_TITLE, GEUM_DESC, "금양"))
        self.assertTrue(env["is_next_stage"]("금양 120억원 추가 과징금 부과", "", "금양"))
        # (2026-10-12) 확연한 진전만 인정: 절차성 단계·금액만 다른 동일 단계는 제외
        self.assertFalse(env["is_next_stage"]("금양, 상장폐지 효력정지 가처분 신청", "", "금양"))
        self.assertFalse(env["is_next_stage"]("금양 매매거래정지", "", "금양"))
        self.assertFalse(env["is_next_stage"]("금양 120억원 과징금 부과", "", "금양"))
        self.assertTrue(env["is_next_stage"]("금양 정리매매 개시", "", "금양"))
        self.assertTrue(env["is_next_stage"]("금양 파산선고", "", "금양"))
        start = next(i for i, n in enumerate(main.body) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "GRADE_DEMOTE" for t in n.targets))
        end = next(i for i, n in enumerate(main.body) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "ref_after_demote" for t in n.targets))
        cap = ast.FunctionDef(name="cap", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                              body=main.body[start:end], decorator_list=[])
        env.update(_entity_canon={}, exposure_data={}, known_entities={"금양": 3},
                   load_known_case_entities=lambda: {"금양"})
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cap], type_ignores=[])), "actual-cap", "exec"), env)
        # (2026-10-12) 진전 없는 기지 사건 재조명은 참고가 아니라 제외(None)
        for title, desc, expected in [(GEUM_TITLE, GEUM_DESC, None),
                                       ("금양, 주식매수청구권 1000억원 지급의무 확정", GEUM_DESC, "긴급"),
                                       ("금양, 신규 사업 유동성 위기", GEUM_DESC, "긴급")]:
            article = dict(entity="금양", title=title, desc=desc, grade="긴급")
            env["filtered"] = [article]
            with contextlib.redirect_stdout(io.StringIO()):
                env["cap"]()
            self.assertEqual(article["grade"] if env["filtered"] else None, expected, title)

    def test_actual_main_dedup_preserves_punctuated_material_changes(self):
        from difflib import SequenceMatcher
        with open(nm.__file__, encoding="utf-8") as source:
            tree = ast.parse(source.read())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        start = next(i for i, n in enumerate(main.body) if isinstance(n, ast.FunctionDef)
                     and n.name == "_stage_hits")
        end = next(i for i, n in enumerate(main.body) if isinstance(n, ast.Assign)
                   and isinstance(n.value, ast.Name) and n.value.id == "filtered_final")
        body = main.body[start:end+1] + [ast.Return(value=ast.Name(id="filtered", ctx=ast.Load()))]
        gate = ast.FunctionDef(name="gate", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="filtered")],
                              kwonlyargs=[], kw_defaults=[], defaults=[]), body=body, decorator_list=[])
        code = compile(ast.fix_missing_locations(ast.Module(body=[gate], type_ignores=[])), "actual-dedup-gate", "exec")
        cases = [
            ("금융위, 금양 과징금 10억원 부과", "공정위, 금양 과징금 10억원 부과", "금감원제재"),
            ("금양 과징금 1,000억원 부과", "금양 과징금 2,000억원 부과", "금감원제재"),
            ("금양 서울중앙지법 가처분 기각", "금양 서울고법 가처분 기각", "상장폐지"),
            ("금융위…금양 과징금 10억원 부과", "공정위…금양 과징금 10억원 부과", "금감원제재"),
            ("금양 가처분 기각", "금양 가처분 항고 기각", "상장폐지"),
            ("금양 서울중앙지법 가처분 기각", "금양 과징금 여파…서울고법 가처분 기각", "상장폐지"),
            ("금양 거래정지", "금양 거래정지 상태, 법원 회생절차 개시 결정", "거래정지"),
            ("금양 2025년 감사의견 거절", "금양 2026년 감사의견 거절", "감사의견거절"),
            ("금양 2025년 회계연도 감사의견 거절", "금양 2026년 회계연도 감사의견 거절", "감사의견거절"),
        ]
        for previous, changed, event_type in cases:
            with self.subTest(previous=previous, changed=changed):
                history = EventHistory(now=NOW)
                history.record("금양", previous, event_type=event_type)
                env = dict(nm.__dict__, event_history=history, exposure_data={},
                           seen_stages={("금양", f["stage"]) for f in material_facts(previous)},
                           seen_context={"title_norms": [previous], "desc_norms": []},
                           seen_entities_today={"금양"},
                           seen_combos={("금양", event_type), ("ek", f"금양_{event_type}")},
                           load_entity_canonical_map=lambda: {}, _norm=lambda t: t,
                           _sim=lambda a, b: SequenceMatcher(None, a, b).ratio())
                exec(code, env)
                self.assertTrue(history.new_material("금양", changed))
                self.assertFalse(history.duplicate("금양", changed))
                # (2026-10-12) 같은 단계의 금액만 바뀐 보도는 '확연한 진전' 아님 → 제외(사용자 결정).
                #   새 기관·법원·회계연도는 계속 진전으로 인정한다.
                _amount_only = changed == "금양 과징금 2,000억원 부과"
                for title, expected_count in [(previous, 0), (changed, 0 if _amount_only else 1)]:
                    article = dict(entity="금양", title=title, desc="", event_type=event_type, grade="긴급")
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(len(env["gate"]([article])), expected_count, title)
        # Spelling/format changes alone do not reopen the same source event.
        history = EventHistory(now=NOW)
        history.record("금양", "금양 서울중앙지법 가처분 기각")
        self.assertFalse(history.new_material("금양", "금양 서울중앙지방법원 가처분 기각"))
        history.record("금양", "금양 2025사업연도 감사의견 거절")
        self.assertFalse(history.new_material("금양", "금양 2025년 감사의견 거절"))
        dated = material_facts("금양 2026년10월11일 보도 2025년 감사의견 거절",
                               now=datetime(2026, 10, 11, 21, tzinfo=KST))  # 실시각 의존 제거(10-13)
        self.assertNotIn("회계연도:2026", dated[0]["identities"])
        history.record("금양", "금융위, 금양 과징금 1,000억원 부과")
        self.assertFalse(history.new_material("금양", "금융위원회, 금양 과징금 1000억원 부과"))

    def test_actual_owner_filter_runs_before_summary(self):
        with open(nm.__file__, encoding="utf-8") as source:
            tree = ast.parse(source.read())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        start = next(i for i, n in enumerate(main.body) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "_scope" for t in n.targets))
        end = next(i for i, n in enumerate(main.body) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "_summary_src" for t in n.targets))
        body = main.body[start:end+1] + [ast.Return(value=ast.Name(id="_summary_src", ctx=ast.Load()))]
        gate = ast.FunctionDef(name="gate", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                              body=body, decorator_list=[])
        code = compile(ast.fix_missing_locations(ast.Module(body=[gate], type_ignores=[])), "actual-owner-gate", "exec")
        history = EventHistory(now=NOW)
        history.record("금양", GEUM_TITLE, GEUM_DESC, scope="self")
        article = dict(entity="금양", title=GEUM_TITLE, desc=GEUM_DESC, url="new-url", grade="참고")
        for self_only, alerted in [(True, 0), (True, 1), (False, 0)]:
            empty = []
            env = dict(nm.__dict__, event_history=history, filtered=[article], _entity_canon={}, exposure_data={},
                       ref_date="", FORCE_SELF_ONLY=False, now=NOW, now_str_full="test", seen_urls=set(),
                       raw_articles=[], hard_excluded_articles=[], ai_filtered_articles=[], total_count=1,
                       decide_send_scope=lambda *a: {"self_only": self_only, "market_crash": False, "alerted_count": alerted},
                       filter_articles_for_scope=lambda articles, *a: articles,
                       send_email_no_result=lambda *a: empty.append(a), save_seen_urls=lambda *a: None,
                       save_filter_log=lambda *a: None, save_run_stats=lambda *a: None)
            exec(code, env)
            result = env["gate"]()
            self.assertEqual(bool(empty), self_only and not alerted)
            self.assertEqual(result, None if self_only and not alerted else [] if self_only else [article])

    def test_real_main_send_tail_records_only_delivered_cards(self):
        # Execute the actual production post-send block, not a replica of its
        # algorithm. No SMTP/API or earlier pipeline stages are invoked.
        with open(nm.__file__, encoding="utf-8") as source:
            tree = ast.parse(source.read())
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        start = next(i for i, node in enumerate(main.body) if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "delivery_complete" for t in node.targets))
        tail = ast.FunctionDef(name="tail", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                               body=main.body[start:], decorator_list=[])
        code = compile(ast.fix_missing_locations(ast.Module(body=[tail], type_ignores=[])), "actual-main-tail", "exec")
        sent = dict(entity="금양", title=GEUM_TITLE, desc=GEUM_DESC, event_type="거래정지", url="sent", grade="참고")
        omitted = dict(entity="다원시스", title="다원시스 상장폐지 결정", url="omitted", grade="참고")
        for self_only, success in [(True, True), (False, True), (False, False)]:
            history = EventHistory(now=NOW)
            saved = []
            env = dict(nm.__dict__, subject="test", html="test", _self_only=self_only,
                       send_email=lambda *a, **k: success, save_run_stats=lambda *a: None,
                       total_count=2, _mail_articles=[sent], filtered=[sent, omitted],
                       _entity_canon={}, exposure_data={}, sent_urls=set(), event_history=history,
                       _stage_hits=lambda t, d: [f["stage"] for f in material_facts(t, d)],
                       _norm=lambda t: t, raw_articles=[], hard_excluded_articles=[], ai_filtered_articles=[],
                       save_filter_log=lambda *a: None,
                       save_seen_urls=lambda *a, **k: saved.append((a, k)))
            exec(code, env)
            with contextlib.redirect_stdout(io.StringIO()):
                env["tail"]()
            self.assertEqual(bool(saved), success)
            self.assertEqual(bool(history.records), success)
            if success:
                self.assertEqual({r["entity"] for r in history.records}, {"금양"})
                self.assertEqual({r["scope"] for r in history.records}, {"self" if self_only else "full"})
                self.assertEqual(bool(saved[0][0][1]), not self_only)

    def test_smtp_partial_success_is_explicit(self):
        class SMTP:
            def __init__(self, *a): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def ehlo(self): pass
            def login(self, *a): pass
            def sendmail(self, sender, targets, message):
                return {targets[0]: (550, b"refused")} if targets[0] == "fail@example.com" else {}
        with patch.object(nm.smtplib, "SMTP_SSL", SMTP), patch.object(nm, "EMAIL_SENDER", "ok@example.com"), \
             patch.object(nm, "EMAIL_RECEIVERS", ["fail@example.com"]), patch.object(nm, "EMAIL_CC", []), \
             patch.object(nm, "_notify_partial_failure"), contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(nm.send_email("test", "test"), False)
        with patch.object(nm.smtplib, "SMTP_SSL", SMTP), patch.object(nm, "NO_RESULT_RECEIVER", "ok@example.com"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(nm.send_email("test", "test", self_only=True), True)

class DatedEventGateTests(unittest.TestCase):
    AS_OF = datetime(2026, 10, 11, 21, tzinfo=KST)
    REAL_TITLE = "[아침을 열며] 중앙그룹의 기업회생 신청이 갖는 의미"
    REAL_DESC = ("2026년 6월 15일 지주사인 중앙홀딩스를 비롯해 JTBC, 콘텐트리중앙, "
                 "메가박스중앙, 중앙피앤아이 등 중앙그룹 5개사는 서울회생법원에 "
                 "기업회생절차 개시를 신청했습니다. JTBC가 6월 12일 206억원")

    @classmethod
    def setUpClass(cls):
        # Compile the actual production dedup and grade-cap blocks, preserving
        # their real ordering and source parser. No AI, quote, or email calls.
        with open(nm.__file__, encoding="utf-8") as source:
            tree = ast.parse(source.read())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        start = next(i for i, n in enumerate(main.body) if isinstance(n, ast.FunctionDef) and n.name == "_stage_hits")
        end = next(i for i, n in enumerate(main.body) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "ref_after_demote" for t in n.targets))
        gate = ast.FunctionDef(name="gate", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="filtered")],
                              kwonlyargs=[], kw_defaults=[], defaults=[]),
                              body=main.body[start:end] + [ast.Return(value=ast.Name(id="filtered", ctx=ast.Load()))],
                              decorator_list=[])
        cls.code = compile(ast.fix_missing_locations(ast.Module(body=[gate], type_ignores=[])), "actual-dated-main-gate", "exec")

    def gate(self, title, desc="", history=None, entity="중앙그룹", now=None, legacy=False):
        from difflib import SequenceMatcher
        history = history or EventHistory(now=now or self.AS_OF)
        env = dict(nm.__dict__, event_history=history, exposure_data={},
                   seen_stages={(entity, "회생신청")} if legacy else set(),
                   seen_context={"title_norms": [], "desc_norms": []},
                   seen_entities_today={entity} if legacy else set(),
                   seen_combos={(entity, "기업회생"), ("ek", f"{entity}_기업회생")} if legacy else set(),
                   load_entity_canonical_map=lambda: {}, load_known_case_entities=lambda: {entity},
                   known_entities={entity: 3}, _norm=lambda t: t,
                   _sim=lambda a, b: SequenceMatcher(None, a, b).ratio())
        exec(self.code, env)
        article = dict(entity=entity, title=title, desc=desc, event_type="기업회생", grade="긴급")
        with contextlib.redirect_stdout(io.StringIO()):
            return env["gate"]([article])

    def test_real_21h_source_is_reference_on_cold_start_and_legacy_duplicate(self):
        self.assertEqual(material_facts(self.REAL_TITLE, self.REAL_DESC, now=self.AS_OF), [])
        self.assertTrue(is_background_recap(self.REAL_TITLE, self.REAL_DESC, now=self.AS_OF))
        self.assertEqual(self.gate(self.REAL_TITLE, self.REAL_DESC), [])  # (2026-10-12) 진전 없는 기지사건 후속 = 제외
        self.assertEqual(self.gate(self.REAL_TITLE, self.REAL_DESC, legacy=True), [])

    def test_absolute_relative_and_cross_field_old_date_scope(self):
        for date in ("2026년 6월 15일", "2026-06-15", "2026.06.15", "6월 15일",
                     "지난 6월 15일", "지난 7일", "작년", "지난해", "지난달", "3일 전"):
            with self.subTest(date=date):
                title = "중앙그룹 기업회생 신청"
                desc = f"중앙그룹은 {date} 기업회생절차 개시를 신청했다."
                self.assertEqual(self.gate(title, desc), [])  # (2026-10-12) 진전 없는 기지사건 후속 = 제외
                self.assertEqual(material_facts(title, desc, now=self.AS_OF), [])
        self.assertEqual(self.gate("중앙그룹 2026년6월15일 기업회생 신청",
                                  "중앙그룹은 기업회생절차 개시를 신청했다."), [])  # (2026-10-12) 진전 없는 기지사건 후속 = 제외
        # An unrelated company's old application must not date the title event.
        self.assertEqual(self.gate("새계열사 기업회생 신청",
                                  "중앙그룹은 2026년6월15일 기업회생절차 개시를 신청했다.",
                                  entity="새계열사")[0]["grade"], "긴급")

    def test_mixed_old_and_fresh_clauses_keep_current_decision(self):
        for desc in (
            "중앙그룹은 2026년6월15일 회생절차 개시를 신청했다. 법원은 오늘 회생절차 개시를 결정했다.",
            "중앙그룹은 2026년6월15일 회생절차 개시를 신청했고 오늘 법원은 회생절차 개시를 결정했다.",
            "중앙그룹은 지난달 회생절차 개시를 신청했고 오늘 법원은 회생절차 개시를 결정했다.",
            "중앙그룹은 작년 회생절차 개시를 신청했고 2026년10월11일 법원은 회생절차 개시를 결정했다.",
        ):
            with self.subTest(desc=desc):
                facts = material_facts("중앙그룹 회생 경과", desc, now=self.AS_OF)
                self.assertEqual([f["stage"] for f in facts], ["회생개시"])
                self.assertEqual(facts[0]["dates"], ["2026-10-11"])
                self.assertEqual(self.gate("중앙그룹 회생 경과", desc, legacy=True)[0]["grade"], "긴급")
        self.assertEqual(self.gate("금양 신규 사업 횡령 발생", "지난달 가처분 기각 결정이 있었다.",
                                  entity="금양")[0]["grade"], "긴급")

    def test_yesterday_delayed_report_overlaps_14h_window(self):
        morning = self.AS_OF.replace(hour=7)
        for date in ("어제", "전날", "지난 10일", "10월 10일", "2026-10-10"):
            with self.subTest(date=date):
                desc = f"중앙그룹은 {date} 기업회생절차 개시를 신청했다."
                self.assertEqual(self.gate("중앙그룹 회생 신청", desc, now=morning)[0]["grade"], "긴급")
                self.assertEqual(self.gate("중앙그룹 회생 신청", desc), [])  # (2026-10-12) 진전 없는 기지사건 후속 = 제외
        # Calendar inference crosses a year boundary without dating last month
        # to last year twice, or treating it as a current action.
        january = datetime(2027, 1, 1, 7, tzinfo=KST)
        facts = material_facts("중앙그룹 지난 12월31일 회생 신청", now=january)
        self.assertEqual(facts[0]["dates"], ["2026-12-31"])

    def test_application_is_not_opening_and_real_order_reopens_old_bad_history(self):
        for text in ("중앙그룹 회생 신청", "중앙그룹 기업회생절차 신청", "중앙그룹 회생절차 개시 신청",
                     "중앙그룹 회생절차 개시를 신청했다"):
            self.assertEqual([f["stage"] for f in material_facts(text, now=self.AS_OF)], ["회생신청"])
        history = EventHistory(now=self.AS_OF)
        history.record("중앙그룹", "중앙그룹 회생 신청")
        self.assertEqual(self.gate("중앙그룹 회생절차 개시를 신청했다", history=history), [])
        self.assertEqual(self.gate("중앙그룹 오늘 회생절차 개시 결정", history=history, legacy=True)[0]["grade"], "긴급")
        # Reproduce the pre-fix persisted error without editing production state.
        history.records[0]["facts"].append({"family": "insolvency", "stage": "회생개시",
                                            "amounts": [], "dates": ["2026년6월15일"], "identities": []})
        self.assertTrue(history.new_material("중앙그룹", "중앙그룹 오늘 회생절차 개시 결정"))
        self.assertEqual(self.gate("중앙그룹 오늘 회생절차 개시 결정", history=history, legacy=True)[0]["grade"], "긴급")

    def test_requested_order_and_rejected_application_are_distinct_stages(self):
        application = EventHistory(now=self.AS_OF)
        application.record("중앙그룹", "중앙그룹 회생 신청")
        for suffix in ("개시 결정 신청", "개시결정을 신청", "개시 신청서 제출", "개시 신청서 접수",
                       "개시를 요청", "개시결정을 요청", "개시 결정 신청했다"):
            with self.subTest(suffix=suffix):
                title = "중앙그룹 회생절차 " + suffix
                self.assertEqual([f["stage"] for f in material_facts(title, now=self.AS_OF)], ["회생신청"])
                self.assertFalse(application.new_material("중앙그룹", title))
                self.assertEqual(self.gate(title, history=application, legacy=True), [])
        for suffix in ("개시신청 기각", "개시 신청을 기각", "개시결정 신청 각하", "기각"):
            with self.subTest(suffix=suffix):
                title = "중앙그룹 회생절차 " + suffix
                self.assertEqual([f["stage"] for f in material_facts(title, now=self.AS_OF)], ["회생기각"])
                self.assertTrue(application.new_material("중앙그룹", title))
                self.assertEqual(self.gate(title, history=application, legacy=True)[0]["grade"], "긴급")
        for stage, suffix in (("회생개시", "개시 결정"), ("회생폐지", "폐지 결정")):
            title = "중앙그룹 법원 회생절차 " + suffix
            self.assertEqual([f["stage"] for f in material_facts(title, now=self.AS_OF)], [stage])
            self.assertEqual(self.gate(title, history=application, legacy=True)[0]["grade"], "긴급")
        # A rejected request to terminate proceedings is not an actual end.
        self.assertEqual(material_facts("중앙그룹 회생절차 폐지신청 기각", now=self.AS_OF), [])

    def test_court_between_opening_and_request_is_still_only_application(self):
        for court in ("법원", "서울회생법원", "서울중앙지법"):
            history = EventHistory(now=self.AS_OF)
            history.record("중앙그룹", f"중앙그룹 {court}에 회생 신청")
            for action in ("개시를", "개시 결정을"):
                for verb in ("신청했다", "요청했다"):
                    title = f"중앙그룹 회생절차 {action} {court}에 {verb}"
                    with self.subTest(title=title):
                        self.assertEqual([f["stage"] for f in material_facts(title, now=self.AS_OF)],
                                         ["회생신청"])
                        self.assertFalse(history.new_material("중앙그룹", title))
                        self.assertEqual(self.gate(title, history=history, legacy=True), [])
            # The court's own order still opens a separate legal stage.
            title = f"중앙그룹 오늘 {court} 회생절차 개시 결정"
            self.assertEqual(self.gate(title, history=history, legacy=True)[0]["grade"], "긴급")
            title = f"중앙그룹 오늘 회생절차 개시 결정 후 {court}에 신청서 제출"
            self.assertIn("회생개시", [f["stage"] for f in material_facts(title, now=self.AS_OF)])
            title = f"중앙그룹 오늘 회생절차 개시를 {court}에 신청했으나 기각"
            self.assertEqual([f["stage"] for f in material_facts(title, now=self.AS_OF)], ["회생기각"])
            self.assertEqual(self.gate(title, history=history, legacy=True)[0]["grade"], "긴급")

    def test_same_sentence_court_order_can_omit_repeated_rehabilitation_noun(self):
        for old in ("2026년6월15일", "지난달"):
            desc = f"중앙그룹은 {old} 회생절차 개시를 신청했고 오늘 법원은 개시 결정을 내렸다."
            facts = material_facts("중앙그룹 회생 경과", desc, now=self.AS_OF)
            self.assertEqual([f["stage"] for f in facts], ["회생개시"])
            self.assertEqual(facts[0]["dates"], ["2026-10-11"])
            self.assertEqual(self.gate("중앙그룹 회생 경과", desc, legacy=True)[0]["grade"], "긴급")
        for desc in (
            "중앙그룹은 6월15일 회생절차 개시를 신청했고 오늘 법원은 개시 결정을 요청했다.",
            "중앙그룹은 6월15일 회생절차 개시를 신청했고 오늘 법원은 개시 결정을 서울회생법원에 요청했다.",
            "중앙그룹은 6월15일 회생절차 개시를 신청했고 오늘 다른회사는 영업 개시 결정을 내렸다.",
            "중앙그룹은 6월15일 회생절차 개시를 신청했다. 오늘 법원은 심리를 개시했다.",
        ):
            self.assertEqual(material_facts("중앙그룹 회생 경과", desc, now=self.AS_OF), [])

    def test_deadline_date_order_and_distinct_company_names_fail_open(self):
        for clause in ("6월15일을 납부기한으로 하는", "6월15일까지 납부해야 하는", "6월15일이 만기인",
                       "6월15일 기준일 자료에 나온", "만기가 6월15일인", "기준일이 6월15일인"):
            with self.subTest(clause=clause):
                desc = f"금양은 {clause} 과징금 10억원 부과 결정을 받았다."
                self.assertTrue(material_facts("금양 과징금 10억원 부과", desc, now=self.AS_OF))
                self.assertEqual(self.gate("금양 과징금 10억원 부과", desc, entity="금양", legacy=True)[0]["grade"], "긴급")
        for company in ("금양홀딩스", "금양에너지", "새금양"):
            desc = f"{company}는 6월15일 과징금 부과 결정을 받았다."
            self.assertEqual(self.gate("금양 과징금 부과", desc, entity="금양", legacy=True)[0]["grade"], "긴급")

    def test_subject_boundaries_are_local_to_the_action_clause(self):
        for desc in (
            "금양은 입장을 내지 않았다. 금양홀딩스는 6월15일 과징금 부과 결정을 받았다.",
            "금양은 입장을 내지 않았다; 금양에너지는 6월15일 과징금 부과 결정을 받았다.",
            "금양은 입장을 내지 않았고, 금양홀딩스는 6월15일 과징금 부과 결정을 받았다.",
            "금양은 입장을 내지 않았다…새금양은 6월15일 과징금 부과 결정을 받았다.",
        ):
            with self.subTest(desc=desc):
                self.assertTrue(material_facts("금양 과징금 부과", desc, now=self.AS_OF))
                self.assertFalse(is_background_recap("금양 과징금 부과", desc, now=self.AS_OF))
                self.assertEqual(self.gate("금양 과징금 부과", desc, entity="금양", legacy=True)[0]["grade"], "긴급")
        # Subject-only punctuation still carries the correct date and authority.
        desc = "금양, 금융위, 6월15일 과징금 10억원 부과 결정을 받았다."
        self.assertEqual(self.gate("금양 과징금 부과", desc, entity="금양"), [])  # (2026-10-12) 진전 없는 기지사건 후속 = 제외

    def test_prospective_stage_does_not_claim_an_order_already_happened(self):
        for title in ("중앙그룹 내일 회생 개시 예정", "중앙그룹 모레 회생 개시", "중앙그룹 다음달 회생 개시 예정",
                      "중앙그룹 2026년10월12일 회생 개시", "중앙그룹 오는12일 회생 개시", "중앙그룹 회생 개시 예정"):
            with self.subTest(title=title):
                self.assertEqual(material_facts(title, now=self.AS_OF), [])
                self.assertEqual(self.gate(title), [])  # (2026-10-12) 진전 없는 기지사건 후속 = 제외
        for title, desc in (
            ("중앙그룹 오늘 회생 개시 결정, 내일 효력", ""),
            ("중앙그룹 오늘 법원이 다음달 회생 개시를 결정했다", ""),
            ("중앙그룹 회생 개시 결정", "법원은 오늘 회생 개시를 결정했다. 내일 효력이 발생할 예정이다."),
            ("중앙그룹 회생 개시 결정", "법원은 오늘 회생 개시를 결정했다. 10월12일부터 효력이 발생한다."),
        ):
            with self.subTest(title=title, desc=desc):
                self.assertTrue(material_facts(title, desc, now=self.AS_OF))
                self.assertEqual(self.gate(title, desc, legacy=True)[0]["grade"], "긴급")

    def test_legacy_date_spelling_does_not_reopen_same_day_event(self):
        history = EventHistory(now=self.AS_OF)
        history.record("중앙그룹", "중앙그룹 오늘 회생절차 개시 결정")
        history.records[0]["facts"][0]["dates"] = ["2026년10월11일"]
        self.assertFalse(history.new_material("중앙그룹", "중앙그룹 2026-10-11 회생절차 개시 결정"))
        self.assertEqual(self.gate("중앙그룹 10월11일 회생절차 개시 결정", history=history), [])

    def test_new_amount_authority_period_and_deadline_are_not_stale_events(self):
        for title, desc in (
            ("금양 공정위 과징금 10억원 부과", "금양은 6월15일 금융위 과징금 10억원 부과 결정을 받았다."),
            ("금양 금융위 과징금 2,000억원 부과", "금양은 6월15일 금융위 과징금 1,000억원 부과 결정을 받았다."),
            ("금양 2026년 감사의견 거절", "금양은 6월15일 2025년 감사의견 거절을 통보받았다."),
            ("금양 과징금 10억원 부과", "금양은 과징금을 오늘 10억원 부과받았다. 납부기한은 6월15일이다."),
            ("금양 과징금 10억원 부과", "금양은 납부기한이 6월15일인 과징금 10억원 부과 결정을 받았다."),
            ("금양 과징금 10억원 부과", "금양은 납부기한이 12월15일인 과징금 10억원 부과 결정을 받았다."),
        ):
            with self.subTest(title=title, desc=desc):
                self.assertEqual(self.gate(title, desc, entity="금양", legacy=True)[0]["grade"], "긴급")


if __name__ == "__main__":
    with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden in offline tests")):
        unittest.main()
