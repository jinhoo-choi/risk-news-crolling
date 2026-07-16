# -*- coding: utf-8 -*-
"""본인 한정(self_only) 테스트 발송 스크립트.

목적: 발신자 표시명(❗리스크봇)·수신자 표시 수정이 실제 Gmail SMTP
환경에서 의도대로 렌더링되는지 확인. 실제 send_email()/build_email_html()
함수를 그대로 사용하며, self_only=True로 강제해 EMAIL_SENDER(본인)
에게만 발송하고 CC 그룹(risk_vip 등)에는 나가지 않는다.

실행: python3 test_send_self.py  (GitHub Actions에서 workflow_dispatch로 트리거)
"""
import os
from datetime import datetime, timezone, timedelta

import naver_news_monitor as m

KST = timezone(timedelta(hours=9))
now = datetime.now(KST)


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
        ai_summary="[발송 테스트] 이 메일은 본인에게만 발송되는 테스트입니다. "
                    "실제 리스크 판단 결과가 아닙니다.",
        exposure_data=exposure_data,
        ref_date=next(iter(exposure_data.values()))[0].get("기준일", "") if exposure_data else "",
        today_str=now.strftime("%Y-%m-%d"),
    )

    subject = f"[발송 테스트] ❗리스크봇 표시 확인 — {now.strftime('%m월 %d일 %H시 %M분')} 기준"
    m.send_email(subject, html, self_only=True)
    print("본인 한정 테스트 발송 완료 — Gmail에서 발신자·받는사람 표시를 확인하세요.")


if __name__ == "__main__":
    main()
