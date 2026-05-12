"""
네이버 뉴스 키워드 모니터링 & 이메일 알림
GitHub Actions 전용 버전 (1회 실행 후 종료)
"""

import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import os
from datetime import datetime

# ─────────────────────────────────────────────
# 설정 — GitHub Secrets에서 자동으로 읽어옴
# ─────────────────────────────────────────────
EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECEIVER  = os.environ["EMAIL_RECEIVER"]

KEYWORDS = ["리스크", "회생", "상장폐지", "파산", "워크아웃"]
MAX_NEWS_PER_KEYWORD = 5
SEEN_FILE = "seen_news.json"
# ─────────────────────────────────────────────


def load_seen_urls():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_urls(seen: set):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)


def crawl_naver_news(keyword: str) -> list:
    url = f"https://search.naver.com/search.naver?where=news&query={keyword}&sort=1"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"[{keyword}] 크롤링 오류: {e}")
        return []

    soup = BeautifulSoup(res.text, "html.parser")
    articles = []

    for item in soup.select("ul.list_news > li.bx")[:MAX_NEWS_PER_KEYWORD]:
        title_tag = item.select_one("a.news_tit")
        if not title_tag:
            continue
        articles.append({
            "title"  : title_tag.get_text(strip=True),
            "url"    : title_tag.get("href", ""),
            "keyword": keyword,
        })

    return articles


def build_email_html(new_articles: list) -> str:
    now = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
    rows = ""

    for kw in KEYWORDS:
        kw_articles = [a for a in new_articles if a["keyword"] == kw]
        if not kw_articles:
            continue

        rows += f"""
        <tr>
          <td style="background:#1a3c6e;color:#fff;padding:8px 14px;
              font-weight:bold;font-size:13px;">🔍 {kw}</td>
        </tr>"""

        for a in kw_articles:
            rows += f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #eee;">
            <a href="{a['url']}" style="color:#1a3c6e;font-weight:bold;
               text-decoration:none;font-size:14px;line-height:1.5;">
              {a['title']}
            </a><br>
            <span style="color:#999;font-size:11px;">{a['url']}</span>
          </td>
        </tr>"""

    html = f"""
    <html><body style="font-family:'맑은 고딕',Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
      <div style="max-width:680px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;
                  box-shadow:0 2px 8px rgba(0,0,0,0.1);">
        <div style="background:#1a3c6e;padding:18px 24px;">
          <h2 style="color:#fff;margin:0;font-size:17px;">📰 뉴스 모니터링 알림</h2>
          <p style="color:#aac4e8;margin:4px 0 0;font-size:12px;">{now} 기준 신규 뉴스 {len(new_articles)}건</p>
        </div>
        <table style="width:100%;border-collapse:collapse;">
          {rows}
        </table>
        <div style="padding:14px 24px;background:#f9f9f9;color:#bbb;font-size:11px;text-align:center;">
          자동 발송 · 키워드: {', '.join(KEYWORDS)}
        </div>
      </div>
    </body></html>
    """
    return html


def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
    print("이메일 발송 완료")


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 뉴스 모니터링 시작")
    seen_urls    = load_seen_urls()
    new_articles = []

    for keyword in KEYWORDS:
        articles = crawl_naver_news(keyword)
        for article in articles:
            if article["url"] and article["url"] not in seen_urls:
                new_articles.append(article)
                seen_urls.add(article["url"])
        print(f"  [{keyword}] 신규 {len([a for a in new_articles if a['keyword']==keyword])}건")

    save_seen_urls(seen_urls)

    if new_articles:
        now_str = datetime.now().strftime("%m/%d %H:%M")
        subject = f"[뉴스 모니터] {now_str} 신규 {len(new_articles)}건"
        send_email(subject, build_email_html(new_articles))
    else:
        print("신규 뉴스 없음 — 이메일 미발송")


if __name__ == "__main__":
    main()
