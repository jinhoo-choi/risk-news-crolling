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

# entity 표기가 흔들려도 경쟁사 판정이 유지되는지 (2026-07-29 삼성증권 사고)
# 실사례: '금융위의 삼성증권 봐주기?…중징계 감경될 듯'이 주의로 발송됨.
# 원인은 _ent in _BROKER_ENTITIES 정확 일치 — 공백·빈값·그룹 계열사명에서 뚫림.
ENTITY_VARIANT_CASES = [
    ("정확 일치", "삼성증권"),
    ("앞뒤 공백", " 삼성증권 "),
    ("그룹 계열사(생명)", "삼성생명"),
    ("그룹 계열사(카드)", "삼성카드"),
    ("빈값", ""),
]
_BROKER_TITLE = "금융위의 삼성증권 봐주기?...중징계 감경될 듯"

# ★반대로, 경쟁사가 '가해자'이고 피해종목이 따로 있으면 강등하면 안 된다
VICTIM_CASES = [
    ("NH투자증권 직원이 DI동일 주가조작 가담 적발", "DI동일"),
    ("키움증권 직원 연루 코오롱티슈진 시세조종", "코오롱티슈진"),
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


# GRADE_LIMITS 상한 준수 + 강등 잠금 (2026-07-28 21시 긴급 4건 사고)
def run_limit():
    """긴급 4건 투입 시 상한(2건, force_urgent 면제 1건 추가)을 지키고
    강등된 건이 잠기는지 확인."""
    arts = [
        {"title": "한국투자증권의 황당한 오류…연도 하나 틀려 복지 재심사",
         "entity": "한국투자증권", "entities": ["한국투자증권"], "grade": "긴급",
         "_ai_confidence": 0.9, "_risk_score": 7.8},
        {"title": "해성에어로보틱스, 인천지법 접수 파산신청 공시",
         "entity": "해성에어로보틱스", "entities": ["해성에어로보틱스"], "grade": "긴급",
         "_ai_confidence": 0.9, "_risk_score": 7.1},
        {"title": "'거래정지' 진원생명과학 주요 사업 중단 아냐",
         "entity": "진원생명과학", "entities": ["진원생명과학"], "grade": "긴급",
         "_ai_confidence": 0.85, "_risk_score": 5.8},
        {"title": "미중 반도체 쇼크…코스피 장중 6,000 붕괴",
         "entity": "삼성전자", "entities": ["삼성전자"], "grade": "긴급",
         "_ai_confidence": 0.85, "_risk_score": 5.0},
    ]
    for a in arts:
        a.update({"url": "http://x", "keyword": "", "desc": ""})
    with contextlib.redirect_stdout(io.StringIO()):
        out = nm.regrade_by_score(arts, exposure_data=EXPO)
    urgent = sum(1 for a in out if a.get("grade") == "긴급")
    locked = sum(1 for a in out if a.get("_grade_locked"))
    return urgent, locked


def run_verify_lock():
    """★_verify_high_risk_by_claude가 실제로 잠금을 존중하는지 검증.

    기존 테스트는 '_grade_locked가 설정되는가'만 봤다. 그 결과 잠금을
    '확인하는' 코드가 유실됐는데도 통과했고, 경쟁사 전산사고가 긴급 6.2로
    발송됐다(2026-07-29 18:31 키움증권). 소비자 쪽을 직접 호출해 검증한다.
    """
    import json as _json

    locked = {"title": "키움증권 전산사고", "grade": "참고", "_grade_locked": True}
    unlocked = {"title": "A사 부도", "grade": "주의"}
    arts = [locked, unlocked]

    # AI가 둘 다 '긴급'으로 올리려는 상황을 모의
    class _R:
        status_code = 200

        def json(self):
            return {"content": [{"type": "text", "text": _json.dumps(
                [{"id": 1, "grade": "긴급"}, {"id": 2, "grade": "긴급"}])}]}

        def raise_for_status(self):
            pass

    import requests
    _orig = requests.post
    requests.post = lambda *a, **k: _R()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            nm._verify_high_risk_by_claude(arts)
    finally:
        requests.post = _orig
    return locked["grade"], unlocked["grade"]


# 경쟁사 강등 잠금 검증용 고정 픽스처 (2026-09-03)
# 기존엔 운영 CSV(EXPO)를 그대로 써서 KB증권의 실제 익스포저 존재 여부에
# 결과가 좌우됐다. 0903 갱신에서 KB증권 행이 사라지자(0902 채권 100억/280명 →
# 0903 0행) '익스포저없음 강등'이 먼저 걸려 잠금 판정이 뒤집혔고, 코드가
# 멀쩡한데 테스트만 깨졌다. test_new_gates.py에서 같은 문제를 이미 겪었다
# (2026-08-19 아이에스동서 건).
# 검증 대상은 '경쟁사 강등 시 _grade_locked가 서는가'이지 특정 증권사의
# 잔고가 아니므로, 운영 데이터에서 떼어낸다.
_LOCK_EXPO = {
    "KB증권": [
        {"기준일": "2026-09-03", "종목명": "KB증권", "종목코드": "999001",
         "종목유형": "채권", "뱅잔고": "100", "뱅고객수": "280",
         "영잔고": "0", "영고객수": "0", "잔고(억)": "100", "고객수": "280",
         "리스크고객수": "0", "리스크잔고(억)": "0", "시장": "국내"},
    ],
    "한국투자증권": [
        {"기준일": "2026-09-03", "종목명": "한국투자증권", "종목코드": "999002",
         "종목유형": "채권", "뱅잔고": "50", "뱅고객수": "120",
         "영잔고": "0", "영고객수": "0", "잔고(억)": "50", "고객수": "120",
         "리스크고객수": "0", "리스크잔고(억)": "0", "시장": "국내"},
    ],
}


def run_lock(title, entity, grade):
    art = {"title": title, "entity": entity, "entities": [entity],
           "grade": grade, "_ai_confidence": 0.85, "_risk_score": 5.0,
           "url": "http://x", "keyword": "", "desc": ""}
    with contextlib.redirect_stdout(io.StringIO()):
        out = nm.regrade_by_score([art], exposure_data=_LOCK_EXPO)
    return bool(out[0].get("_grade_locked")) if out else False


# 익스포저 있는 종목의 긴급은 유지되는지 별도 확인
EXPO_CASES = [
    ("익스포저 대형 종목 긴급 유지",
     "삼성전자 회사채 채무불이행 발생", "삼성전자", "긴급", "긴급"),
]


def main():
    fails = []
    # 운영 CSV(EXPO)에 의존하는 케이스의 전제를 먼저 확인한다 (2026-09-03).
    # exposure_data.csv가 갱신되면서 종목이 빠지면 코드가 멀쩡해도 테스트가
    # 깨진다. 0903 갱신에서 KB증권 행이 사라져 실제로 겪었다.
    # 조용히 실패하면 데이터 변동을 코드 결함으로 오인해 엉뚱한 곳을 판다.
    for _nm_ in ("삼성전자",):
        if not nm.find_exposure(_nm_, EXPO):
            print(f"    ⚠ [픽스처 전제] {_nm_}: 익스포저에서 사라짐 — "
                  f"CSV 갱신 영향. 아래 실패는 코드 결함이 아닐 수 있음")
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

    print("\n" + "=" * 76)
    print("[경쟁사 판정 — entity 표기 변형 내성]")
    print("=" * 76)
    for name, ent in ENTITY_VARIANT_CASES:
        g, _ = run(_BROKER_TITLE, ent, "주의")
        ok = g in ("참고", "배제")
        if not ok:
            fails.append((f"경쟁사 변형: {name}", g, "참고 또는 배제"))
        print(f"  {'OK  ' if ok else 'FAIL'} {name:20} entity={ent!r:14} → {g}")

    print("\n" + "=" * 76)
    print("[★미탐 방지 — 경쟁사가 가해자·피해종목 별도면 유지]")
    print("=" * 76)
    for title, ent in VICTIM_CASES:
        g, _ = run(title, ent, "긴급")
        ok = g == "긴급"
        if not ok:
            fails.append((f"피해종목 유지: {ent}", g, "긴급"))
        print(f"  {'OK  ' if ok else 'FAIL'} {title[:36]:38} → {g}")

    print("\n" + "=" * 76)
    print("[★재검증이 잠금을 실제로 존중하는지 (소비자 쪽 검증)]")
    print("=" * 76)
    _lg, _ug = run_verify_lock()
    ok_lock = (_lg == "참고")
    ok_norm = (_ug == "긴급")
    if not ok_lock:
        fails.append(("잠금 건 등급 고정", _lg, "참고"))
    if not ok_norm:
        fails.append(("미잠금 건 재검증 반영", _ug, "긴급"))
    print(f"  {'OK  ' if ok_lock else 'FAIL'} 잠금 건: AI가 긴급으로 올려도 → {_lg}")
    print(f"  {'OK  ' if ok_norm else 'FAIL'} 미잠금 건: AI 판단 반영 → {_ug}")

    print("\n" + "=" * 76)
    print("[GRADE_LIMITS 상한 준수 + 강등 잠금]")
    print("=" * 76)
    _u, _l = run_limit()
    _max_urgent = nm.GRADE_LIMITS["긴급"] + 1   # force_urgent 1건 면제
    ok1 = _u <= _max_urgent
    ok2 = _l >= 1
    if not ok1:
        fails.append(("긴급 상한 준수", _u, f"≤{_max_urgent}"))
    if not ok2:
        fails.append(("강등 시 잠금 설정", _l, "≥1"))
    print(f"  {'OK  ' if ok1 else 'FAIL'} 긴급 4건 투입 → {_u}건 유지 (상한 {_max_urgent})")
    print(f"  {'OK  ' if ok2 else 'FAIL'} 강등된 건 잠금 → {_l}건")

    total = (len(CASES) + len(EXPO_CASES) + len(LOCK_CASES) + 2
             + len(ENTITY_VARIANT_CASES) + len(VICTIM_CASES) + 2)
    print("\n" + "=" * 76)
    print(f"결과: {total - len(fails)}/{total} 통과")
    print("=" * 76)
    for n, got, exp in fails:
        print(f"  · {n}\n    실제={got} 기대={exp}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
