"""수동 발송 스크립트 — 자동 발송분에서 오탐만 제외해 전체 대상 재발송.

배경: 2026-07-28 14시는 코스피 서킷브레이커가 발동한 급락장(삼성전자 -11.7%,
SK하이닉스 -13.1%, 경보 60개 종목)이었으나, 시장급락 안전장치가 무력화된
버그(집계값을 판정 시점에 읽지 못함, 커밋 69ac0d6에서 수정)로 본인 한정
발송에 그쳤다. 오탐 1건('[더벨][상장폐지 카운트다운] 온타이드' — 이중
브래킷 연재물)을 제외한 3건으로 전체 대상 재발송한다.

제목·기준시각·수집건수는 자동 발송분과 동일 형식을 유지한다
(내용이 14시 수집분 그대로이므로 표기가 사실과 일치).
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
        "id": 1, "grade": "참고",
        "title": "삼전·닉스 '장중 급락' 속...반도체 HBM·CXL·소부장주 '장중 뚝'",
        "url": "http://www.choicenews.co.kr/news/articleView.html?idxno=168864",
        "entity": "삼성전자", "entities": ["삼성전자"],
        "keyword": "", "pub_str": "07/28 10:26", "_risk_score": 5.0,
    },
    {
        "id": 2, "grade": "참고",
        "title": "삼성전자 11%·SK하이닉스 13% 급락…코스피 6200선도 붕괴",
        "url": "http://www.newsian.co.kr/news/articleView.html?idxno=92871",
        "entity": "삼성전자", "entities": ["삼성전자"],
        "keyword": "", "pub_str": "07/28 13:58", "_risk_score": 4.0,
    },
    {
        "id": 3, "grade": "참고",
        "title": "NAVER(네이버) 주가 21만원대로 '털썩'…코스피·코스닥 서킷브레이커 발동",
        "url": "https://www.cbci.co.kr/news/articleView.html?idxno=592504",
        "entity": "NAVER", "entities": ["NAVER"],
        "keyword": "", "pub_str": "07/28 12:52", "_risk_score": 4.0,
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
_BASE_TIME = datetime(2026, 7, 28, 14, 10, tzinfo=KST)
_COLLECTED = 1410

html = build_email_html(
    articles,
    total_count=_COLLECTED,
    ai_summary="코스피 급락·서킷브레이커 발동 — 여신잔고 위험고객 60개 종목 하락",
    exposure_data=exposure_data,
    ref_date=ref_date,
    today_str="2026-07-28",
    now_override=_BASE_TIME,
)

subject = "[리스크 탐지] 07월 28일 14시 기준"
send_email(subject, html, self_only=False)
print("발송 완료:", subject)
