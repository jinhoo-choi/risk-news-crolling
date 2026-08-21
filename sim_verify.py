"""미검증 패치 시뮬레이션 검수 (2026-08-16)

실전 회차에 해당 유형 기사가 나오지 않아 검증되지 못한 패치를,
가상 기사로 발동/미발동을 확인한다. 회귀 스위트가 아니라 1회성 검수용.

대상:
  A. 고객문구 월(月) 환각 가드      (8/14 TIME ETF 실사례 재현)
  B. ETF·ETN 구조적 상폐 주의 강등  (동일)
  C. 익스포저 종목 오귀속 방지      (8/13 삼성 그룹사 기사 재현)
  D. 고객문구 문장 경계 절단        (8/15 엑시큐어 실사례 재현)
"""
import os, re, sys, io, contextlib

for _k in ("EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER",
           "ANTHROPIC_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"):
    os.environ.setdefault(_k, "x")

import naver_news_monitor as M

PASS, FAIL = [], []


def check(case, cond, detail=""):
    (PASS if cond else FAIL).append(case)
    print(f"  {'✅' if cond else '❌'} {case}" + (f"\n       {detail}" if detail else ""))


# ── A. 월 환각 가드 ───────────────────────────────────────────────
print("\n[A] 고객문구 월(月) 환각 가드")
src_etf = ("TIME 미국배당다우존스액티브 ETF는 상장폐지 전 영업일인 오는 18일에 "
           "매매거래가 정지된다. 19일 상장폐지 예정이다.")
a1, f1 = M.sanitize_customer_notice(
    "[한국투자증권] 긴급 안내 보유하신 ETF가 5월 19일 상장 폐지될 예정입니다. "
    "마지막 거래 가능일(5월 18일)까지 매도 여부를 검토하시기 바랍니다.", [], src_etf)
check("창작된 '5월' 제거, 일자 보존", "5월" not in a1 and "19일" in a1 and "18일" in a1, a1)

a2, _ = M.sanitize_customer_notice("8월 19일 상장폐지 예정입니다.", [], src_etf)
check("현재월(8월)은 오차단 없음", "8월 19일" in a2, a2)

src_m = "회사는 3월 19일 상장폐지 결정을 받았다."
a3, _ = M.sanitize_customer_notice("3월 19일 상장폐지 결정을 받았습니다.", [], src_m)
check("기사에 명시된 월은 보존", "3월 19일" in a3, a3)

a4, _ = M.sanitize_customer_notice("2025년 8월 10일 상장 폐지", [], src_etf)
check("연도 가드 회귀 없음", "2025년" not in a4, a4)


# ── B. ETF 구조적 상폐 등급 상한 ──────────────────────────────────
print("\n[B] ETF·ETN 구조적 상폐 주의 강등")
etf_cases = [
    ("TIME ETF 상관계수(실사례)", "TIME 미국배당다우존스액티브, 상관계수 미달에 19일 상장폐지 예정", "TIME 미국배당다우존스액티브", True),
    ("KODEX 추적오차",           "KODEX 200선물인버스, 추적오차 확대로 상장폐지 결정",            "KODEX 200선물인버스",       True),
    ("ETN 존속기한",             "삼성 레버리지 원유선물 ETN, 존속기한 만료로 상장폐지",          "삼성 레버리지 원유선물ETN", True),
    ("일반주식 자본잠식 상폐",   "광명전기, 반기말 완전자본잠식…상장폐지 실질심사",              "광명전기",                 False),
    ("ETF 기초자산 폭락 청산",   "XX ETF, 편입종목 폭락에 조기청산 상장폐지",                     "XX ETF",                   False),
    ("감사의견거절",             "엔지켐생명과학, 상반기 검토의견 의견거절",                      "엔지켐생명과학",           False),
]
for name, title, ent, expect in etf_cases:
    art = {"title": title, "entity": ent, "entities": [ent], "grade": "긴급",
           "summary": "", "confidence": 0.95, "event_type": "상장폐지"}
    M.regrade_by_score([art], {})
    fired = art.get("_notice_exempt") is True
    check(f"{name} → {'발동' if expect else '미발동'} 기대", fired == expect,
          f"grade={art.get('grade')} locked={art.get('_grade_locked')}")

# 강등돼도 고객 안내는 유지되는가 (렌더 조건 재현)
art = {"title": "TIME 미국배당다우존스액티브, 상관계수 미달에 19일 상장폐지 예정",
       "entity": "TIME 미국배당다우존스액티브", "entities": ["TIME 미국배당다우존스액티브"],
       "grade": "긴급", "summary": "", "confidence": 0.95, "event_type": "상장폐지"}
M.regrade_by_score([art], {})
check("강등 후에도 안내문구 생성 조건 충족",
      art.get("grade") != "긴급" and bool(art.get("_notice_exempt")),
      f"grade={art.get('grade')} notice_exempt={art.get('_notice_exempt')}")


# ── C. 익스포저 종목 오귀속 방지 ──────────────────────────────────
print("\n[C] 익스포저 종목 오귀속 방지 (그룹사 다종목 기사)")
src = open("naver_news_monitor.py", encoding="utf-8").read()
_blk = src[src.index("        def _fmt_exp(r):"):src.index('        exp_str = ", ".join')]
ns = {"_num": M._num}
exec("\n".join(l[8:] for l in _blk.split("\n")), ns)

rows = [  # 8/13 실제 데이터 (삼성증권 채권이 2행으로 분리 저장된 상태)
    dict(종목명="삼성생명", 종목코드="032830", 종목유형="주식", 뱅잔고="661", 뱅고객수="6056", 영잔고="874", 영고객수="4313"),
    dict(종목명="삼성화재", 종목코드="000810", 종목유형="주식", 뱅잔고="115", 뱅고객수="1414", 영잔고="95",  영고객수="392"),
    dict(종목명="삼성증권", 종목코드="016360", 종목유형="채권", 뱅잔고="99",  뱅고객수="323",  영잔고="0",   영고객수="0"),
    dict(종목명="삼성증권", 종목코드="016360", 종목유형="채권", 뱅잔고="4",   뱅고객수="31",   영잔고="92",  영고객수="96"),
]
merged = ns["_merge_exp_rows"](rows)
exp_str = ", ".join(ns["_fmt_exp"](r) for r in merged)
check("중복 채권 행 병합 (4행 → 3행)", len(merged) == 3, f"{len(merged)}행")
check("병합값이 카드 표시값(103억/354명)과 일치", "삼성증권 채권 뱅키스 103억원/354명" in exp_str)
check("영업점 채권값 보존 (92억/96명)", "영업점 92억원/96명" in exp_str)
check("모든 항목에 종목명 표기 — 오귀속 차단",
      all(nm in exp_str for nm in ("삼성생명", "삼성화재", "삼성증권")), exp_str[:90] + "…")
check("유형만 단독 표기되는 항목 없음", not re.search(r'(^|, )(주식|채권|여신) ', exp_str))


# ── D. 고객문구 문장 경계 절단 ────────────────────────────────────
print("\n[D] 고객문구 문장 경계 절단")
real = ("[한국투자증권] 긴급 안내 엑시큐어하이트론이 상반기 연결·별도 재무제표에 대해 "
        "회계법인으로부터 '의견거절'을 받았으며, 자본잠식률이 79.1%로 악화되었습니다. "
        "의견거절은 상장 폐지(거래 불가) 사유에 해당할 수 있으며, 향후 거래가 제한될 수 있습니다. "
        "해당 종목을 보유하고 계신 고객께서는 보유 수량 및 주문 가능 상태를 즉시 확인하시고, "
        "KIND(kind.krx.co.kr)에서 공시 내용을 확인하시기 바랍니다.")
out = M.truncate_at_sentence(real, 200)
check("문장 중간 절단 없음 — 종결부호로 끝남", out.rstrip().endswith(("다.", "요.", ".")), f"…{out[-24:]}")
check("상한 준수", len(out) <= 200, f"len={len(out)}")
check("잘림 표시 '...' 잔존 없음", not out.endswith("..."), f"…{out[-16:]}")
short = "[한국투자증권] 안내 보유 수량을 확인하시기 바랍니다."
check("상한 이하 문구는 원문 유지", M.truncate_at_sentence(short, 200) == short)

# URL 오인 절단 회귀 (2026-08-16) — 도메인의 점을 문장 끝으로 오인해
# "…KIND(kind.krx.co." 로 잘리던 버그. 고객문구엔 KIND·DART 주소가 거의
# 항상 들어가므로 상시 회귀로 둔다.
url_case = ("[한국투자증권] 긴급 안내 윌비스가 반기 재무제표 검토의견 의견거절을 받아 "
            "상장 폐지 심의 대상이 될 수 있습니다. 심의 결과에 따라 거래가 제한될 수 있으니 "
            "보유 수량 및 주문 가능 여부를 즉시 확인하시기 바랍니다. 관련 공시는 "
            "KIND(kind.krx.co.kr) 및 전자공시시스템(dart.fss.or.kr)에서 확인하시기 바랍니다. "
            "문의: 고객센터 1544-5000")
u = M.truncate_at_sentence(url_case, 200)
check("도메인 점을 문장 끝으로 오인하지 않음", u.rstrip().endswith(("다.", "요.")), f"…{u[-26:]}")
check("URL 중간 절단 없음", not re.search(r'\.(co|or|krx|fss)\.?$', u.rstrip()), f"…{u[-26:]}")
check("행동유도 보존 (조회처보다 앞에 온 경우)", "확인하시기 바랍니다" in u)

# 행동유도가 마지막에 온 문구는 소실이 계측되는가
lost_case = ("[한국투자증권] 긴급 안내 엑시큐어하이트론이 상반기 재무제표에 대해 의견거절을 "
             "받았으며, 자본잠식률이 79.1%로 악화되었습니다. 의견거절은 상장 폐지 사유에 해당할 "
             "수 있으며, 향후 거래가 제한될 수 있습니다. 해당 종목을 보유하고 계신 고객께서는 "
             "보유 수량 및 주문 가능 상태를 즉시 확인하시고, KIND(kind.krx.co.kr)에서 공시 "
             "내용을 확인하시기 바랍니다.")
_before = getattr(M.truncate_at_sentence, "action_lost", 0)
l = M.truncate_at_sentence(lost_case, 200)
check("행동유도 소실이 계측됨",
      getattr(M.truncate_at_sentence, "action_lost", 0) == _before + 1)
check("소실되더라도 문장은 완결", l.rstrip().endswith(("다.", "요.")), f"…{l[-24:]}")


# ── 대응방안 익스포저 수치 제거 (직전 패치 재확인) ────────────────
print("\n[E] 대응방안 익스포저 수치 제거 (회귀 확인)")
for name, txt, must_out, must_in in [
    ("채널+금액 괄호", "보유 고객(뱅키스 12억원/1,671명 · 영업점 9억원/409명) 평가손 산출", "뱅키스", "평가손 산출"),
    ("고객수만",       "보유 고객(뱅키스 765명·영업점 114명) 평가손 산출",                  "765",   "평가손 산출"),
    ("빈 채널괄호",    "보유 고객(뱅키스·영업점) 평가손 산출",                              "영업점", "평가손 산출"),
]:
    r = M.strip_exposure_figures(txt)
    r = r[0] if isinstance(r, tuple) else r
    check(f"{name} 제거", must_out not in r and must_in in r, r)

for name, txt, keep in [
    ("정책 임계기준 보존", "여신 보유잔고 3억원 이상 고객 즉시 인계, OB 최우선 진행", "3억원 이상"),
    ("기사 사건규모 보존", "약 3,820억 규모 부실 사업장 익스포저 재평가 후 보고",     "3,820억"),
]:
    r = M.strip_exposure_figures(txt)
    r = r[0] if isinstance(r, tuple) else r
    check(name, keep in r, r)


# ── F. 8/17~8/19 회차 검수 반영분 ────────────────────────────────
print("\n[F] ETF 안내 누락 — 등급 경로와 무관하게 exempt")
for name, grade in [("AI가 처음부터 주의(8/17 실사례)", "주의"),
                    ("AI가 긴급 판정", "긴급"),
                    ("AI가 참고 판정", "참고")]:
    a = {"title": "수익률 너무 좋은데 상장폐지! 액티브 ETF, 0.7 상관계수 미달",
         "entity": "TIME 미국배당다우존스액티브", "entities": ["TIME 미국배당다우존스액티브"],
         "grade": grade, "summary": "", "confidence": 0.9, "event_type": "상장폐지"}
    M.regrade_by_score([a], {})
    check(f"{name} → 안내문구 생성", a.get("_notice_exempt") is True)

a = {"title": "광명전기, 반기말 완전자본잠식 상장폐지 심사", "entity": "광명전기",
     "entities": ["광명전기"], "grade": "주의", "summary": "", "confidence": 0.9,
     "event_type": "상장폐지"}
M.regrade_by_score([a], {})
check("일반주식 상폐는 exempt 미부여", a.get("_notice_exempt") is None)


print("\n[G] 사건유형 무근거 배지 차단")
badge_cases = [
    ("★우리금융 실적기사→횡령배임", "신한 3040억 벌 때 우리 -571억…임종룡의 해외사업, 왜 거꾸로 가나",
     "횡령배임", "해외사업 손실 확대로 실적 부진", ""),
    ("★위메이드 민원기사→금감원제재", '뿔난 위메이드 주주…"새 주인 실체 불분명" 금감원에 민원 제기',
     "금감원제재", "주주들이 민원을 제기했다", ""),
    # 유동성위기 패턴을 넓힌 뒤에도 실적 부진 기사는 여전히 막혀야 한다
    ("★실적부진→유동성위기 오분류", "OO전자 3분기 영업이익 30% 감소…시장 기대 하회",
     "유동성위기", "실적이 부진했다", ""),
]
for name, title, ev, desc, body in badge_cases:
    a = {"title": title, "event_type": ev, "entity": "X", "desc": desc, "body": body,
         "reason": "AI가 생성한 사유 — 근거로 쓰이면 안 된다"}
    check(f"{name} 배지 생략", M._event_badge_label(a) == "")

ok_cases = [
    # 과차단 회귀 (2026-08-21) — '미상환'이 근거 패턴에 없어 정상 건의 배지가
    # 사라졌다. 8/21 14시 셀루메드 실사례. 게이트를 넣을 때 생긴 부작용이라
    # 상시 회귀로 둔다.
    ("★셀루메드 대여금 미상환", "셀루메드, 140억 대여금 회수 '빨간불'…담보 미설정·만기 미상환",
     "유동성위기", "유동성 위기"),
    ("한국토지신탁 구속·검찰", "한국토지신탁, 내부통제는?…임직원 구속·회장 검찰", "횡령배임", "횡령·배임"),
    ("듀오백 거래정지", "거래소 듀오백, 20일부터 주권매매거래 정지", "거래정지", "거래정지"),
    ("JTBC 상폐확정", "JTBC 상장채권 3종 상폐 확정", "상장폐지", "상장폐지"),
    ("포스코이앤씨 파산", "브라질 하원, 포스코이앤씨 현지법인 파산 규탄안 의결", "파산부도", "파산·부도"),
    ("형지글로벌 미지급", "형지글로벌, 전환사채 원리금 미지급 사태 발생", "유동성위기", "유동성 위기"),
    ("실제 제재", "금감원, 위메이드에 과징금 20억 부과", "금감원제재", "금감원 제재"),
]
for name, title, ev, want in ok_cases:
    a = {"title": title, "event_type": ev, "entity": "X", "desc": "", "body": ""}
    check(f"{name} 배지 유지", M._event_badge_label(a) == want, M._event_badge_label(a))


print("\n[H] 전망성 기사 긴급 과대 강등")
spec_cases = [
    ("★한빛소프트(8/19 실사례)", "한빛, 상장폐지 위험권 … 향후 시장 전망은?", True),
    ("전망 의문형", "OO전자, 상장폐지 가능성 제기…괜찮나", True),
    ("유동성 위기는 강등 금지", "OO건설 유동성 위기 심화…자금난 확대", False),
    ("듀오백 확정", "거래소 듀오백, 20일부터 주권매매거래 정지", False),
    ("JTBC 확정", "JTBC 상장채권 3종 상폐 확정…투자자 매매 주의보", False),
    ("형지글로벌 확정", "형지글로벌, 관리종목 지정되면서 전환사채 원리금 미지급 사태 발생", False),
    ("위기+확정 동시", "OO전자 상장폐지 위기…거래정지 결정", False),
]
for name, title, expect in spec_cases:
    a = {"title": title, "entity": "X", "entities": ["X"], "grade": "긴급",
         "summary": "", "confidence": 0.95, "event_type": "상장폐지"}
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        M.regrade_by_score([a], {})
    fired = "[전망성 기사 긴급→주의]" in _buf.getvalue()
    check(f"{name} → {'강등' if expect else '유지'}", fired == expect)


print("\n[I] 대응방안 종목명 보강")
for name, txt, ent, expect in [
    ("★듀오백(8/19 누락)", "보유 고객 평가손 즉시 산출 → 주문 가능 상태 점검", "듀오백", True),
    ("★JTBC(8/19 누락)", "보유 고객 채권 평가손 즉시 산출 → 정리매매 절차 점검", "JTBC", True),
    ("모나미(이미 표기)", "모나미 보유 고객 평가손 즉시 산출 → 보고", "모나미", False),
    ("약칭 표기(중복 방지)", "한빛 보유 고객 평가손 즉시 산출", "한빛소프트", False),
]:
    out, fixed = M.prepend_entity_to_action(txt, ent)
    check(f"{name} 보강={expect}", fixed == expect, out[:48])
    check(f"{name} 결과에 종목명 존재", ent[:2] in out[:24])


print("\n" + "═" * 60)
print(f"  시뮬레이션 검수: {len(PASS)}건 통과 / {len(FAIL)}건 실패")
if FAIL:
    for f in FAIL:
        print(f"    ❌ {f}")
print("═" * 60)
sys.exit(1 if FAIL else 0)
