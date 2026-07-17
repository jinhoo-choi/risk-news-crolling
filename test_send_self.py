# -*- coding: utf-8 -*-
"""본인 한정(하드코딩 수신자) 테스트 발송 스크립트.

목적: 발신자 표시명(❗리스크봇)·수신자 헤더 구성 수정이 실제 Gmail SMTP
환경에서 의도대로 렌더링되는지 확인.

기존 send_email(self_only=True)는 EMAIL_SENDER/NO_RESULT_RECEIVER
시크릿값을 수신자로 사용했으나, 이 스크립트는 그 경로를 타지 않고
수신자를 111715@koreainvestment.com으로 직접 하드코딩한다
(본인 확인 요청 — 노출 무관). EMAIL_SENDER/EMAIL_PASSWORD는 SMTP
로그인(인증) 용도로만 사용되고 수신자 결정에는 관여하지 않는다.

실행: python3 test_send_self.py  (GitHub Actions에서 workflow_dispatch로 트리거)
"""
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import naver_news_monitor as m

KST = timezone(timedelta(hours=9))
now = datetime.now(KST)

TEST_RECEIVER = "111715@koreainvestment.com"  # 하드코딩 — secrets(EMAIL_SENDER/EMAIL_RECEIVER) 미사용


def main():
    exposure_data = m.load_exposure_data()

    # 테스트용 최소 기사 1건 — 실제 익스포저·실제 렌더 함수를 그대로 태움
    articles = [{
        "grade": "참고",
        "entity": "SK하이닉스",
        "entities": ["SK하이닉스"],
        "keyword": "발송테스트",
        "title": "[발송 테스트] 발신자·수신자 표시 확인용 — 실제 리스크 기사 아님",
        "url": "https://github.com/jinhoo-choi/risk-news-crolling",
        "desc": "본 기사는 실제 리스크 탐지 결과가 아니라, 발신자 표시명(❗리스크봇)"
                "과 수신자 헤더 구성이 실제 Gmail 환경에서 의도대로 렌더링되는지"
                "확인하기 위한 테스트 발송입니다.",
        "pub_str": f"{now.strftime('%m/%d %H:%M')} (방금 전)",
        "_ai_confidence": 0.3,
        "_risk_score": 1.0,
    }]

    html = m.build_email_html(
        articles, total_count=len(articles),
        ai_summary="[발송 테스트] 이 메일은 지정된 테스트 수신자에게만 발송됩니다. "
                    "실제 리스크 판단 결과가 아닙니다.",
        exposure_data=exposure_data,
        ref_date=next(iter(exposure_data.values()))[0].get("기준일", "") if exposure_data else "",
        today_str=now.strftime("%Y-%m-%d"),
    )

    subject = f"[발송 테스트] ❗리스크봇 표시 확인 — {now.strftime('%m월 %d일 %H시 %M분')} 기준"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = m._from_header()
    msg["To"]      = m._addr_header(TEST_RECEIVER)
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.ehlo()
        server.login(m.EMAIL_SENDER, m.EMAIL_PASSWORD)  # SMTP 인증 용도만 — 수신자와 무관
        refused = server.sendmail(m.EMAIL_SENDER, [TEST_RECEIVER], msg.as_string())

    if refused:
        print(f"⚠️ 테스트 수신자 거부됨: {refused}")
    else:
        print(f"테스트 발송 완료 → {TEST_RECEIVER} (Gmail에서 발신자·받는사람 표시를 확인하세요.)")


if __name__ == "__main__":
    main()
