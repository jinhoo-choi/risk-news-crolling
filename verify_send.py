"""신규 수정분 실환경 검증용 — 가상 기사로 메일을 렌더해 본인에게만 발송.

배경: 2026-08-01은 토요일이라 3회차(07/14/21시) 모두 '참고' 등급만 나와
익스포저 카드·여신표가 렌더되지 않았고, 아래 커밋들이 실메일에서 검증되지
못했다.
    537f33f 익스포저 카드 표시범위 축소 (계열사 확장 조건부화 + 0억 행 숨김)
    02141f6 미보유 유형 조치 문구 제거 + 여신표 0억 표기 정리
    8ef7f97 오탐·표기오류 6종

기사는 전부 가상이며 URL도 실재하지 않는다. 오인 방지를 위해
  · 제목에 [검증] 표기
  · send_email(self_only=True) 로 본인에게만 발송
  · CONFIRM_SEND=YES 없으면 렌더만 하고 중단
"""
import os
import sys
from datetime import datetime, timezone, timedelta

from naver_news_monitor import (
    build_email_html, send_email, load_exposure_data,
    regrade_by_score, allow_group_expansion,
    strip_unsupported_action_clauses, sanitize_customer_notice,
    find_exposure, GROUP_ENTITIES_MAP,
)

KST = timezone(timedelta(hours=9))
now = datetime.now(KST)

exposure_data = load_exposure_data()
ref_date = ""
for rows in exposure_data.values():
    if rows:
        ref_date = rows[0].get("기준일", "")
        break

# ── 가상 기사 ────────────────────────────────────────────────────────
# 각 건이 특정 수정분을 타격하도록 구성했다.
articles = [
    # [1] 계열사 확장 차단 + 고객문구 정제(플레이스홀더·연도 환각)
    #     event_type=상장폐지 → 전이성 없음 → SK 계열 11개사가 붙지 않아야 한다.
    {
        "id": 1, "grade": "긴급", "event_type": "상장폐지",
        "title": "[검증] SK하이닉스 자회사 SK시그넷, 상장폐지 결정…8월 20일 정리매매",
        "url": "https://example.com/verify/1",
        "entity": "SK하이닉스", "entities": ["SK하이닉스"],
        "keyword": "거래정지",           # 배지가 event_type(상장폐지)로 나와야 함
        "pub_str": now.strftime("%m/%d %H:%M"), "_risk_score": 7.5,
        "_ai_confidence": 0.95,
        "reason": "상장폐지 확정 — 정리매매 일정 확정",
        "action": ("보유 고객 평가손 즉시 산출 → 정리매매 기간 매도 안내, "
                   "여신 보유잔고 3억원 이상 고객 즉시 인계, OB 최우선 진행"),
        "customer_notice": ("[한국투자증권] 긴급 안내\n"
                            "SK시그넷(종목코드 확인 요망)은 2025년 8월 20일 "
                            "상장 폐지 예정으로, 정리매매가 진행될 예정입니다."),
    },
    # [2] 계열사 확장 유지 — event_type=기업회생 → 전이성 있음
    #     [1]과 동일한 SK 그룹이므로, 확장 여부가 event_type만으로 갈리는지
    #     같은 조건에서 A/B 대조가 된다. 여기서는 11개 계열사가 붙어야 한다.
    {
        "id": 2, "grade": "긴급", "event_type": "기업회생",
        "title": "[검증] SK이노베이션, 회생절차 개시 신청…계열 교차보증 연쇄 우려",
        "url": "https://example.com/verify/2",
        "entity": "SK이노베이션", "entities": ["SK이노베이션"],
        "keyword": "파산",
        "pub_str": now.strftime("%m/%d %H:%M"), "_risk_score": 8.2,
        "_ai_confidence": 0.93,
        "reason": "회생절차 개시 신청 — 계열 교차보증 연쇄 가능성",
        "action": ("계열 교차보증 익스포저 즉시 집계 → 담보비율 점검, "
                   "여신 보유잔고 3억원 이상 고객 즉시 인계, OB 최우선 진행"),
        "customer_notice": ("[한국투자증권] 긴급 안내\n"
                            "SK이노베이션 관련 보유 종목의 거래 상황을 확인해 주시기 바랍니다."),
    },
    # [3] 미보유 유형 조치 제거 — 롯데카드는 채권만 보유(여신 0)
    {
        "id": 3, "grade": "주의", "event_type": "신용등급강등",
        "title": "[검증] 롯데카드 신용등급 A0→A- 하향…회사채 스프레드 확대",
        "url": "https://example.com/verify/3",
        "entity": "롯데카드", "entities": ["롯데카드"],
        "keyword": "신용등급",
        "pub_str": now.strftime("%m/%d %H:%M"), "_risk_score": 6.0,
        "_ai_confidence": 0.85,
        "reason": "신용등급 하향 확정",
        "action": ("채권 만기 도래 일정 즉시 산출 → 차환 리스크 시나리오 점검, "
                   "여신 보유잔고 3억원 이상 고객 즉시 인계, OB 최우선 진행"),
    },
    # [4] 리스크 해소 국면 → 참고 강등되어야 한다(주의로 입력)
    {
        "id": 4, "grade": "주의", "event_type": "상장폐지",
        "title": "[검증] 대진첨단소재, 상장폐지 절차 일시 보류…법원 가처분 인용",
        "url": "https://example.com/verify/4",
        "entity": "대진첨단소재", "entities": ["대진첨단소재"],
        "keyword": "상장폐지",
        "pub_str": now.strftime("%m/%d %H:%M"), "_risk_score": 7.3,
        "_ai_confidence": 0.90,
        "reason": "상폐 절차 중단 — 리스크 완화 방향",
        "action": "추이 점검 → 가처분 결과 확인 후 재평가",
    },
]

# 등급 재산정(참고 강등 로직 포함)
articles = regrade_by_score(articles, exposure_data)

# 대응방안·고객문구 후처리 — 실제 파이프라인과 동일 순서로 적용
for a in articles:
    exp_rows = find_exposure(a.get("entity", ""), exposure_data)
    if a.get("action"):
        a["action"], _rm = strip_unsupported_action_clauses(a["action"], exp_rows)
        if _rm:
            print(f"  [미보유 유형 조치 제거] {a['title'][:34]} — {_rm}")
    if a.get("customer_notice"):
        a["customer_notice"], _fx = sanitize_customer_notice(
            a["customer_notice"], exp_rows, a.get("title", ""))
        if _fx:
            print(f"  [고객문구 정제] {a['title'][:34]} — {_fx}")

print("\n[등급 재산정 결과]")
for a in articles:
    print(f"   {a['grade']:3s} | {a['title'][:52]}")

print("\n[계열사 확장 판정]")
for a in articles:
    ok = allow_group_expansion(a)
    n = len(GROUP_ENTITIES_MAP.get(a.get("entity", ""), []))
    print(f"   {a.get('event_type',''):6s} → 확장 {'허용' if ok else '차단'} "
          f"(그룹맵 보유 {n}개사)")

html = build_email_html(
    articles,
    total_count=1287,
    ai_summary="[검증 발송] 수정분 실환경 점검 — 실제 리스크 아님",
    exposure_data=exposure_data,
    ref_date=ref_date,
    today_str=now.strftime("%Y-%m-%d"),
)

with open("verify_output.html", "w", encoding="utf-8") as f:
    f.write(html)
print(f"\n렌더 완료: verify_output.html ({len(html):,} bytes)")

if os.environ.get("CONFIRM_SEND", "").upper() != "YES":
    print("[중단] 발송하려면 CONFIRM_SEND=YES 필요 (렌더만 수행)")
    sys.exit(0)

subject = f"[검증] 리스크봇 수정분 점검 — {now.strftime('%m월 %d일 %H시')}"
send_email(subject, html, self_only=True)   # 본인에게만
print("발송 완료(본인 한정):", subject)
