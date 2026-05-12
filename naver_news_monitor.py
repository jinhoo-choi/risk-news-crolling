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
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
# 설정 — GitHub Secrets에서 자동으로 읽어옴
# ─────────────────────────────────────────────
EMAIL_SENDER      = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]
EMAIL_RECEIVERS   = [e.strip() for e in os.environ["EMAIL_RECEIVER"].split(",")]
ANTHROPIC_KEY     = os.environ["ANTHROPIC_API_KEY"]
NAVER_CLIENT_ID   = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]

KEYWORDS = ["리스크", "회생", "기업회생", "상장폐지", "파산", "워크아웃", "부도", "거래정지", "자본잠식", "횡령", "배임", "반대매매", "주가조작", "신용등급 강등", "PF 부실", "미매각", "영업정지", "자산유동화"]
MAX_NEWS_PER_KEYWORD = 1000  # 당일 기사 전체 수집 (pubDate 필터로 제한됨)
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
    """네이버 검색 API로 뉴스 수집 — 당일(KST) 기사만"""
    from email.utils import parsedate_to_datetime
    kst = timezone(timedelta(hours=9))
    today_kst = datetime.now(kst).date()

    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    articles = []
    start = 1

    while True:
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

        stop = False
        for item in items:
            pub_date_str = item.get("pubDate", "")
            try:
                pub_dt = parsedate_to_datetime(pub_date_str).astimezone(kst)
                pub_date = pub_dt.date()
            except Exception:
                pub_date = today_kst

            if pub_date < today_kst:
                stop = True
                break

            title = BeautifulSoup(item.get("title", ""), "html.parser").get_text()
            link  = item.get("originallink") or item.get("link", "")
            if title and link:
                articles.append({
                    "title"  : title,
                    "url"    : link,
                    "keyword": keyword,
                })

        total = data.get("total", 0)
        start += 100
        if stop or start > min(total, 1000):
            break

    return articles


def ai_filter_batch(batch: list, offset: int = 0) -> list:
    """30건씩 배치로 AI 필터링"""
    if not batch:
        return []

    numbered = "\n".join([f"{i+offset+1}. {a['title']}" for i, a in enumerate(batch)])

    prompt = f"""당신은 한국투자증권 리스크 관리 전문가입니다.
아래 뉴스 제목들을 보고 증권사 실무 관점에서 엄격하게 판단하세요.

[관련 있음 — relevant: true 조건]
다음 중 하나라도 해당하면 관련 있음:
- 기업 부도·파산·회생·워크아웃·상장폐지가 확정되었거나 신청·징후 단계인 기사
- 금융당국(금감원·금융위)의 증권사 대상 조사·검사·제재·규제 강화 기사
- 부동산PF, 브릿지론, 미매각채권 관련 손실·부실 기사
- 반대매매 급증, 마진콜, 유동성 위기 등 시장 충격 기사
- 증권업 전반 수익성·건전성에 직접 영향을 주는 거시 리스크 기사

[관련 없음 — relevant: false 조건]
다음 중 하나라도 해당하면 반드시 제외:
- 단순히 "리스크", "파산", "위기" 등 용어만 언급하는 분석·전망·칼럼 기사
- 학술·연구·교육·세미나·보고서 관련 기사
- 해외 사례 기사 (국내 증권사에 직접 영향 없는 것)
- 일반 기업 경영 이슈로 금융권 익스포저가 없는 기사
- 제목에 구체적 기업명·사건 없이 일반론만 언급하는 기사
- 수출 확대·실적 개선·신사업 진출·수상·협약 등 긍정적 내용의 기사
- 기업 성장·투자 유치·흑자 전환 등 호재성 기사
- 제품 출시·마케팅·홍보성 기사

[등급 기준] — relevant: true인 경우만 적용
- 긴급: 아래 중 하나 해당
  · 부도·파산·회생·상폐 확정 또는 신청 (신청 단계도 긴급)
  · 리츠·펀드·ETF 등 증권사 판매 금융상품의 기초자산 부실·회생·상폐 신청
  · 금융당국 조사·제재 착수
  · 증권사 직접 손실 발생 또는 임박
  · 시장 전반 충격 현실화 (반대매매 급증, 뱅크런 등)
  · 특정 기업이 100억원 이상 채무 미상환·디폴트 선언
- 주의: 징후·가능성 단계, 모니터링 필요한 잠재 리스크
- 참고: 업황 파악에 유용하나 직접 위험은 낮은 것

[중복 기사 처리]
- 동일한 사건·이슈를 다른 언론사가 보도한 경우, 가장 먼저 나온 기사(id 숫자 작은 것) 1건만 relevant:true로 처리
- 나머지 동일 사건 기사는 relevant:false로 처리
- 제목이 다르더라도 핵심 사건(기업명+사건유형)이 동일하면 중복으로 판단

반드시 JSON 배열만 반환하세요. 마크다운 코드블록(\`\`\`) 없이 순수 JSON만.
- reason: 선별 이유를 증권사 실무 관점에서 20자 이내로 (relevant=false면 null)
반환 형식 예시:
[{{"id":1,"relevant":true,"grade":"긴급","reason":"400억 채무불이행·회생신청"}},{{"id":2,"relevant":false,"grade":null,"reason":null}}]

뉴스 목록:
{numbered}"""

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        res.raise_for_status()
        raw = res.json()["content"][0]["text"].strip()
        # 코드블록 제거
        raw = raw.replace("```json", "").replace("```", "").strip()
        # JSON 배열 부분만 추출 (앞뒤 불필요한 텍스트 제거)
        start_idx = raw.find("[")
        end_idx = raw.rfind("]") + 1
        if start_idx == -1 or end_idx == 0:
            raise ValueError("JSON 배열을 찾을 수 없음")
        raw = raw[start_idx:end_idx]
        grades = json.loads(raw)
        grade_map = {g["id"]: g for g in grades}

        result = []
        for i, article in enumerate(batch):
            info = grade_map.get(i + offset + 1, {})
            if info.get("relevant") and info.get("grade"):
                article["grade"] = info["grade"]
                article["reason"] = info.get("reason", "")
                result.append(article)
        return result

    except Exception as e:
        print(f"AI 필터링 오류: {e}")
        try:
            print(f"API 응답 상태코드: {res.status_code}")
            print(f"API 응답 원문: {res.text[:300]}")
        except:
            pass
        return []


def dedup_by_title(articles: list) -> list:
    """배치 경계 걸친 중복 기사 제거 — Claude API로 최종 중복 제거"""
    if not articles:
        return []

    numbered = "\n".join([f"{i+1}. {a['title']}" for i, a in enumerate(articles)])
    prompt = f"""아래 뉴스 제목 목록에서 동일한 사건·이슈를 다룬 중복 기사를 제거하세요.
동일 기업명 + 동일 사건유형이면 중복으로 판단하며, id가 가장 작은 것(먼저 나온 것)만 남기세요.

반드시 JSON 배열만 반환하세요. 마크다운 코드블록 없이 순수 JSON만.
형식: [{{"id": 유지할id}}, ...] — 유지할 기사 id만 포함

뉴스 목록:
{numbered}"""

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        res.raise_for_status()
        raw = res.json()["content"][0]["text"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        start_idx = raw.find("[")
        end_idx = raw.rfind("]") + 1
        raw = raw[start_idx:end_idx]
        keep_ids = {item["id"] for item in json.loads(raw)}
        return [a for i, a in enumerate(articles) if (i + 1) in keep_ids]
    except Exception as e:
        print(f"중복 제거 오류: {e} — 중복 제거 생략")
        return articles


def ai_filter_and_grade(articles: list) -> list:
    """전체 기사를 30건씩 배치로 나눠 AI 필터링 후 중복 제거"""
    if not articles:
        return []
    result = []
    batch_size = 30
    for i in range(0, len(articles), batch_size):
        batch = articles[i:i+batch_size]
        print(f"  배치 {i//batch_size+1}/{-(-len(articles)//batch_size)} 처리 중... ({len(batch)}건)")
        result.extend(ai_filter_batch(batch, offset=i))

    if len(result) > 1:
        print(f"  중복 제거 중... (필터링 후 {len(result)}건)")
        result = dedup_by_title(result)
        print(f"  중복 제거 후 {len(result)}건")

    return result


def build_email_html(articles: list, total_count: int = 0, ai_summary: str = ''):
    now = datetime.now(timezone(timedelta(hours=9)))  # 한국시간 KST
    sections = {"긴급": [], "주의": [], "참고": []}
    for a in articles:
        sections[a["grade"]].append(a)

    GRADE_STYLE = {
        "긴급": {"header_bg":"#fff0f0","border_left":"#f87171","label_color":"#dc2626","card_bg":"#fff8f8","card_border":"#fecaca"},
        "주의": {"header_bg":"#fffbeb","border_left":"#fbbf24","label_color":"#d97706","card_bg":"#fffdf0","card_border":"#fde68a"},
        "참고": {"header_bg":"#f0fdf4","border_left":"#4ade80","label_color":"#16a34a","card_bg":"#f8fff9","card_border":"#bbf7d0"},
    }
    rows = ""
    for grade in ["긴급", "주의", "참고"]:
        items = sections[grade]
        if not items:
            continue
        m = GRADE_META[grade]
        gs = GRADE_STYLE[grade]
        rows += f'''
        <div style="padding:8px 22px 4px;background:{gs["header_bg"]};border-left:3px solid {gs["border_left"]};margin:14px 18px 0;border-radius:0 8px 0 0;">
          <span style="font-size:13px;font-weight:500;color:{gs["label_color"]};">{m["emoji"]} {grade} · {len(items)}건</span>
        </div>'''
        for a in items:
            rows += f'''
        <div style="margin:0 18px 14px;border:0.5px solid {gs["card_border"]};border-radius:0 0 8px 8px;background:{gs["card_bg"]};padding:14px 16px;">
          <a href="{a['url']}" style="color:#1e3a6e;font-weight:500;font-size:15px;text-decoration:none;display:block;margin-bottom:6px;line-height:1.6;">{a['title']}</a>
          <div style="font-size:12px;color:#64748b;margin-bottom:4px;">🤖 {a.get('reason','')}</div>
          <div style="font-size:12px;color:#64748b;margin-bottom:5px;">{a['url']}</div>
          <span style="font-size:11px;color:#3b5491;background:#eef2ff;padding:2px 9px;border-radius:20px;">키워드: {a['keyword']}</span>
        </div>'''  

    urgent_count = len(sections["긴급"])
    subject_flag = "🔴 긴급 포함 " if urgent_count else ""

    html = f"""<html><body style="font-family:'맑은 고딕',Arial,sans-serif;background:#f4f6f9;margin:0;padding:20px;">
      <div style="max-width:680px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;border:0.5px solid #e2e8f0;">
        <div style="background:linear-gradient(135deg,#4f6fad 0%,#3b5491 100%);padding:22px 26px;">
          <div style="color:#fff;font-size:18px;font-weight:500;margin-bottom:8px;">
            🤖 eBiz본부 리스크 탐지봇
            <span style="font-size:11px;background:rgba(255,255,255,0.2);padding:3px 9px;border-radius:20px;margin-left:8px;vertical-align:middle;">Powered by Claude AI</span>
          </div>
          <div style="color:rgba(255,255,255,0.8);font-size:13px;line-height:1.7;">
            {now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 (한국시간)<br>
            수집 {total_count}건 → AI 필터링 후 {len(articles)}건 선별
            ({round((1 - len(articles)/total_count)*100) if total_count else 0}% 제거) &nbsp;·&nbsp;
            🔴 긴급 {len(sections['긴급'])} / 🟡 주의 {len(sections['주의'])} / 🟢 참고 {len(sections['참고'])}
          </div>
        </div>
        <div style="padding:16px 22px;background:#f0f5ff;border-bottom:0.5px solid #dce8ff;">
          <div style="font-size:13px;font-weight:500;color:#3b5491;margin-bottom:6px;">🤖 AI 분석 요약</div>
          <div style="font-size:13px;color:#475569;line-height:1.8;">{ai_summary}</div>
        </div>
        {rows}
        <div style="padding:12px 22px;background:#f8fafc;border-top:0.5px solid #e8edf5;border-bottom:0.5px solid #e8edf5;font-size:12px;color:#64748b;line-height:2.0;">
          <strong style="color:#334155;">📌 등급 기준</strong><br>
          🔴 <strong style="color:#334155;">긴급</strong> · 부도·파산·회생·상폐 확정 또는 신청 / 리츠·펀드 기초자산 부실 / 금융당국 조사·제재 / 100억↑ 채무불이행<br>
          🟡 <strong style="color:#334155;">주의</strong> · 징후·가능성 단계 / 모니터링 필요 잠재 리스크<br>
          🟢 <strong style="color:#334155;">참고</strong> · 업황 파악 목적 / 직접 위험 낮음
        </div>
        <div style="padding:14px 22px;background:#f8fafc;color:#94a3b8;font-size:12px;line-height:2.0;">
          AI 필터링 적용 · 키워드: {', '.join(KEYWORDS)}<br>
          ※ 본 이메일은 네이버API로 수집한 뉴스를 Claude AI가 eBiz본부의 관점으로 리스크 분석하여 선별, 발송하였습니다.<br>
          ※ 담당자 (정) 최진후 차장 / (부) 이원세 대리 · 장인호 대리
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

    now = datetime.now(timezone(timedelta(hours=9)))  # 한국시간 KST
    now_str = now.strftime("%m월%d일 %H시 %M분")
    subject = f"(eBiz본부) AI 뉴스기사 모니터링 결과_{now_str} 기준"
    total_count = len(raw_articles)

    if not filtered:
        print("증권사 리스크 관련 뉴스 없음 — 결과 없음 메일 발송")
        empty_html = f"""<html><body style="font-family:'맑은 고딕',Arial,sans-serif;background:#f4f6f9;margin:0;padding:20px;">
      <div style="max-width:680px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;border:0.5px solid #e2e8f0;">
        <div style="background:linear-gradient(135deg,#4f6fad 0%,#3b5491 100%);padding:22px 26px;">
          <div style="color:#fff;font-size:18px;font-weight:500;margin-bottom:6px;">
            🤖 eBiz본부 리스크 탐지봇
            <span style="font-size:11px;background:rgba(255,255,255,0.2);padding:3px 9px;border-radius:20px;margin-left:8px;vertical-align:middle;">Powered by Claude AI</span>
          </div>
          <div style="color:rgba(255,255,255,0.8);font-size:13px;">{now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 (한국시간)</div>
        </div>
        <div style="padding:36px 24px;text-align:center;color:#64748b;font-size:15px;line-height:1.8;">
          AI 뉴스기사 모니터링 결과<br>리스크에 해당하는 기사가 없습니다.
        </div>
        <div style="padding:14px 22px;background:#f8fafc;border-top:0.5px solid #e2e8f0;color:#94a3b8;font-size:12px;line-height:2.0;">
          AI 필터링 적용 · 키워드: {', '.join(KEYWORDS)}<br>
          ※ 본 이메일은 네이버API로 수집한 뉴스를 Claude AI가 eBiz본부의 관점으로 리스크 분석하여 선별, 발송하였습니다.<br>
          ※ 담당자 (정) 최진후 차장 / (부) 이원세 대리 · 장인호 대리
        </div>
      </div></body></html>"""
        send_email(subject, empty_html)
        return

    # AI 전체 요약 생성
    grade_summary = []
    if len([a for a in filtered if a["grade"]=="긴급"]) > 0:
        grade_summary.append(f"긴급 {len([a for a in filtered if a['grade']=='긴급'])}건")
    if len([a for a in filtered if a["grade"]=="주의"]) > 0:
        grade_summary.append(f"주의 {len([a for a in filtered if a['grade']=='주의'])}건")
    kw_top = {}
    for a in filtered:
        kw_top[a["keyword"]] = kw_top.get(a["keyword"], 0) + 1
    top_kw = sorted(kw_top, key=kw_top.get, reverse=True)[:3]
    ai_summary = f"총 {total_count}건 수집 중 {len(filtered)}건을 리스크 기사로 선별했습니다. "
    if grade_summary:
        ai_summary += f"{', '.join(grade_summary)}이 감지되었으며, "
    ai_summary += f"주요 키워드는 {', '.join(top_kw)}입니다."

    html, flag = build_email_html(filtered, total_count=total_count, ai_summary=ai_summary)
    send_email(subject, html)


if __name__ == "__main__":
    main()
