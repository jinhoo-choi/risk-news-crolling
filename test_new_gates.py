"""2026-08-01 신설 게이트 정탐/오탐 시뮬레이션.

기존 test_variants.py는 is_hard_excluded(하드 제외)만 검증한다. 오늘 추가된
게이트는 '제외' 단계가 아니라 등급 판정·표시·문구 정제 단계에 있어 별도
검증이 필요하다. 회귀셋에 없는 신규 시나리오로 구성했다.

  is_risk_resolved()                 리스크 해소 국면 참고 강등
  allow_group_expansion()            계열사 확장 조건부화
  strip_unsupported_action_clauses() 미보유 유형 조치 제거
  sanitize_customer_notice()         고객문구 정제
  sanitize_action_numbers()          창작 수치 차단(기사 실재 수치 면제)
  _event_badge_label()               배지 사건유형
  display_risk_score()               표시 점수 등급 대역
  RISK_PRIORITY                      사전 확장 커버리지

각 항목은 '이래야 한다(정탐)'와 '이러면 안 된다(오탐)'를 짝으로 둔다.
"""
import sys, types, os, io, contextlib
import pandas as pd

fake = types.ModuleType("yfinance")
class _T:
    def __init__(self, t): pass
    def history(self, **k): return pd.DataFrame()
fake.Ticker = _T
sys.modules["yfinance"] = fake
for k in ["EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER",
          "ANTHROPIC_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"]:
    os.environ.setdefault(k, "x@t.com" if "EMAIL" in k else "x")

import importlib.util
_spec = importlib.util.spec_from_file_location("nm", "naver_news_monitor.py")
nm = importlib.util.module_from_spec(_spec)
with contextlib.redirect_stdout(io.StringIO()):
    try:
        _spec.loader.exec_module(nm)
    except SystemExit:
        pass
    EXPO = nm.load_exposure_data()

fails = []
def chk(group, name, cond, detail=""):
    if not cond:
        fails.append((group, name, detail))
    print(f"  {'OK  ' if cond else 'FAIL'} [{group}] {name}"
          + (f"  → {detail}" if not cond and detail else ""))


# ══ 1. 리스크 해소 국면 ═══════════════════════════════════════════════
print("\n[1] 리스크 해소 국면 (강등돼야 함 / 강등되면 안 됨)")
RESOLVED_YES = [
    ("가처분 인용",        "OO소재, 상장폐지 절차 일시 보류…법원 가처분 인용"),
    ("이의신청 수용",      "△△전자 상폐 절차 중단…거래소 이의신청 수용"),
    ("거래정지 해제",      "□□바이오 매매거래 정지 해제…내일부터 거래 재개"),
    ("회생 종결",          "◇◇건설 회생절차 종결 결정…경영 정상화"),
    ("워크아웃 졸업",      "☆☆중공업 워크아웃 졸업…채권단 관리 종료"),
    ("상폐 심사 철회",     "▽▽테크 상장폐지 심사 철회 결정"),
    ("공백 변형",          "OO소재 상장 폐지 절차 유예 결정"),
]
RESOLVED_NO = [
    ("정리매매 동반",      "더테크놀로지, 정리매매 첫날 82% 급락…8월 10일 상장폐지"),
    ("해제 후 정리매매",   "OO기업 거래정지 해제, 곧바로 정리매매 돌입"),
    ("유예기간 종료",      "××사 상장폐지 유예기간 종료…퇴출 수순"),
    ("이의신청 기각",      "△△사 상장폐지 결정, 이의신청 기각"),
    ("가처분 신청(진행)",  "금양, 상장폐지 효력정지 가처분 신청"),
    ("회생 신청(진행)",    "제이알글로벌리츠 기초자산 회생 신청"),
    ("보류 무관",          "OO사, 배당 지급 보류 결정"),
]
for n, t in RESOLVED_YES:
    chk("해소", f"강등O {n}", nm.is_risk_resolved(t), t[:40])
for n, t in RESOLVED_NO:
    chk("해소", f"강등X {n}", not nm.is_risk_resolved(t), t[:40])


# ══ 2. 계열사 확장 게이트 ═════════════════════════════════════════════
print("\n[2] 계열사 확장 (전이성 사건만 확장)")
for ev in ["파산부도", "기업회생", "유동성위기", "차환실패", "신용등급강등"]:
    chk("확장", f"허용 {ev}", nm.allow_group_expansion({"event_type": ev}))
for ev in ["상장폐지", "거래정지", "금감원제재", "시스템장애", "PF부실",
           "대규모환매", "감사의견거절", "반대매매", "횡령배임", "기타리스크"]:
    chk("확장", f"차단 {ev}", not nm.allow_group_expansion({"event_type": ev}))
chk("확장", "당사직접이슈 예외",
    nm.allow_group_expansion({"event_type": "시스템장애", "_force_urgent": True}))
chk("확장", "article 없음 안전", not nm.allow_group_expansion(None))
chk("확장", "event_type 결측 안전", not nm.allow_group_expansion({"entity": "SK"}))


# ══ 3. 미보유 유형 조치 제거 ══════════════════════════════════════════
print("\n[3] 미보유 유형 조치 (여신 없으면 OB 제거 / 있으면 유지)")
lotte = nm.find_exposure("롯데카드", EXPO)      # 채권만
skh   = nm.find_exposure("SK하이닉스", EXPO)    # 여신 대규모
OB_CASES = [
    ("표준형",   "채권 산출 → 여신 보유잔고 3억원 이상 고객 즉시 인계, OB 최우선 진행"),
    ("즉시 생략", "점검 → 여신 보유잔고 1억원 이상 고객 인계, OB 진행"),
    ("천만원",   "점검 → 여신 보유잔고 5천만원 이상 고객 인계, OB 진행"),
    ("문중 위치", "여신 보유잔고 3억원 이상 고객 즉시 인계, OB 최우선 진행 → 본부장 보고"),
]
for n, a in OB_CASES:
    out, rm = nm.strip_unsupported_action_clauses(a, lotte)
    chk("조치", f"제거O {n}", "OB" not in out, out[:44])
for n, a in OB_CASES:
    out, rm = nm.strip_unsupported_action_clauses(a, skh)
    chk("조치", f"유지 {n}", rm == [] and out == a, out[:44])
out, _ = nm.strip_unsupported_action_clauses("채권 평가손 산출 → 고객 안내 준비", lotte)
chk("조치", "주식없음 고객안내 제거", "고객 안내" not in out, out[:44])
out, _ = nm.strip_unsupported_action_clauses("담보비율 점검 → 고객 안내 준비", skh)
chk("조치", "주식있음 고객안내 유지", "고객 안내" in out, out[:44])
out, _ = nm.strip_unsupported_action_clauses("만기 일정 산출 → 컴플라이언스부 공유", lotte)
chk("조치", "무관 문구 불변", out == "만기 일정 산출 → 컴플라이언스부 공유", out[:44])


# ══ 3-B. 여신 미보유 시 신용거래 조치 제거 (2026-08-02) ══════════════
print("\n[3-B] 신용거래 조치 (여신 0이면 제거 / 여신 있으면 유지)")
dawon = nm.find_exposure("다원시스", EXPO)   # 주식만
REAL = ("다원시스 보유 고객 평가손 및 담보부족계좌 즉시 산출 "
        "→ 이의신청 기각·상폐 확정 시 강제 매도 예정 수량·손실 금액 재산출 후 컴플라이언스부 보고 "
        "→ 신용융자·미수 보유 고객 우선 추출하여 담보비율 긴급 점검, 반대매매 연쇄 가능성 사전 차단 "
        "→ 이의신청 결과 실시간 추적, 추가 하한가 발생 시 담보부족 계좌 재산출 즉시 착수")
out, rm = nm.strip_unsupported_action_clauses(REAL, dawon)
chk("신용", "8/2 실사례 문제절 제거", "신용융자" not in out, out[:50])
chk("신용", "유효절 보존(평가손)", "평가손" in out, out[:50])
chk("신용", "유효절 보존(손실 금액 재산출)", "손실 금액 재산출" in out, out[:50])
chk("신용", "유효절 보존(실시간 추적)", "실시간 추적" in out, out[:50])
chk("신용", "절 개수 4→3", len([c for c in out.split("→") if c.strip()]) == 3)
out2, rm2 = nm.strip_unsupported_action_clauses(
    "담보비율 점검 → 신용융자 고객 추출하여 반대매매 점검", skh)
chk("신용", "여신 보유 시 유지", rm2 == [], str(rm2))
chk("신용", "곁가지 1회 언급은 유지",
    not nm._is_yeosin_dependent_clause("상폐 확정 시 강제 매도 수량 재산출 후 보고"))
chk("신용", "키워드 2개면 제거",
    nm._is_yeosin_dependent_clause("신용융자 고객 담보비율 점검"))
chk("신용", "절 시작 키워드면 제거",
    nm._is_yeosin_dependent_clause("반대매매 연쇄 가능성 사전 차단"))
# 8/3 JR리츠 — 절 안 '및' 하위절 단위 판정
jr = nm.find_exposure("제이알글로벌리츠", EXPO)   # 주식·채권만, 여신 0
JR = ("JR리츠 보유 주식 고객 평가손 산출 및 담보대출 보유 고객 담보비율 긴급 점검 "
      "→ 임시주총 결과 확인 후 채권 평가손 재산출")
out3, rm3 = nm.strip_unsupported_action_clauses(JR, jr)
chk("신용", "8/3 담보대출 하위절 제거", "담보대출" not in out3, out3[:52])
chk("신용", "같은 절 주식 조치 보존", "주식 고객 평가손 산출" in out3, out3[:52])
chk("신용", "담보대출 키워드 등재", "담보대출" in nm._YEOSIN_DEP_KW)
# 8/4 본느 — 문장 파손 방지 (서술어 없이 끝나면 제거 포기)
bonne = nm.find_exposure("본느", EXPO)   # 주식만, 여신 0
out4, rm4 = nm.strip_unsupported_action_clauses(
    "본느 보유 고객 평가손 및 담보계좌 전수 점검", bonne)
chk("신용", "파손 방지: 원문 유지", "담보계좌" in out4, out4[:50])
out5, rm5 = nm.strip_unsupported_action_clauses(
    "본느 보유 고객 평가손 산출 및 담보계좌 전수 점검", bonne)
chk("신용", "서술어 있으면 정상 제거", "담보계좌" not in out5 and "산출" in out5, out5[:50])
chk("신용", "담보계좌·신용계좌 등재",
    "담보계좌" in nm._YEOSIN_DEP_KW and "신용계좌" in nm._YEOSIN_DEP_KW)


# ══ 3-C. 중복 조치 정리 (2026-08-02) ═════════════════════════════════
print("\n[3-C] 중복 조치 (동일 조치명 중복만 제거)")
o, r = nm.dedup_action_phrases("이의신청 결과 추적 → 고객 안내 준비, 소비자보호부 고객 안내 준비 요청")
chk("중복", "8/2 실사례", o.count("고객 안내 준비") == 1, o)
o, r = nm.dedup_action_phrases("평가손 산출 → 담보비율 점검 → 고객 안내 준비")
chk("중복", "중복 없으면 불변", r == [], o)
o, r = nm.dedup_action_phrases(
    "손실 금액 재산출 후 보고 → 담보부족 계좌 재산출 즉시 착수")
chk("중복", "다른 조치의 '재산출' 보존",
    "손실 금액 재산출" in o and "담보부족 계좌 재산출" in o, o)
o, r = nm.dedup_action_phrases("")
chk("중복", "빈 입력 안전", o == "" and r == [])


# ══ 3-D. 규제 완화 호재 게이트 (2026-08-02) ══════════════════════════
print("\n[3-D] 규제 완화 (호재 차단 / 조임 방향 통과)")
EASE_BLOCK = [
    ("8/2 실사례",   "“23조 부실 털자”…지역농협 NPL펀드 셀프투자 허용"),
    ("한도 완화",     "금융위, 증권사 부동산PF 익스포저 한도 규제 완화"),
    ("규제 개선",     "금감원, 부실채권 매각 규제 개선…처리 속도 빨라진다"),
    ("요건 완화",     "당국, 저축은행 PF 대출 만기연장 요건 완화 허용"),
    ("부실규모 병기", "“30조 연체” 카드사 대손상각 기준 완화 인가"),
]
for n, t in EASE_BLOCK:
    e, r = nm.is_hard_excluded(t)
    chk("완화", f"차단 {n}", e, f"통과됨: {t[:40]}")
EASE_PASS = [
    ("규제 강화",   "금융위, 증권사 부동산PF 익스포저 한도 규제 강화…자본확충 요구"),
    ("제한 신설",   "금감원, 증권사 내부통제 기준 제한 신설"),
]
for n, t in EASE_PASS:
    e, r = nm.is_hard_excluded(t)
    chk("완화", f"통과 {n}", not e, f"차단됨({r}): {t[:40]}")
chk("완화", "정탐 이력 오차단 없음",
    not any(nm.is_hard_excluded(x["title"])[0]
            and "규제 완화" in (nm.is_hard_excluded(x["title"])[1] or "")
            for x in __import__("json").load(
                open("regression_set.json", encoding="utf-8"))["true_positive_history"]))


# ══ 3-E. 여신표 채널 행 정렬 (2026-08-03) ════════════════════════════
print("\n[3-E] 여신표 정렬 (위험고객 · 최고리스크 줄 수 일치)")
_seg_src = open("naver_news_monitor.py", encoding="utf-8").read()
chk("정렬", "최고리스크 빈 채널 '-' 유지",
    "or _dash" in _seg_src and "_dash = '<span style=\"color:#cbd5e1;\">-</span>'" in _seg_src)
chk("정렬", "최고리스크 항상 2줄 구조",
    _seg_src.count('<div>{b_line}</div>') >= 1
    and '<div style="margin-top:3px;">{y_line}</div>' in _seg_src)
chk("정렬", "조건부 lines 누적 제거", "if b_line: lines.append" not in _seg_src)


# ══ 3-F. 담보비율 정규화 (2026-08-10) ════════════════════════════════
print("\n[3-F] 담보비율 (소수 보정 + 정수 %)")
for v, exp, why in [
    ("1.41", "141", "소수 표기 보정"),
    ("1.4687", "147", "소수 4자리 보정"),
    ("139.07", "139", "정상값 정수화"),
    ("149.55", "150", "반올림"),
    ("142.00", "142", "소수부 0"),
    ("0", "0", "0은 보정 안 함"),
    ("", "", "공란 안전"),
    ("abc", "abc", "비수치 안전"),
]:
    got = nm._normalize_ratio_pct(v)
    chk("담보", f"{why} {v!r}", got == exp, f"{got!r} != {exp!r}")
chk("담보", "정규화 결과에 소수점 없음",
    all("." not in nm._normalize_ratio_pct(x)
        for x in ("1.41", "139.07", "149.55", "1.4687")))


# ══ 4. 고객문구 정제 ══════════════════════════════════════════════════
print("\n[4] 고객문구 정제 (제거돼야 함 / 보존돼야 함)")
exp = [{"뱅잔고": "2", "뱅고객수": "380", "영잔고": "1", "영고객수": "127"}]
DIRTY = [
    ("플레이스홀더",  "A사(종목코드 확인 요망)는 상장폐지 예정입니다.",  "확인 요망"),
    ("확인 바람",     "B사(코드 확인 바람) 거래정지 안내드립니다.",       "확인 바람"),
    ("TBD",           "C사(TBD) 정리매매 진행 예정입니다.",              "TBD"),
    ("공백 기호",     "D사 ○○○ 상장폐지 예정입니다.",                   "○○○"),
]
for n, t, bad in DIRTY:
    o, f = nm.sanitize_customer_notice(t, exp)
    chk("문구", f"제거 {n}", bad not in o, o[:44])
o, f = nm.sanitize_customer_notice("E사는 2025년 8월 10일 상장 폐지 예정입니다.", exp)
chk("문구", "과거 연도 제거", "2025년" not in o and "8월 10일" in o, o[:44])
o, f = nm.sanitize_customer_notice("F사는 2026년 8월 10일 상장 폐지 예정입니다.", exp)
chk("문구", "당해 연도 보존", "2026년" in o, o[:44])
o, f = nm.sanitize_customer_notice("G사는 2027년 만기 도래 예정입니다.", exp)
chk("문구", "내년 보존", "2027년" in o, o[:44])
o, f = nm.sanitize_customer_notice(
    "여신 1억원 이상 고객은 확인 바랍니다.", exp)
chk("문구", "임계 표현 보존", "1억원 이상" in o, o[:44])
o, f = nm.sanitize_customer_notice("보유 고객 380명 대상 안내드립니다.", exp)
chk("문구", "실제 익스포저 보존", "380명" in o, o[:44])
o, f = nm.sanitize_customer_notice("보유 고객 9,999명 대상 안내드립니다.", exp)
chk("문구", "창작 수치 제거", "9,999명" not in o and "9999명" not in o, o[:44])


# ══ 5. 창작 수치 차단 (기사 실재 수치 면제) ═══════════════════════════
print("\n[5] 창작 수치 (기사 인용 보존 / 환각 차단)")
hug = nm.find_exposure("주택도시보증공사", EXPO)
src = "감사원, HUG PF보증 부실심사로 3,820억 사업장까지 보증"
t, b = nm.sanitize_action_numbers("3,820억 규모 부실 사업장 점검", hug, src)
chk("수치", "기사 인용 면제", not t, str(b))
t, b = nm.sanitize_action_numbers("보유 고객 1,062명(여신 106억원) 인계", hug, src)
chk("수치", "환각 차단", t and b, str(b))
mixed = "3,820억 규모 점검 → 보유 고객 9,999명 인계 → 여신 1억원 이상 OB"
t, b = nm.sanitize_action_numbers(mixed, hug, src)
out = nm._strip_tainted_numbers(mixed, b)
chk("수치", "혼합: 기사수치 보존", "3,820억" in out, out[:50])
chk("수치", "혼합: 창작수치 제거", "9,999명" not in out, out[:50])
chk("수치", "혼합: 임계 보존", "1억원 이상" in out, out[:50])
t, b = nm.sanitize_action_numbers("100억 규모 점검", [], src)
chk("수치", "익스포저 없으면 스킵", not t)


# ══ 6. 배지 사건유형 ══════════════════════════════════════════════════
print("\n[6] 배지 (event_type 기준 / keyword 무시)")
BADGE = [
    ("키워드 불일치", {"keyword": "상장폐지", "event_type": "금감원제재"}, "금감원 제재"),
    ("키워드 불일치2", {"keyword": "거래정지", "event_type": "상장폐지"}, "상장폐지"),
    ("기타리스크 생략", {"keyword": "해외주식 급락", "event_type": "기타리스크"}, ""),
    ("event_type 결측", {"keyword": "부도", "event_type": None}, ""),
    ("event_type 빈값", {"keyword": "부도", "event_type": "  "}, ""),
    ("정상 매핑", {"keyword": "x", "event_type": "파산부도"}, "파산·부도"),
]
for n, a, exp_lbl in BADGE:
    chk("배지", n, nm._event_badge_label(a) == exp_lbl, repr(nm._event_badge_label(a)))
chk("배지", "HTML 주입 무해화",
    "<" not in nm._event_badge_label({"event_type": "<script>x</script>"}))


# ══ 7. 표시 점수 등급 대역 ════════════════════════════════════════════
print("\n[7] 표시 점수 (등급 대역 준수 / 역전 없음)")
CASES = [
    ("긴급 저점수", 4.5, "긴급"), ("긴급 고점수", 9.0, "긴급"),
    ("주의 저점수", 4.5, "주의"), ("주의 고점수", 6.8, "주의"),
    ("참고 고점수", 9.0, "참고"),
]
vals = {}
for n, raw, g in CASES:
    v = nm.display_risk_score({"_risk_score": raw, "grade": g})
    vals[n] = v
    lo, hi = nm._GRADE_SCORE_BAND[g]
    chk("점수", f"{n} 대역 준수", lo <= v <= hi, f"{v} not in [{lo},{hi}]")
chk("점수", "긴급최저 > 주의최고",
    vals["긴급 저점수"] > vals["주의 고점수"],
    f'{vals["긴급 저점수"]} vs {vals["주의 고점수"]}')
chk("점수", "주의최저 > 참고최고",
    vals["주의 저점수"] > vals["참고 고점수"],
    f'{vals["주의 저점수"]} vs {vals["참고 고점수"]}')
chk("점수", "원점수 없으면 빈값",
    nm.display_risk_score({"grade": "긴급"}) == "")
chk("점수", "이상값 방어",
    nm.display_risk_score({"_risk_score": 999, "grade": "주의"}) <= 7.0)
chk("점수", "회차 독립성",
    nm.display_risk_score({"_risk_score": 6.0, "grade": "긴급"})
    == nm.display_risk_score({"_risk_score": 6.0, "grade": "긴급"}))


# ══ 8. 사전 확장 커버리지 ═════════════════════════════════════════════
print("\n[8] RISK_PRIORITY 사전 (신규 표현 매칭 / 오탐 미격상)")
def kw(t):
    ns = nm._NS_RE.sub("", t)
    return max([v for k, v in nm.RISK_PRIORITY.items()
                if k in t or nm._NS_RE.sub("", k) in ns], default=1.0)
SHOULD_HIT = [
    ("회생절차 개시", "OO사, 회생절차 개시 신청…법정관리 수순"),
    ("신용등급 하향", "나이스신평, OO사 신용등급 A0→A- 하향"),
    ("신용등급 표기끼임", "한신평, H社 신용등급 BBB→BB 하향…부정적 검토"),
    ("차환 실패",    "OO건설 ABCP 차환 실패…만기 미매각"),
    ("감사의견 거절", "XX리츠 감사의견 거절…상폐 사유 발생"),
    ("실질심사",     "다원시스, 상장적격성 실질심사 대상 결정"),
    ("불성실공시",   "OO전자 불성실공시법인 지정예고"),
    ("투자유의종목", "거래소, 3종 투자유의종목 적출"),
    ("주가조작",     "NH투자증권 직원 주가조작 가담 적발"),
    ("발행어음 미상환", "OO증권 발행어음 만기 500억 미상환 확정"),
    ("PF 부실",     "증권사 PF 브릿지론 현장검사 착수"),
]
for n, t in SHOULD_HIT:
    chk("사전", f"매칭 {n}", kw(t) > 1.0, f"w={kw(t)}")
SHOULD_NOT = [
    ("홍보성",   "한국투자증권 IT투자액 1762억 '톱'"),   # 당사명은 기존 매칭(제외 대상 아님)
    ("연예 가십", "샘킴, 정호영 배신하고 에스파 춤췄다"),
    ("일반 시황", "코스피 강세 마감…외국인 순매수 지속"),
    ("실적 호조", "OO전자 2분기 영업이익 사상 최대"),
]
for n, t in SHOULD_NOT[1:]:
    chk("사전", f"미격상 {n}", kw(t) == 1.0, f"w={kw(t)}")
chk("사전", "최고대역 미침범",
    max(v for k, v in nm.RISK_PRIORITY.items()
        if k in ("주가조작", "미상환", "신용등급", "PF 부실")) <= 1.5)


# ══ 결과 ══════════════════════════════════════════════════════════════
print("\n" + "─" * 60)
if fails:
    print(f"  ❌ {len(fails)}건 실패")
    for g, n, d in fails:
        print(f"     [{g}] {n}  {d}")
    sys.exit(1)
print("  ✅ 신규 게이트 시뮬레이션 전체 통과")
