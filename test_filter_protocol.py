"""필터 응답·재시도 회귀. --live-ab는 저장된 정답 기사로 실제 API를 비교한다.

메일·뉴스 수집·운영 상태 저장은 호출하지 않는다. 기본 실행은 API 비용 0.
"""
import contextlib
import copy
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

for key in ("EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER",
            "ANTHROPIC_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"):
    os.environ.setdefault(key, "offline-test")
import naver_news_monitor as M


def positive(article_id, grade="주의"):
    return {"id": article_id, "relevant": True, "grade": grade,
            "entity": f"기업{article_id}", "entities": [f"기업{article_id}"],
            "confidence": .9, "reason": "확정 사건", "event_type": "기업회생"}


def articles(n=2):
    return [{"title": f"기사 {i}", "desc": "", "url": f"https://example.com/{i}"}
            for i in range(n)]


class Response:
    status_code = 200

    def __init__(self, rows, stop="end_turn"):
        self.text = json.dumps(rows, ensure_ascii=False) if not isinstance(rows, str) else rows
        self.stop = stop

    def raise_for_status(self):
        pass

    def json(self):
        return {"content": [{"type": "text", "text": self.text}],
                "stop_reason": self.stop, "usage": {"input_tokens": 1, "output_tokens": 1}}


class FilterProtocolTests(unittest.TestCase):
    def setUp(self):
        M._RUN_STATS.clear()
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.sleep = patch.object(M.time, "sleep", return_value=None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.quiet.__exit__(None, None, None)

    def test_all_false_and_offset_count(self):
        with patch.object(M.requests, "post", return_value=Response([{"id": 0, "seen": 2}])):
            self.assertEqual(M.filter_batch_complete(articles(), 100), [])
        self.assertTrue(M._RUN_STATS["filter_seen_detail"][-1]["complete"])
        self.assertEqual(M._RUN_STATS["filter_seen_detail"][-1]["batch"], 2)

    def test_equal_length_results_union(self):
        first = Response([positive(1)])  # sentinel 없음: 최초 후보는 보존
        second = Response([{"id": 1, "relevant": False}, positive(2), {"id": 0, "seen": 2}])
        with patch.object(M.requests, "post", side_effect=[first, second]):
            result = M.filter_batch_complete(articles())
        self.assertEqual([a["_filter_id"] for a in result], [1, 2])

    def test_retry_does_not_mutate_first_candidate(self):
        batch = articles(1)
        with patch.object(M.requests, "post", side_effect=[
                Response([positive(1, "긴급")]),
                Response([positive(1, "참고"), {"id": 0, "seen": 1}])]):
            result = M.filter_batch_complete(batch)
        self.assertEqual(result[0]["grade"], "긴급")
        self.assertNotIn("grade", batch[0])

    def test_initial_failure_recovers_with_full_response(self):
        def classify(batch, offset=0, full_response=False):
            if not full_response:
                return None
            M._RUN_STATS.setdefault("filter_seen_detail", []).append({"complete": True})
            return [{"_filter_id": 1, "grade": "주의"}]
        with patch.object(M, "ai_filter_batch", side_effect=classify):
            self.assertEqual(len(M.filter_batch_complete(articles(1))), 1)

    def test_retry_none_fails_explicitly(self):
        with patch.object(M, "ai_filter_batch", side_effect=[[{"_filter_id": 1, "grade": "주의"}], None]):
            with self.assertRaisesRegex(RuntimeError, "완전성"):
                M.filter_batch_complete(articles())

    def test_both_calls_fail_explicitly(self):
        with patch.object(M, "ai_filter_batch", return_value=None):
            with self.assertRaises(RuntimeError):
                M.filter_batch_complete(articles())

    def test_incomplete_full_retry_is_not_zero_risk(self):
        with patch.object(M.requests, "post", return_value=Response([{"id": 0, "seen": 118}])):
            with self.assertRaises(RuntimeError):
                M.filter_batch_complete(articles(18), 100)

    def test_invalid_envelopes_and_rows(self):
        bad = [[], [positive(1)], [{"id": 0, "seen": "2"}],
               [{"id": 0, "seen": True}], [{"id": 0, "seen": 2}, positive(1)],
               [{"id": 0, "seen": 2}, {"id": 0, "seen": 2}],
               [positive(1), positive(1), {"id": 0, "seen": 2}],
               [positive(3), {"id": 0, "seen": 2}],
               [positive("1"), {"id": 0, "seen": 2}],
               [positive(True), {"id": 0, "seen": 2}],
               [dict(positive(1), relevant="false"), {"id": 0, "seen": 2}],
               [dict(positive(1), entity=""), {"id": 0, "seen": 2}],
               [dict(positive(1), entities="기업1"), {"id": 0, "seen": 2}],
               [dict(positive(1), grade="unknown"), {"id": 0, "seen": 2}]]
        for rows in bad:
            with self.subTest(rows=rows), patch.object(M.requests, "post", return_value=Response(rows)):
                M.ai_filter_batch(articles())
                self.assertFalse(M._RUN_STATS["filter_seen_detail"][-1]["complete"])

    def test_full_response_checks_every_id(self):
        with patch.object(M.requests, "post", return_value=Response([positive(1), {"id": 0, "seen": 2}])):
            M.ai_filter_batch(articles(), full_response=True)
        self.assertFalse(M._RUN_STATS["filter_seen_detail"][-1]["complete"])

    def test_truncation_and_malformed_json_are_not_repaired(self):
        for response in (Response([positive(1)], "max_tokens"), Response('[{"id":1,')):
            with self.subTest(response=response.text), patch.object(M.requests, "post", return_value=response):
                self.assertIsNone(M.ai_filter_batch(articles()))

    def test_duplicate_id_retains_positive_candidate(self):
        rows = [{"id": 1, "relevant": False}, positive(1), {"id": 0, "seen": 2}]
        with patch.object(M.requests, "post", return_value=Response(rows)):
            result = M.ai_filter_batch(articles())
        self.assertEqual(result[0]["_filter_id"], 1)
        self.assertFalse(M._RUN_STATS["filter_seen_detail"][-1]["complete"])

    def test_protocol_and_metrics_are_connected(self):
        with patch.object(M.requests, "post", return_value=Response([
                {"id": 1, "relevant": False}, {"id": 0, "seen": 1}])) as post:
            M.ai_filter_batch(articles(1), full_response=True)
        request = post.call_args.kwargs["json"]
        self.assertIn('seen', request["system"])
        self.assertNotIn('__FILTER_RESPONSE_CONTRACT__', str(request))
        self.assertIn('모든 기사 id', str(request))
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "stats.jsonl")
            M.save_run_stats(1, 0, M.CLAUDE_MODEL, True, path=path)
            row = json.loads(Path(path).read_text())
        self.assertTrue(row["filter_seen_detail"][0]["complete"])
        self.assertIn("filter_input_count", row)
        self.assertIn("is_test", row)


def live_ab(base_ref, output):
    """API 비교 결과를 파일에 남긴다. 후보 누락/오류는 종료코드 1로 병합을 막는다."""
    if os.environ.get("ANTHROPIC_API_KEY") == "offline-test":
        raise RuntimeError("실제 ANTHROPIC_API_KEY가 필요합니다")
    corpus = json.loads(Path("regression_set.json").read_text())
    positives = [dict(x, expected=True, case_id=f"tp-{i}")
                 for i, x in enumerate(corpus["true_positive_history"])]
    negatives = [dict(x, expected=False, case_id=f"fp-{i}")
                 for i, x in enumerate(corpus["false_positive_history"])]
    batches = []
    for i in range(max(math.ceil(len(positives)/6), math.ceil(len(negatives)/13))):
        batches.append((f"history-{i}", 0, positives[i*6:i*6+6] + negatives[i*13:i*13+13]))
    # 실제 회귀 기사의 저빈도 positive를 배치 양 끝·100 이후 id에서도 확인.
    noise = [dict(negatives[i % len(negatives)], case_id=f"noise-{i}") for i in range(99)]
    batches += [("sparse-first", 0, positives[:1] + noise),
                ("sparse-last-offset", 100, noise + positives[:1])]
    report = {"base_ref": base_ref, "candidate_sha": os.getenv("GITHUB_SHA", "local"),
              "model": M.CLAUDE_FILTER_MODEL, "batches": [], "new_misses": [],
              "errors": [], "price_as_of": "2026-09-26"}
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for name in ("naver_news_monitor.py", "filter_prompt.txt", "known_cases.json", "group_map.json", "ticker_map.json"):
            (root/name).write_bytes(subprocess.check_output(["git", "show", f"{base_ref}:{name}"]))
        spec = importlib.util.spec_from_file_location("risk_baseline", root/"naver_news_monitor.py")
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        baseline.CLAUDE_FILTER_MODEL = M.CLAUDE_FILTER_MODEL
        for label, offset, cases in batches:
            batch = [dict(x, desc=x.get("desc", ""), url=f"https://example.com/{x['case_id']}") for x in cases]
            record = {"name": label, "input_count": len(batch), "offset": offset, "cases": []}
            results = {}
            for name, module in (("baseline", baseline), ("candidate", M)):
                module._RUN_STATS.clear()
                try:
                    fn = module.ai_filter_batch if name == "baseline" else module.filter_batch_complete
                    selected = fn(copy.deepcopy(batch), offset=offset)
                    if selected is None:
                        raise RuntimeError("API/파싱 실패")
                    results[name] = {x["case_id"] for x in selected}
                    record[name] = {"selected": sorted(results[name]), "stats": copy.deepcopy(module._RUN_STATS)}
                except Exception as exc:
                    report["errors"].append({"batch": label, "mode": name, "type": type(exc).__name__})
                    results[name] = set()
                    record[name] = {"error": type(exc).__name__, "stats": copy.deepcopy(module._RUN_STATS)}
            for case in cases:
                row = {"case_id": case["case_id"], "title": case["title"], "expected": case["expected"],
                       "baseline": case["case_id"] in results["baseline"],
                       "candidate": case["case_id"] in results["candidate"]}
                record["cases"].append(row)
                if row["expected"] and row["baseline"] and not row["candidate"]:
                    report["new_misses"].append({"batch": label, **row})
            report["batches"].append(record)
            Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(f"[A/B] {label}: {len(batch)}건 / 기존 {len(results['baseline'])} / 변경 {len(results['candidate'])}", flush=True)
    history = [x for b in report["batches"] if b["name"].startswith("history") for x in b["cases"]]
    report["history_summary"] = {mode: {"tp": sum(x["expected"] and x[mode] for x in history),
                                       "fn": sum(x["expected"] and not x[mode] for x in history),
                                       "fp": sum(not x["expected"] and x[mode] for x in history),
                                       "tn": sum(not x["expected"] and not x[mode] for x in history)}
                                 for mode in ("baseline", "candidate")}
    report["passed"] = not report["new_misses"] and not report["errors"]
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("history_summary", "new_misses", "errors", "passed")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    if "--live-ab" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--live-ab", action="store_true")
        parser.add_argument("--base-ref", required=True)
        parser.add_argument("--output", default="filter_ab_result.json")
        args = parser.parse_args()
        sys.exit(live_ab(args.base_ref, args.output))
    unittest.main()
