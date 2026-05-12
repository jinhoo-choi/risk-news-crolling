"""
네이버 뉴스 키워드 모니터링 & Claude AI 필터링 & 이메일 알림
GitHub Actions 전용 버전 / 네이버 검색 API 사용
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
EMAIL_SENDER      = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]
EMAIL_RECEIVERS   = [e.strip() for e in os.environ["EMAIL_RECEIVER"].split(",")]
ANTHROPIC_KEY     = os.environ["ANTHROPIC_API_KEY"]
NAVER_CLIENT_ID   = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]

KEYWORDS = ["리스크", "회생", "상장폐지", "파산", "워크아웃"]
MAX_NEWS_PER_KEYWORD = 30
SEEN_FILE = "seen_news.json"

GRADE_META = {
    "긴급": {"emoji": "🔴", "color": "#c0392b", "bg": "#fdf0ef"},
    "주의": {"emoji": "🟡", "color": "#d68910", "bg": "#fefce8"},
    "참고": {"emoji": "🟢", "color": "#1e8449", "bg": "#f0faf4"},
}
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
    """네이버 검색 API로 뉴스 수집"""
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    articles = []
    start = 1

    while len(articles) < MAX_NEWS_PER_KEYWORD:
        params = {
            "query": keyword,
            "display": 100,
            "start": start,
            "sort": "date",
        }
        try:
            res = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers=headers,
                params=params,
                timeout=10,
            )
            res.raise_for_status()
            data = res.json()
        except Exception as e:
            print(f"[{keyword}] API 오류: {e}")
            break

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            title = BeautifulSoup(item.get("title", ""), "html.parser").get_text()
            link  = item.get("originallink") or item.get("link", "")
            if link:
                articles.append({
                    "title"  : title,
                    "url"    : link,
                    "keyword": keyword,
                })
            if len(articles) >= MAX_NEWS_PER_KEYWORD:
                break

        total = data.get("total", 0)
        start += 100
        if start > min(total, 1000):
            break

    return articles


def ai_filter_and_grade(articles: list) -> list:
    if not articles:
        return []

    numbered = "\n".join([f"{i+1}. {a['title']}" for i, a in enumerate(articles)])

    prompt = f"""당신은 증권사 리스크 관리 전문가입니다.
아래 뉴스 제목들을 보고, 한국 증권사(특히 한국투자증권)의 영업·신용·시장·규제 리스크 관점에서 판단해 주세요.

판단 기준:
- 증권사 고객사·투자처의 부실, 파산, 워크아웃, 상장폐지
- 금융당국 제재, 검사, 규제 강화
- 시장 충격 (유동성 위기, 마진콜, 반대매매 급증)
- 부동산PF, 브릿지론, 미매각 관련
- 증권업 전반에 영향을 줄 수 있는 거시 리스크

각 뉴스에 대해 아래 JSON 배열만 반환하세요. 다른 말은 절대 하지 마세요.
- relevant: true/false (증권사 리스크와 무관하면 false)
- grade: "긴급" | "주의" | "참고" (relevant=false면 null)
  - 긴급: 즉각적인 손실·규제 위험 가능성
  - 주의: 모니터링 필요한 잠재 리스크
  - 참고: 업황 파악에 유용하나 직접 위험은 낮음

뉴스 목록:
{numbered}

반환 형식 예시:
[{{"id":1,"relevant":true,"grade":"긴급"}},{{"id":2,"relevant":false,"grade":null}}]"""

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-5-20251001",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        res.raise_for_status()
        raw = res.json()["content"][0]["text"].strip()
        grades = json.loads(raw)
        grade_map = {g["id"]: g for g in grades}

        result = []
        for i, article in enumerate(articles):
            info = grade_map.get(i + 1, {})
            if info.get("relevant") and info.get("grade"):
                article["grade"] = info["grade"]
                result.append(article)
        return result

    except Exception as e:
        print(f"AI 필터링 오류: {e} — 필터 없이 전체 반환")
        for a in articles:
            a["grade"] = "참고"
        return articles


def build_email_html(articles: list):
    now = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
    sections = {"긴급": [], "주의": [], "참고": []}
    for a in articles:
        sections[a["grade"]].append(a)

    rows = ""
    for grade in ["긴급", "주의", "참고"]:
        items = sections[grade]
        if not items:
            continue
        m = GRADE_META[grade]
        rows += f'<tr><td style="background:{m["color"]};color:#fff;padding:8px 14px;font-weight:bold;font-size:13px;">{m["emoji"]} {grade} ({len(items)}건)</td></tr>'
        for a in items:
            rows += f'''<tr style="background:{m['bg']};"><td style="padding:10px 14px;border-bottom:1px solid #eee;">
              <a href="{a['url']}" style="color:#1a3c6e;font-weight:bold;text-decoration:none;font-size:14px;line-height:1.6;">{a['title']}</a><br>
              <span style="color:#999;font-size:11px;">{a['url']}</span><br>
              <span style="color:#aaa;font-size:11px;">키워드: {a['keyword']}</span>
            </td></tr>'''

    urgent_count = len(sections["긴급"])
    subject_flag = "🔴 긴급 포함 " if urgent_count else ""

    html = f"""<html><body style="font-family:'맑은 고딕',Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
      <div style="max-width:680px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
        <div style="background:#1a3c6e;padding:18px 24px;">
          <h2 style="color:#fff;margin:0;font-size:17px;">📰 뉴스 리스크 모니터링</h2>
          <p style="color:#aac4e8;margin:4px 0 0;font-size:12px;">
            {now} · 총 {len(articles)}건
            (🔴 긴급 {len(sections['긴급'])} / 🟡 주의 {len(sections['주의'])} / 🟢 참고 {len(sections['참고'])})
          </p>
        </div>
        <table style="width:100%;border-collapse:collapse;">{rows}</table>
        <div style="padding:14px 24px;background:#f9f9f9;color:#bbb;font-size:11px;text-align:center;line-height:1.8;">
          AI 필터링 적용 · 키워드: {', '.join(KEYWORDS)}<br>
          ※ 본 이메일은 Claude API를 통해 발송되었습니다.<br>
          ※ 담당자 : 최진후 차장 / 이원세 대리 / 장인호 대리
        </div>
      </div></body></html>"""

    return html, subject_flag


def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(EMAIL_RECEIVERS)
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVERS, msg.as_string())
    print("이메일 발송 완료")


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 뉴스 모니터링 시작")
    seen_urls    = load_seen_urls()
    raw_articles = []

    for keyword in KEYWORDS:
        articles = crawl_naver_news(keyword)
        new = []
        for article in articles:
            if article["url"] and article["url"] not in seen_urls:
                new.append(article)
                seen_urls.add(article["url"])
        raw_articles.extend(new)
        print(f"  [{keyword}] 신규 {len(new)}건")

    save_seen_urls(seen_urls)

    if not raw_articles:
        print("신규 뉴스 없음 — 종료")
        return

    print(f"\nAI 필터링 중... (총 {len(raw_articles)}건)")
    filtered = ai_filter_and_grade(raw_articles)
    print(f"필터링 후 {len(filtered)}건 선별")

    if not filtered:
        print("증권사 리스크 관련 뉴스 없음 — 이메일 미발송")
        return

    now_str = datetime.now().strftime("%m/%d %H:%M")
    html, flag = build_email_html(filtered)
    send_email(f"[뉴스 리스크] {flag}{now_str} · {len(filtered)}건", html)


if __name__ == "__main__":
    main()
