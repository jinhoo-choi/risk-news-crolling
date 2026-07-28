"""등급 조정(regrade_by_score) 자동 검증.

배경: 이 함수는 오탐의 상당수를 실제로 걸러내는 핵심 로직인데(경쟁사 자체
리스크 배제·당사 오추출 방어·익스포저 기반 등급 상한·confidence 강등)
자동 검증이 전혀 없었다. 전수점검에서 '검증 사각지대'로 지목된 영역.

is_hard_excluded를 통과한 기사가 여기서 어떻게 처리되는지를 진리표로 고정한다.
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


def run(title, entity, grade, conf=0.85, score=7.0, desc=""):
    """regrade_by_score 1건 실행 → (등급 or '배제', entity)"""
    art = {"title": title, "entity": entity, "entities": [entity] if entity else [],
           "grade": grade, "_ai_confidence": conf, "_risk_score": score,
           "url": "http://x", "keyword": "", "desc": desc}
    with contextlib.redirect_stdout(io.StringIO()):
        out = nm.regrade_by_score([art], exposure_data=EXPO)
    if not out:
        return "배제", None
    return out[0].get("grade"), out[0].get("entity")


# (설명, 제목, entity, 입력등급, 기대등급, entity 기대)
#   기대등급 '배제' = 결과에서 제거되어야 함
#   None = 검사 안 함
CASES = [
    # ── 경쟁사 자체 리스크 ──
    ("경쟁사 전산장애(익스포저 있음) → 참고 강등",
     "키움증권 MTS 접속 장애…30분간 매매 중단", "키움증권", "긴급", "참고", None),
    ("경쟁사 자체손실(익스포저 없음) → 배제",
     "[단독] 신한투자증권, 美 운용사 파산보호로 2800억원 회수 불투명",
     "신한투자증권", "긴급", "배제", None),
    ("공백 변형 경쟁사명도 인식",
     "키움 증권 MTS 접속 장애…매매 중단", "키움증권", "긴급", "참고", None),

    # ── 당사 이슈는 유지 ──
    ("당사 MTS 장애 → 긴급 유지",
     "한국투자증권 MTS 접속 장애, 매매 1시간 중단", "한국투자증권", "긴급", "긴급", None),
    ("당사 제재 → 긴급 유지",
     "한국투자증권 전산사고 과태료 1억 제재", "한국투자증권", "긴급", "긴급", None),

    # ── 당사 오추출 방어 ──
    ("경쟁사 주체 기사에 당사 오추출 → entity 무효화",
     "키움證, 빗썸 지분 인수 추진…잦은 전산사고는 '부담'",
     "한국투자증권", "주의", None, ""),

    # ── 피해종목이 별도인 경우는 유지 ──
    ("경쟁사가 가해자·피해종목 별도 → 유지",
     "NH투자증권 직원이 DI동일 주가조작 가담 적발", "DI동일", "긴급", "긴급", None),

    # ── 익스포저 기반 등급 상한 ──
    ("익스포저 없는 종목은 긴급 불가 → 강등",
     "XX리츠 감사의견 거절…상장폐지 사유 발생", "존재하지않는종목XYZ", "긴급", None, None),
]

# ★결정론적 강등이 AI 재검증에 덮어써지지 않는지 (2026-07-29 사고)
LOCK_CASES = [
    ("경쟁사 강등 시 _grade_locked 설정",
     "이번엔 기관주의…KB증권, 가중 제제 리스크 노출", "KB증권", "주의", True),
    ("이미 참고여도 잠금 설정",
     "이번엔 기관주의…KB증권, 가중 제제 리스크 노출", "KB증권", "참고", True),
    ("당사 이슈는 잠기지 않음(재검증 정상 동작)",
     "한국투자증권 MTS 접속 장애, 매매 1시간 중단", "한국투자증권", "긴급", False),
]


def run_lock(title, entity, grade):
    art = {"title": title, "entity": entity, "entities": [entity],
           "grade": grade, "_ai_confidence": 0.85, "_risk_score": 5.0,
           "url": "http://x", "keyword": "", "desc": ""}
    with contextlib.redirect_stdout(io.StringIO()):
        out = nm.regrade_by_score([art], exposure_data=EXPO)
    return bool(out[0].get("_grade_locked")) if out else False


# 익스포저 있는 종목의 긴급은 유지되는지 별도 확인
EXPO_CASES = [
    ("익스포저 대형 종목 긴급 유지",
     "삼성전자 회사채 채무불이행 발생", "삼성전자", "긴급", "긴급"),
]


def main():
    fails = []
    print("=" * 76)
    print("[등급 조정(regrade_by_score) 검증]")
    print("=" * 76)
    for name, title, ent, gin, gexp, eexp in CASES:
        g, e = run(title, ent, gin)
        ok = True
        if gexp is not None and g != gexp:
            ok = False
        if eexp is not None and e != eexp:
            ok = False
        if not ok:
            fails.append((name, f"등급={g} entity={e}", f"등급={gexp} entity={eexp}"))
        print(f"  {'OK  ' if ok else 'FAIL'} {name:44} → {g}"
              f"{'' if eexp is None else f' / entity={e!r}'}")

    print()
    for name, title, ent, gin, gexp in EXPO_CASES:
        g, _ = run(title, ent, gin)
        ok = g == gexp
        if not ok:
            fails.append((name, g, gexp))
        print(f"  {'OK  ' if ok else 'FAIL'} {name:44} → {g}")

    print("\n" + "=" * 76)
    print("[결정론적 강등 잠금 — AI 재검증 덮어쓰기 방지]")
    print("=" * 76)
    for name, title, ent, gin, expect in LOCK_CASES:
        got = run_lock(title, ent, gin)
        ok = got == expect
        if not ok:
            fails.append((name, got, expect))
        print(f"  {'OK  ' if ok else 'FAIL'} {name:46} → 잠금={got}")

    total = len(CASES) + len(EXPO_CASES) + len(LOCK_CASES)
    print("\n" + "=" * 76)
    print(f"결과: {total - len(fails)}/{total} 통과")
    print("=" * 76)
    for n, got, exp in fails:
        print(f"  · {n}\n    실제={got} 기대={exp}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
