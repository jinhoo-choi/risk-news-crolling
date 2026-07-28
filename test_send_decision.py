"""발송판정 자동 검증 — 오늘(2026-07-28) 급락장 미발송 사고의 재발 방지.

배경: 변형 테스트(test_variants.py)는 is_hard_excluded 한 함수만 검증했다.
발송 범위 판정·등급 조정·점수 계산은 자동 검증 대상이 아니었고, 실제로
그 사각지대에서 사고가 났다.
  · 시장급락 안전장치가 집계값을 판정 시점에 읽지 못해 항상 미발동
    (build_email_html 내부 side effect에 의존 → 호출 순서 변경으로 붕괴)

이 테스트는 '발송 여부를 결정하는 전 경로'를 진리표로 고정한다.
실패 시 종료코드 1.
"""
import sys, types, os, io, contextlib
import pandas as pd

# yfinance 모의 — 전 종목 -11% 급락 시나리오를 만들 수 있게 한다
_CRASH = {"on": False}


class _FakeTicker:
    def __init__(self, tk):
        self.tk = tk

    def history(self, period=None, interval=None, auto_adjust=None):
        if not _CRASH["on"]:
            return pd.DataFrame()
        from datetime import datetime, timezone, timedelta
        kst = timezone(timedelta(hours=9))
        today = datetime.now(kst).strftime("%Y-%m-%d")
        idx = pd.to_datetime([f"{today} 09:00", f"{today} 15:30"]).tz_localize("Asia/Seoul")
        return pd.DataFrame({"Close": [10000, 8900]}, index=idx)


_fake = types.ModuleType("yfinance")
_fake.Ticker = _FakeTicker
sys.modules["yfinance"] = _fake

for _k in ["EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER",
           "ANTHROPIC_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"]:
    os.environ.setdefault(_k, "x@t.com" if "EMAIL" in _k else "x")

import importlib.util
_spec = importlib.util.spec_from_file_location("nm", "naver_news_monitor.py")
nm = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(nm)
except SystemExit:
    pass

with contextlib.redirect_stdout(io.StringIO()):
    EXPO = nm.load_exposure_data()


def _exposure(entity):
    if not entity:
        return 0.0
    return sum(nm._num(r.get("잔고(억)")) for r in nm.find_exposure(entity, EXPO))


def decide(articles, crash=False):
    """★운영과 동일한 함수를 호출한다 (로직 복제 금지).

    이전 버전은 판정 로직을 테스트에서 재현했는데, 변이 테스트 결과
    'main의 시장급락 집계 호출을 지워도 테스트가 통과'하는 것이 확인됐다.
    재현본을 검사하면 실제 코드가 깨져도 잡지 못한다.
    → decide_send_scope() / filter_articles_for_scope()를 직접 호출한다.
    """
    _CRASH["on"] = crash
    # 시세 조건을 바꿔가며 검증하므로 회차 캐시를 매번 비운다
    nm.clear_price_alert_cache()

    with contextlib.redirect_stdout(io.StringIO()):
        sc = nm.decide_send_scope(articles, EXPO, "2026-07-27")
        mail = nm.filter_articles_for_scope(articles, EXPO, sc["self_only"])

    return {"full": not sc["self_only"], "max": sc["max_score"],
            "crash": sc["market_crash"], "cnt": sc["alerted_count"],
            "rbal": sc["alerted_rbal"], "mail": mail}


def A(grade, score, conf=0.5, entity="삼성전자"):
    return {"grade": grade, "_risk_score": score, "_ai_confidence": conf, "entity": entity}


CASES = [
    # (설명, 기사목록, 급락여부, 전체발송 기대)
    ("긴급 존재 — 점수 낮아도 전체발송", [A("긴급", 3.0)], False, True),
    ("주의 5.5 경계 — 전체발송", [A("주의", 5.5)], False, True),
    ("주의 5.4 경계직하 — 본인한정", [A("주의", 5.4)], False, False),
    ("주의 4.2 + conf0.85 + 대형익스포저 — 전체발송", [A("주의", 4.2, 0.85)], False, True),
    ("주의 4.2 + conf0.85 + 소액(2억) — 본인한정", [A("주의", 4.2, 0.85, "예선테크")], False, False),
    ("주의 4.2 + conf0.85 + 익스포저없음 — 본인한정", [A("주의", 4.2, 0.85, "없는회사XYZ")], False, False),
    ("참고만 9.0 — 본인한정", [A("참고", 9.0, 0.9)], False, False),
    ("기사 0건 — 본인한정", [], False, False),
    # ★ 오늘 사고 재발 방지 — 급락장이면 뉴스와 무관하게 전체발송
    ("급락장 + 참고만 — 전체발송(시장급락)", [A("참고", 3.0, 0.3)], True, True),
    ("급락장 + 기사 0건 — 전체발송(시장급락)", [], True, True),
]

# 시장급락 임계 자체를 검증 — 임계가 비정상적으로 높아지면(=안전장치 무력화)
# 실제 급락장에서도 미발동하므로, 집계값과 임계를 함께 확인한다.
# (변이 테스트에서 'MARKET_CRASH_RBAL_THRESHOLD를 999999로' 바꿔도
#  통과하던 구멍을 메움)
THRESHOLD_CASES = [
    ("시장급락 종목수 임계가 상식 범위(5~30)", lambda: 5 <= nm.MARKET_CRASH_STOCK_THRESHOLD <= 30),
    ("시장급락 잔고 임계가 상식 범위(50~500억)", lambda: 50 <= nm.MARKET_CRASH_RBAL_THRESHOLD <= 500),
    ("발송 임계가 상식 범위(4.0~7.0)", lambda: 4.0 <= nm.SELF_ONLY_MAX_SCORE <= 7.0),
    ("conf 우회 익스포저 임계가 상식 범위(10~500억)",
     lambda: 10 <= nm.STRONG_CAUTION_MIN_EXPOSURE <= 500),
    ("참고 축소 임계가 상식 범위(500~20000억)",
     lambda: 500 <= nm.REF_FULLSEND_MIN_EXPOSURE <= 20000),
]

REF_CASES = [
    ("전체발송 시 소액 참고 제외",
     [A("긴급", 8.0), A("참고", 4.0, 0.5, "예선테크")], False, 1),
    ("전체발송 시 대형 참고 유지",
     [A("긴급", 8.0), A("참고", 4.0, 0.5, "삼성전자")], False, 2),
    ("본인한정 시 참고 전부 유지",
     [A("참고", 4.0, 0.5, "예선테크"), A("참고", 3.0, 0.5, "삼성전자")], False, 2),
]


# 2차 검증 모델 승급 (2026-07-29 도입)
MODEL_CASES = [
    ("긴급 존재 → Opus 승급", [A("긴급", 8.0)], False, True),
    ("주의 5.5 → Opus 승급", [A("주의", 5.5)], False, True),
    ("주의 4.0 → Sonnet 유지", [A("주의", 4.0)], False, False),
    ("참고만 고점수 → Sonnet 유지", [A("참고", 9.0, 0.9)], False, False),
    ("급락장 → Opus 승급", [], True, True),
]


def pick_model(articles, crash=False):
    """운영과 동일한 판정으로 2차 검증 모델을 고른다."""
    _CRASH["on"] = crash
    nm.clear_price_alert_cache()
    with contextlib.redirect_stdout(io.StringIO()):
        sc = nm.decide_send_scope(articles, EXPO, "2026-07-27")
    return nm.CLAUDE_MODEL if sc["self_only"] else nm.CLAUDE_VERIFY_HIGH_MODEL


def main():
    print("=" * 74)
    print("[발송판정 검증]")
    print("=" * 74)
    fails = []
    for name, arts, crash, expect in CASES:
        r = decide(arts, crash)
        ok = r["full"] == expect
        if not ok:
            fails.append((name, f'{r["full"]} (상세 {r})', expect))
        mark = "OK  " if ok else "FAIL"
        extra = f" (급락 {r['cnt']}종목·{r['rbal']:,.0f}억)" if crash else ""
        print(f"  {mark} {name:44} → {'전체' if r['full'] else '본인'}{extra}")

    print("\n" + "=" * 74)
    print("[참고 등급 축소 검증]")
    print("=" * 74)
    for name, arts, crash, expect_n in REF_CASES:
        r = decide(arts, crash)
        ok = len(r["mail"]) == expect_n
        if not ok:
            fails.append((name, len(r["mail"]), expect_n))
        print(f"  {'OK  ' if ok else 'FAIL'} {name:44} → 메일 {len(r['mail'])}건 (기대 {expect_n})")

    print("\n" + "=" * 74)
    print("[임계값 상식 범위 검증]")
    print("=" * 74)
    for name, fn in THRESHOLD_CASES:
        ok = fn()
        if not ok:
            fails.append((name, "범위 이탈", "상식 범위"))
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")

    print("\n" + "=" * 74)
    print("[2차 검증 모델 승급]")
    print("=" * 74)
    for name, arts, crash, expect_high in MODEL_CASES:
        m = pick_model(arts, crash)
        is_high = (m == nm.CLAUDE_VERIFY_HIGH_MODEL)
        ok = is_high == expect_high
        if not ok:
            fails.append((name, m, "Opus" if expect_high else "Sonnet"))
        print(f"  {'OK  ' if ok else 'FAIL'} {name:34} → {m}")
    # 헤더 라벨이 실제 사용 모델을 따라가는지
    nm._LAST_VERIFY_MODEL = nm.CLAUDE_VERIFY_HIGH_MODEL
    lbl_high = nm._model_label()
    nm._LAST_VERIFY_MODEL = nm.CLAUDE_MODEL
    lbl_low = nm._model_label()
    ok_lbl = ("Opus" in lbl_high) and ("Sonnet" in lbl_low)
    if not ok_lbl:
        fails.append(("헤더 라벨 반영", f"{lbl_high} / {lbl_low}", "Opus / Sonnet"))
    print(f"  {'OK  ' if ok_lbl else 'FAIL'} 메일 헤더 라벨 — 승급 시 '{lbl_high}'")

    total = len(CASES) + len(REF_CASES) + len(THRESHOLD_CASES) + len(MODEL_CASES) + 1
    print("\n" + "=" * 74)
    print(f"결과: {total - len(fails)}/{total} 통과")
    print("=" * 74)
    if fails:
        print("\n[실패 상세]")
        for n, got, exp in fails:
            print(f"  · {n}\n    실제={got} 기대={exp}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
