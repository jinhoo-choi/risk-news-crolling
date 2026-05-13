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
from email.utils import parsedate_to_datetime as _pdt

# ─────────────────────────────────────────────
# 설정 — GitHub Secrets에서 자동으로 읽어옴
# ─────────────────────────────────────────────
EMAIL_SENDER      = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]
EMAIL_RECEIVERS   = [e.strip() for e in os.environ["EMAIL_RECEIVER"].split(",")]
ANTHROPIC_KEY     = os.environ["ANTHROPIC_API_KEY"]
NAVER_CLIENT_ID   = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]

KEYWORDS = ["리스크", "회생", "상장폐지", "파산", "워크아웃", "부도", "거래정지", "자본잠식", "배임", "반대매매", "신용등급 강등", "PF 부실", "미매각", "영업정지", "신용융자", "증거금", "발행어음", "서킷브레이커"]
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
                pub_dt = _pdt(pub_date_str).astimezone(kst)
                pub_date = pub_dt.date()
            except Exception:
                pub_date = today_kst

            if pub_date < today_kst:
                stop = True
                break

            title = BeautifulSoup(item.get("title", ""), "html.parser").get_text()
            desc  = BeautifulSoup(item.get("description", ""), "html.parser").get_text()
            link  = item.get("originallink") or item.get("link", "")
            pub   = item.get("pubDate", "")
            if title and link:
                articles.append({
                    "title"  : title,
                    "desc"   : desc[:80] if desc else "",
                    "url"    : link,
                    "pubDate": pub,
                    "keyword": keyword,
                    "body"   : "",  # 선별 후 크롤링
                })

        total = data.get("total", 0)
        start += 100
        if stop or start > min(total, 1000):
            break

    return articles


def fetch_article_body(url: str) -> str:
    """기사 본문 크롤링 — 실패 시 빈 문자열 반환"""
    try:
        res = requests.get(url, timeout=8, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        # 네이버 뉴스 본문 선택자
        for selector in ["#dic_area", "#articleBodyContents", ".article-body", "#articeBody", "article"]:
            tag = soup.select_one(selector)
            if tag:
                text = tag.get_text(separator=" ", strip=True)
                return text[:600]  # 600자로 제한
        # 선택자 실패 시 p 태그 전체
        paragraphs = soup.find_all("p")
        text = " ".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20)
        return text[:600]
    except Exception:
        return ""


def ai_filter_batch(batch: list, offset: int = 0) -> list:
    """30건씩 배치로 AI 필터링"""
    if not batch:
        return []

    numbered = "\n".join([
        f"{i+offset+1}. {a['title']}\n   요약: {a.get('desc','')}"
        for i, a in enumerate(batch)
    ])

    prompt = f"""당신은 한국투자증권 리스크 관리 전문가입니다.
아래 뉴스 제목들을 보고 증권사 실무 관점에서 엄격하게 판단하세요.

[관련 있음 — relevant: true 조건]
다음 중 하나라도 해당하면 관련 있음:
- 기업 부도·파산·회생·워크아웃·상장폐지가 확정되었거나 신청·징후 단계인 기사
- 금융당국(금감원·금융위)의 증권사 대상 조사·검사·제재·규제 강화 기사
- 부동산PF, 브릿지론, 미매각채권 관련 손실·부실 기사
- 반대매매 급증, 마진콜, 신용융자 한도 소진·중단, 증거금 부족 관련 시장 충격 기사
- 서킷브레이커 발동, 종목 증거금률 대폭 상향 등 시장 거래 제한 기사
- 발행어음·IMA 관련 증권사 유동성 위기·만기 불일치 기사
- 신용융자 잔고 급증으로 인한 반대매매 우려·증권사 리스크 관리 강화 기사
- 특정 기업·업종의 신용등급 강등·부실로 증권사 익스포저 손실 우려 기사
- 증권사가 직접 당사자인 제재·손실·건전성 악화 기사

[관련 없음 — relevant: false 조건]
다음 중 하나라도 해당하면 반드시 제외:
- 단순히 "리스크", "파산", "위기" 등 용어만 언급하는 분석·전망·칼럼·오피니언 기사
- 학술·연구·교육·세미나·보고서·강의 관련 기사
- 해외 사례 기사 (국내 증권사에 직접 영향 없는 것)
- 일반 기업 경영 이슈로 금융권 익스포저가 없는 기사
- 제목에 구체적 기업명·사건 없이 일반론만 언급하는 기사
- 수출 확대·실적 개선·신사업 진출·수상·협약·MOU 등 긍정적 기사
- 주가 상승·목표주가 상향·투자의견 매수 등 호재성 기사 (본문에 타 기업 리스크 언급이 있어도 제목이 호재성이면 제외)
- 기사 제목의 주인공이 리스크 상황이 아닌 경우, 본문의 타 기업 리스크 언급만으로 관련 있음 처리 금지
- 기업 성장·투자 유치·흑자 전환·신규 상장 등 호재성 기사
- 제품 출시·마케팅·홍보·이벤트 관련 기사
- 증거금·신용융자 관련 투자 교육·이용 방법·금리 비교 안내 기사
- 공모주·IPO 청약 증거금 관련 기사
- 증거금이 신용·담보 증거금률 변경이 아닌 청약·납입 맥락으로 사용된 기사
- 단순 시황·지수 등락 보도 (구체적 리스크 사건 없는 것)

[등급 기준] — relevant: true인 경우만 적용
- 긴급: 아래 중 하나 해당
  · 부도·파산·회생·상폐 확정 또는 신청 (신청 단계도 긴급)
  · 리츠·펀드·ETF 등 증권사 판매 금융상품의 기초자산 부실·회생·상폐 신청
  · 금융당국 조사·제재 착수
  · 증권사 직접 손실 발생 또는 임박
  · 서킷브레이커 발동 또는 시장 전반 충격 현실화
  · 반대매매 급증·신용융자 한도 전면 중단 등 시장 충격 현실화
  · 특정 기업이 100억원 이상 채무 미상환·디폴트 선언
  · 종목 증거금률 100% 상향 등 극단적 거래 제한 조치
- 주의: 징후·가능성 단계, 모니터링 필요한 잠재 리스크
- 참고: 업황 파악에 유용하나 직접 위험은 낮은 것

[중복 기사 처리]
- 동일한 사건·이슈를 다른 언론사가 보도한 경우, id 숫자 작은 것 1건만 relevant:true로 처리
- 나머지 동일 사건 기사는 relevant:false로 처리
- 제목이 다르더라도 핵심 사건(기업명+사건유형)이 동일하면 중복으로 판단

반드시 JSON 배열만 반환하세요. 마크다운 코드블록(\`\`\`) 없이 순수 JSON만.
- reason: 선별 이유를 증권사 실무 관점에서 20자 이내로 (relevant=false면 null)
- action: relevant:true인 모든 기사에 대해 실무 담당자가 즉시 취해야 할 구체적 조치를 50자 이내로 작성하세요.
  "보고", "공유", "전달" 등 보고 행위는 제외하고 실제 확인·점검·산출 등 실무 행동만 기재.
  등급별 작성 기준:
  - 긴급: [확인 대상] + [즉시 조치] + [기한] 포함. 예) "OO 보유 채권 담보 현황 즉시 파악, 금일 내 평가손 산출"
  - 주의: [모니터링 주기] + [악화 시 트리거 조건] 포함. 예) "주 1회 잔고 추이 점검, 신용등급 추가 강등 시 즉시 대응"
  - 참고: [업황 시사점] + [선제적 점검 항목] 포함. 예) "동종업계 PF 만기 구조 비교, 자사 익스포저 비중 점검"
  기사 유형별 참고 패턴:
  - 회생·파산·부도: 보유 채권 담보 현황 및 선순위 여부 파악
  - 금감원·금융위 조사·제재: 컴플라이언스 소명자료 및 관련 계약 현황 점검
  - PF·브릿지론 부실: 만기 도래 현황 및 미매각 잔액 파악
  - 신용등급 강등: 해당 채권 듀레이션 및 평가손 산출
  - 반대매매·신용융자: 반대매매 가능 규모 및 담보 부족 계좌 현황 파악
  - 리츠·펀드 부실: 기초자산 담보가치 및 선순위 채권 현황 확인
  (relevant=false면 null)
- entity: 기사의 핵심 기업명 또는 주체 1개 (예: 태영건설, 금감원, 홈플러스) (relevant=false면 null)
반환 형식 예시:
[{{"id":1,"relevant":true,"grade":"긴급","reason":"400억 채무불이행·회생신청","action":"해당 리츠 보유 고객 전수 파악 및 손실 시나리오 작성","entity":"제이알글로벌리츠"}},{{"id":2,"relevant":false,"grade":null,"reason":null,"action":null,"entity":null}}]

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
                "max_tokens": 4000,
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
                article["action"] = info.get("action", "")
                article["entity"] = info.get("entity", "")
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
        "긴급": {"header_bg":"#fdf0ef","border_left":"#e57373","label_color":"#c0392b","card_bg":"#fff8f8","card_border":"#f5c6c6"},
        "주의": {"header_bg":"#fefce8","border_left":"#f0b429","label_color":"#b7791f","card_bg":"#fffdf0","card_border":"#f5e09a"},
        "참고": {"header_bg":"#f0faf4","border_left":"#48bb78","label_color":"#276749","card_bg":"#f8fff9","card_border":"#b2dfca"},
    }
    rows = ""
    for grade in ["긴급", "주의", "참고"]:
        items = sections[grade]
        if not items:
            continue
        m = GRADE_META[grade]
        gs = GRADE_STYLE[grade]
        rows += f'''
        <div style="padding:10px 18px 8px;background:{gs["header_bg"]};border-left:4px solid {gs["border_left"]};margin:16px 18px 0;border-radius:6px 6px 0 0;border:1px solid {gs["card_border"]};border-bottom:none;">
          <span style="font-size:16px;font-weight:600;color:{gs["label_color"]};">{grade} · {len(items)}건</span>
        </div>'''
        for a in items:
            rows += f'''
        <div style="border:1px solid {gs["card_border"]};border-top:none;background:{gs["card_bg"]};margin:0 18px 0;padding:14px 16px;border-bottom:1px solid {gs["card_border"]};">
          <a href="{a['url']}" style="font-weight:600;font-size:17px;text-decoration:none;display:block;margin-bottom:5px;line-height:1.6;color:#1e3a6e;word-break:keep-all;">{a['title']}</a>
          <div style="font-size:13px;margin-bottom:6px;">
            <a href="{a['url']}" style="color:#3b5491;text-decoration:none;">↗ 기사 보기</a>
            &nbsp;
            {f'<span style="color:#94a3b8;">{a["pub_str"]}</span>' if a.get("pub_str") else ""}
            &nbsp;<span style="color:#16a34a;font-weight:500;">● 신규</span>
          </div>
          {f'<div style="font-size:14px;color:#64748b;margin-bottom:6px;word-break:keep-all;">{a["desc"]}</div>' if a.get("desc") else ""}
          {f'<div style="border-top:1px solid #e8d5d5;padding-top:8px;margin-top:8px;"><div style="font-size:12px;font-weight:700;color:#c0392b;letter-spacing:0.8px;margin-bottom:4px;">대응방안</div><div style="font-size:14px;color:#1e293b;line-height:1.6;word-break:keep-all;">{a["action"]}</div></div>' if a.get("action") else ""}
        </div>'''  

    html = f"""<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:12px;background:#f4f6f9;font-family:'맑은 고딕',Arial,sans-serif;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;border:1px solid #e2e8f0;">

  <div style="background:linear-gradient(135deg,#4f6fad 0%,#3b5491 100%);padding:22px 26px;">
    <div style="color:#fff;font-size:21px;font-weight:500;margin-bottom:10px;">
      🤖 eBiz본부 리스크 탐지봇
      <span style="font-size:13px;background:rgba(255,255,255,0.2);color:#fff;padding:3px 9px;border-radius:20px;margin-left:8px;vertical-align:middle;">Powered by Claude AI</span>
    </div>
    <div style="color:rgba(255,255,255,0.85);font-size:15px;line-height:1.7;margin-bottom:12px;">
      {now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 (한국시간) &nbsp;·&nbsp;
      수집 {total_count}건 → AI 필터링 후 {len(articles)}건 선별 ({round((1 - len(articles)/total_count)*100) if total_count else 0}% 제거)
    </div>
    <div>
      <span style="display:inline-block;background:#c0392b;color:#fff;font-size:14px;font-weight:500;padding:4px 14px;border-radius:20px;margin-right:6px;">🔴 긴급 {len(sections['긴급'])}건</span>
      <span style="display:inline-block;background:#d97706;color:#fff;font-size:14px;font-weight:500;padding:4px 14px;border-radius:20px;margin-right:6px;">🟡 주의 {len(sections['주의'])}건</span>
      <span style="display:inline-block;background:#276749;color:#fff;font-size:14px;font-weight:500;padding:4px 14px;border-radius:20px;">🟢 참고 {len(sections['참고'])}건</span>
    </div>
  </div>

  <div style="padding:16px 22px;background:#fff;border-bottom:1px solid #e2e8f0;">
    <div style="font-size:16px;font-weight:500;color:#3b5491;margin-bottom:8px;">AI 분석 요약</div>
    <div style="font-size:14px;color:#475569;line-height:1.6;">{ai_summary.replace(chr(10), "<br>")}</div>
  </div>

  {rows}

  <div style="padding:10px 22px;background:#fff;border-top:1px solid #e2e8f0;border-bottom:1px solid #e2e8f0;font-size:13px;color:#94a3b8;line-height:1.9;">
    <strong style="color:#334155;">등급 기준</strong><br>
    <strong style="color:#c0392b;">긴급</strong> · 부도·파산·회생·상폐 확정 또는 신청 / 리츠·펀드 기초자산 부실 / 금융당국 조사·제재 / 채무불이행<br>
    <strong style="color:#b7791f;">주의</strong> · 징후·가능성 단계 / 모니터링 필요 잠재 리스크<br>
    <strong style="color:#276749;">참고</strong> · 업황 파악 목적 / 직접 위험 낮음
  </div>

  <div style="padding:14px 22px;background:#fff;color:#94a3b8;font-size:13px;line-height:2.0;">
    ※ 본 이메일은 네이버API로 수집한 뉴스를 Claude AI가 eBiz본부의 관점으로 리스크 분석하여 선별, 발송하였습니다.<br>
    ※ 담당자<br>
    &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(정) 최진후 차장<br>
    &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(부) 이원세 대리 · 장인호 대리
  </div>

</div></body></html>"""

    return html


def build_empty_html(now) -> str:
    return f"""<html><body style="font-family:'맑은 고딕',Arial,sans-serif;background:#f4f6f9;margin:0;padding:20px;">
      <div style="max-width:680px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;border:0.5px solid #e2e8f0;">
        <div style="background:linear-gradient(135deg,#4f6fad 0%,#3b5491 100%);padding:22px 26px;">
          <div style="color:#fff;font-size:21px;font-weight:500;margin-bottom:6px;">
            🤖 eBiz본부 리스크 탐지봇
            <span style="font-size:13px;background:rgba(255,255,255,0.2);color:#fff;padding:3px 9px;border-radius:20px;margin-left:8px;vertical-align:middle;">Powered by Claude AI</span>
          </div>
          <div style="color:rgba(255,255,255,0.85);font-size:15px;">{now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 (한국시간)</div>
        </div>
        <div style="padding:36px 24px;text-align:center;color:#64748b;font-size:18px;line-height:1.8;">
          AI 리스크 탐지 결과<br>해당하는 뉴스가 없습니다.
        </div>
        <div style="padding:14px 22px;background:#fff;border-top:0.5px solid #e2e8f0;color:#94a3b8;font-size:13px;line-height:2.0;">
          ※ 본 이메일은 네이버API로 수집한 뉴스를 Claude AI가 eBiz본부의 관점으로 리스크 분석하여 선별, 발송하였습니다.<br>
          ※ 담당자<br>
          &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(정) 최진후 차장<br>
          &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(부) 이원세 대리 · 장인호 대리
        </div>
      </div></body></html>"""


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
        kst_tz = timezone(timedelta(hours=9))
        new = []
        for article in articles:
            if article["url"] and article["url"] not in seen_urls:
                try:
                    pub_dt = _pdt(article.get("pubDate","")).astimezone(kst_tz)
                    article["pub_str"] = pub_dt.strftime("%m/%d %H:%M")
                except Exception:
                    article["pub_str"] = ""
                new.append(article)
                seen_urls.add(article["url"])
        raw_articles.extend(new)
        print(f"  [{keyword}] 신규 {len(new)}건")

    save_seen_urls(seen_urls)

    if not raw_articles:
        print("신규 뉴스 없음 — 결과 없음 메일 발송")
        now = datetime.now(timezone(timedelta(hours=9)))
        now_str = now.strftime("%m월%d일 %H시")
        subject = f"(eBiz본부) 리스크 탐지 결과_{now_str} 기준"
        send_email(subject, build_empty_html(now))
        return

    print(f"\nAI 필터링 중... (총 {len(raw_articles)}건)")
    filtered = ai_filter_and_grade(raw_articles)
    print(f"필터링 후 {len(filtered)}건 선별")

    # 본문 크롤링 — 선별된 기사에만 적용
    if filtered:
        print("  본문 크롤링 중...")
        for a in filtered:
            a["body"] = fetch_article_body(a["url"])

    now = datetime.now(timezone(timedelta(hours=9)))  # 한국시간 KST
    now_str = now.strftime("%m월%d일 %H시")
    subject = f"(eBiz본부) 리스크 탐지 결과_{now_str} 기준"
    total_count = len(raw_articles)

    if not filtered:
        print("증권사 리스크 관련 뉴스 없음 — 결과 없음 메일 발송")
        send_email(subject, build_empty_html(now))
        return

    # AI 전체 요약 생성
    grade_summary = []
    if len([a for a in filtered if a["grade"]=="긴급"]) > 0:
        grade_summary.append(f"긴급 {len([a for a in filtered if a['grade']=='긴급'])}건")
    if len([a for a in filtered if a["grade"]=="주의"]) > 0:
        grade_summary.append(f"주의 {len([a for a in filtered if a['grade']=='주의'])}건")
    # AI에게 오늘 리스크 성격 요약 요청
    filtered_titles = "\n".join([f"- [{a['grade']}] {a['title']}" for a in filtered])
    try:
        sum_res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 150,
                "messages": [{"role": "user", "content": f"아래 오늘의 리스크 기사 목록을 보고, 증권사 리스크 담당자를 위해 아래 형식으로 작성하세요.\n\n▸ 오늘의 리스크 성격\n(한 문장으로 오늘 전반적인 리스크 흐름)\n\n▸ 핵심 이슈\n(가장 중요한 이슈를 · 로 구분해 각각 줄바꿈)\n\n▸ 주목 포인트\n(담당자가 특히 주의할 사항 한 줄)\n\n숫자 통계 없이 내용 중심으로. 소제목 바로 다음 줄에 내용을 붙여 쓰고, 빈 줄 없이 작성하세요. 항목 사이만 한 줄 띄우세요.\n\n{filtered_titles}"}],
            },
            timeout=15,
        )
        ai_summary = sum_res.json()["content"][0]["text"].strip()
    except Exception:
        grade_str = ", ".join(grade_summary) if grade_summary else "없음"
        ai_summary = f"총 {total_count}건 수집 중 {len(filtered)}건 선별. {grade_str} 감지."

    html = build_email_html(filtered, total_count=total_count, ai_summary=ai_summary)
    send_email(subject, html)


if __name__ == "__main__":
    main()
