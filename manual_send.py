"""수동 발송 스크립트 — 자동 발송분에서 오탐만 제외해 전체 대상 재발송.

배경: 2026-07-28 07시 자동 발송(6건)은 최고점수 4.0으로 임계(5.5) 미달이라
본인 한정으로 나갔다. 검토 결과 오탐 1건('박은영, JTBC 재정난에 유튜브도
중단' — 연예매체 인물 심경 기사)을 제외한 5건은 전사 공유 가치가 있다고
판단해, 담당자 확인을 거쳐 전체 대상으로 재발송한다.

제목·기준시각은 자동 발송분과 동일 형식을 유지한다(내용이 07시 수집분
그대로이므로 표기가 사실과 일치).
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from naver_news_monitor import (
    build_email_html, send_email, load_exposure_data,
)

KST = timezone(timedelta(hours=9))
now = datetime.now(KST)

# ── 오발송 방지 안전장치 ──────────────────────────────────────────────
# 이 스크립트는 workflow_dispatch(수동 버튼)로 실행되며 전체 그룹메일로
# 발송된다. 아래 ARTICLES_PREPARED_ON 날짜의 기사 내용이 그대로 남아 있어
# 나중에 무심코 실행하면 낡은 기사가 임원진에게 다시 발송되는 사고가 난다.
# 따라서 (1) 기사 작성일이 오늘이 아니면 중단하고,
#        (2) CONFIRM_SEND=YES 환경변수가 있어야만 실제 발송한다.
ARTICLES_PREPARED_ON = "2026-07-28"   # 아래 articles를 새로 채울 때 반드시 갱신

_today = now.strftime("%Y-%m-%d")
if ARTICLES_PREPARED_ON != _today:
    print(f"[중단] 기사 준비일({ARTICLES_PREPARED_ON}) != 오늘({_today}).")
    print("       manual_send.py의 articles를 오늘 내용으로 교체하고")
    print("       ARTICLES_PREPARED_ON을 갱신한 뒤 다시 실행하세요.")
    sys.exit(0)

if os.environ.get("CONFIRM_SEND", "").upper() != "YES":
    print("[중단] 전체 발송을 위해서는 CONFIRM_SEND=YES 가 필요합니다.")
    print("       (워크플로우 수동 실행 시 입력값으로 지정)")
    sys.exit(0)

articles = [
    {
        "id": 1, "grade": "주의",
        "title": "\u201c인터록 논란에 초기대응 부실\u201d…HL만도, 중처법 적용되나",
        "url": "https://www.sentv.co.kr/article/view/sentv202607270100",
        "entity": "HL만도", "entities": ["HL만도"],
        "keyword": "부실 리스크", "event_type": "기타리스크",
        "pub_str": "07/27 18:10", "_risk_score": 4.0,
        "action": ("HL만도 보유 고객 중 여신(담보대출) 보유 계좌 담보비율 현황 점검 → "
                   "중대재해처벌법 적용 여부 및 수사 진행 상황 실시간 추적, 기소·처벌 확정 시 "
                   "주가 추가 하락 가능성 대비 담보부족 계좌 수 및 강제 매도 예정 규모 즉시 "
                   "재산출 → 여신 보유잔고 3억원 이상 고객 즉시 인계, OB 최우선 진행, 고객 안내 준비"),
    },
    {
        "id": 2, "grade": "참고",
        "title": "ASML·베시 8%대 급락…자동 거래정지됐다",
        "url": "https://www.tokenpost.kr/news/breaking/381827",
        "entity": "ASML", "entities": ["ASML"],
        "keyword": "", "pub_str": "07/28 00:02", "_risk_score": 8.1,
    },
    {
        "id": 3, "grade": "참고",
        "title": "엘앤에프 테슬라 계약 정정 공시 논란, 금융당국 압수수색 착수",
        "url": "https://www.tokenpost.kr/news/market/381751",
        "entity": "엘앤에프", "entities": ["엘앤에프"],
        "keyword": "", "pub_str": "07/27 21:38", "_risk_score": 5.0,
    },
    {
        "id": 4, "grade": "참고",
        "title": "카카오페이 사태 후폭풍…금감원, 대형 전자금융업자 개인정보 점검 확대",
        "url": "https://www.greened.kr/news/articleView.html?idxno=346062",
        "entity": "카카오페이", "entities": ["카카오페이"],
        "keyword": "", "pub_str": "07/27 20:48", "_risk_score": 4.5,
    },
    {
        "id": 5, "grade": "참고",
        "title": "엔비디아 주가 5% 급락…AI 투자 우려 커져",
        "url": "https://www.bntnews.co.kr/article/view/bnt202607280017",
        "entity": "엔비디아", "entities": ["엔비디아"],
        "keyword": "", "pub_str": "07/28 06:58", "_risk_score": 4.0,
    },
]

exposure_data = load_exposure_data()
ref_date = ""
for rows in exposure_data.values():
    if rows:
        ref_date = rows[0].get("기준일", "")
        break

# 자동 발송분(07:04, 수집 594건)과 동일한 헤더로 맞춘다.
# 내용이 그 회차 수집분 그대로이므로 표기가 사실과 일치한다.
_BASE_TIME = datetime(2026, 7, 28, 7, 4, tzinfo=KST)
_COLLECTED = 594

html = build_email_html(
    articles,
    total_count=_COLLECTED,
    ai_summary="ASML·엔비디아 급락 및 HL만도 중처법 적용 가능성 주시",
    exposure_data=exposure_data,
    ref_date=ref_date,
    today_str="2026-07-28",
    now_override=_BASE_TIME,
)

subject = "[리스크 탐지] 07월 28일 07시 기준"
send_email(subject, html, self_only=False)
print("발송 완료:", subject)
