"""리스크 점수 계산(calc_risk_score) 자동 검증.

배경: 전수점검에서 남은 미검증 영역. 과거 이 함수에서 두 건의 결함이 나왔다.
  · AI가 준 confidence를 검증 없이 사용 → conf=-0.5에서 점수 -2.8 (범위 이탈)
  · 키워드 가중치가 공백 변형을 못 잡아 같은 사건이 5.0점 vs 8.2점

점수는 발송 임계(5.5)와 직결되므로, 계산이 흔들리면 발송 범위가 통째로 바뀐다.
실패 시 종료코드 1.
"""
import sys, types, os, io, contextlib
import pandas as pd

_fake = types.ModuleType("yfinance")


class _T:
    def __init__(self, tk):
        pass

    def history(self, **k):
        return pd.DataFrame()


_fake.Ticker = _T
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


def score(title, entity="삼성전자", conf=0.8):
    a = {"title": title, "entity": entity, "reason": ""}
    if conf is not None:
        a["_ai_confidence"] = conf
    return nm.calc_risk_score(a, EXPO)


fails = []


def check(name, cond, detail=""):
    if not cond:
        fails.append((name, detail))
    print(f"  {'OK  ' if cond else 'FAIL'} {name}{'  ' + detail if detail and not cond else ''}")


print("=" * 74)
print("[1] 점수 범위 — 항상 0.0~10.0")
print("=" * 74)
for label, conf in [("정상 0.8", 0.8), ("음수 -0.5", -0.5), ("초과 1.5", 1.5),
                    ("None", None), ("문자열 'high'", "high"),
                    ("문자열 '0.9'", "0.9"), ("0", 0)]:
    s = score("일반 기사", "없는회사XYZ", conf)
    check(f"conf={label:14} → {s}", 0.0 <= s <= 10.0, f"범위 이탈: {s}")

print("\n" + "=" * 74)
print("[2] 공백 변형 — 같은 사건은 같은 점수")
print("=" * 74)
for t in ["A사 상장 폐지 결정", "B사 전산 장애 발생", "C사 매매거래 정지",
          "한국투자 증권 MTS 장애", "D사 회생 신청", "E사 채무 불이행",
          "F사 반대 매매 급증"]:
    s1, s2 = score(t), score(t.replace(" ", ""))
    check(f"{t[:22]:24} {s1} vs 공백제거 {s2}", abs(s1 - s2) < 0.05, f"{s1} != {s2}")

print("\n" + "=" * 74)
print("[3] 익스포저 규모 반영 — 클수록 높은 점수")
print("=" * 74)
tiers = [("삼성전자", "초대형"), ("코오롱티슈진", "중형"), ("없는회사XYZ", "없음")]
scores = [(n, lbl, score("일반 동향 기사", n)) for n, lbl in tiers]
for n, lbl, s in scores:
    print(f"       {n:14}({lbl:4}) → {s}")
check("익스포저 큰 종목이 더 높은 점수",
      scores[0][2] >= scores[1][2] >= scores[2][2],
      f"{[s for _, _, s in scores]}")

print("\n" + "=" * 74)
print("[4] 키워드 가중치 — 치명 키워드가 점수를 올림")
print("=" * 74)
base = score("일반 동향 기사")
for t, lbl in [("A사 상장폐지 결정", "상장폐지"), ("A사 부도 발생", "부도"),
               ("한국투자증권 MTS 장애", "당사+MTS")]:
    s = score(t)
    check(f"{lbl:12} {s} > 일반 {base}", s > base, f"{s} <= {base}")

print("\n" + "=" * 74)
print("[5] 오염 데이터 내성 — 예외 없이 동작")
print("=" * 74)
poison = {"오염종목": [{"종목명": "X", "종목유형": "주식", "잔고(억)": "-",
                    "고객수": "abc", "뱅잔고": "", "영잔고": None}]}
try:
    s = nm.calc_risk_score({"title": "T", "entity": "오염종목",
                            "_ai_confidence": 0.8, "reason": ""}, poison)
    check(f"결측·비수치 혼재 데이터 → {s}", 0.0 <= s <= 10.0, f"범위 이탈 {s}")
except Exception as e:
    check("결측·비수치 혼재 데이터", False, f"{type(e).__name__}: {e}")

print("\n" + "=" * 74)
print(f"결과: {'전체 통과' if not fails else f'{len(fails)}건 실패'}")
print("=" * 74)
for n, d in fails:
    print(f"  · {n} — {d}")
sys.exit(0 if not fails else 1)
