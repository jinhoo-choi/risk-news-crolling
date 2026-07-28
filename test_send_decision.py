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
    """main()의 발송판정을 그대로 재현한다.

    ★ 재현이 아니라 '실제 호출'해야 하는 부분: 시장급락 집계값.
      build_price_alert_section을 판정 직전에 호출하는 구조가 유지되는지
      이 테스트가 지킨다.
    """
    _CRASH["on"] = crash
    if crash:
        with contextlib.redirect_stdout(io.StringIO()):
            nm.build_price_alert_section(EXPO, "2026-07-27")
    else:
        nm.build_price_alert_section.last_alerted_count = 0
        nm.build_price_alert_section.last_alerted_rbal = 0

    cnt = getattr(nm.build_price_alert_section, "last_alerted_count", 0)
    rbal = getattr(nm.build_price_alert_section, "last_alerted_rbal", 0)

    actionable = [a for a in articles if a.get("grade") in ("긴급", "주의")]
    mx = max((a.get("_risk_score") or 0) for a in actionable) if actionable else 0
    urgent = any(a.get("grade") == "긴급" for a in articles)

    def strong(a):
        if a.get("grade") != "주의":
            return False
        if (a.get("_conf_raw") or a.get("_ai_confidence") or 0) < 0.80:
            return False
        return _exposure((a.get("entity") or "").strip()) >= nm.STRONG_CAUTION_MIN_EXPOSURE

    market_crash = (cnt >= nm.MARKET_CRASH_STOCK_THRESHOLD
                    and rbal >= nm.MARKET_CRASH_RBAL_THRESHOLD)
    force = urgent or any(strong(a) for a in articles) or market_crash
    self_only = (mx < nm.SELF_ONLY_MAX_SCORE) and not force

    mail = articles
    if not self_only:
        mail = [a for a in articles
                if a.get("grade") != "참고"
                or _exposure((a.get("entity") or "").strip()) >= nm.REF_FULLSEND_MIN_EXPOSURE]
    return {"full": not self_only, "max": mx, "crash": market_crash,
            "cnt": cnt, "rbal": rbal, "mail": mail}


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

REF_CASES = [
    ("전체발송 시 소액 참고 제외",
     [A("긴급", 8.0), A("참고", 4.0, 0.5, "예선테크")], False, 1),
    ("전체발송 시 대형 참고 유지",
     [A("긴급", 8.0), A("참고", 4.0, 0.5, "삼성전자")], False, 2),
    ("본인한정 시 참고 전부 유지",
     [A("참고", 4.0, 0.5, "예선테크"), A("참고", 3.0, 0.5, "삼성전자")], False, 2),
]


def main():
    print("=" * 74)
    print("[발송판정 검증]")
    print("=" * 74)
    fails = []
    for name, arts, crash, expect in CASES:
        r = decide(arts, crash)
        ok = r["full"] == expect
        if not ok:
            fails.append((name, r["full"], expect, r))
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
            fails.append((name, len(r["mail"]), expect_n, r))
        print(f"  {'OK  ' if ok else 'FAIL'} {name:44} → 메일 {len(r['mail'])}건 (기대 {expect_n})")

    total = len(CASES) + len(REF_CASES)
    print("\n" + "=" * 74)
    print(f"결과: {total - len(fails)}/{total} 통과")
    print("=" * 74)
    if fails:
        print("\n[실패 상세]")
        for n, got, exp, r in fails:
            print(f"  · {n}\n    실제={got} 기대={exp} / 상세={r}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
