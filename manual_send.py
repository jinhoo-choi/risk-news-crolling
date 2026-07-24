"""수동 발송 스크립트 — 필터에서 놓친 기사를 참고 등급으로 전체 대상에 발송.

배경: 7/22~23 검토 중 2건이 자동탐지에서 누락된 것을 확인:
1. "한투증권 이벤트 보상 하세월…참여자 불만 잇따라" — 사용자가 직접 제공한
   원문 링크(n.news.naver.com, 봇 환경에선 도메인 차단으로 fetch 불가했으나
   사용자가 원문임을 확인)와 제목/본문 그대로 사용
2. "코오롱티슈진 TG-C 美 3상 실패에 이틀째 '下'" — 오늘 07시 메일에 이미
   주의 등급으로 발송된 코오롱티슈진 건의 후속(이틀 연속 하한가) 심화 기사

두 건 모두 참고 등급으로, 기존 build_email_html()/send_email() 인프라를
그대로 재사용해 전체 대상(EMAIL_RECEIVER+EMAIL_CC+본인)에 발송한다.
GitHub Actions에서 workflow_dispatch로 1회성 실행하는 용도 — 정기 실행
파이프라인(news_monitor.yml)과는 별개.
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
ARTICLES_PREPARED_ON = "2026-07-23"   # 아래 articles를 새로 채울 때 반드시 갱신

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
        "id": 1,
        "grade": "참고",
        "title": "한투증권 이벤트 보상 하세월…참여자 불만 잇따라",
        "url": "https://n.news.naver.com/article/374/0000523122?sid=101",
        "entity": "한국투자증권",
        "entities": ["한국투자증권"],
        "keyword": "",
        "pub_str": "07/22",
        "_risk_score": 3.0,
    },
    {
        "id": 2,
        "grade": "참고",
        "title": "코오롱티슈진 TG-C 美 3상 실패에 이틀째 '下'",
        "url": "https://www.newsis.com/view/NISX20260722_0003718146",
        "entity": "코오롱티슈진",
        "entities": ["코오롱티슈진"],
        "keyword": "반도체주 급락",
        "pub_str": "07/22",
        "_risk_score": 4.5,
    },
]

exposure_data = load_exposure_data()
ref_date = ""
for rows in exposure_data.values():
    if rows:
        ref_date = rows[0].get("기준일", "")
        break

html = build_email_html(
    articles,
    total_count=len(articles),
    ai_summary="필터 누락 확인 후 수동 보정 발송 — 이벤트 보상 지연 소비자 불만, 코오롱티슈진 하한가 후속",
    exposure_data=exposure_data,
    ref_date=ref_date,
    today_str=now.strftime("%Y-%m-%d"),
)

subject = f"[리스크 탐지] {now.strftime('%m월 %d일')} {now.strftime('%H')}시 기준"
send_email(subject, html, self_only=False)
print("발송 완료:", subject)
