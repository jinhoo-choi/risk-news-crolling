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
import csv
import time
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

KEYWORDS = ["부실 리스크", "신용 리스크", "유동성 리스크", "디폴트 리스크", "기업회생", "상장폐지", "파산", "워크아웃", "부도", "거래정지", "자본잠식", "배임", "반대매매", "신용등급 강등", "PF 부실", "미매각", "영업정지", "신용융자", "증거금", "발행어음", "서킷브레이커"]
MAX_NEWS_PER_KEYWORD = 1000  # 당일 기사 전체 수집 (pubDate 필터로 제한됨)
SEEN_FILE = "seen_news.json"
EXPOSURE_FILE = "exposure_data.csv"

# ─────────────────────────────────────────────


def load_exposure_data() -> list:
    """CSV에서 eBiz본부 익스포저 데이터 로드 — 파일 없으면 빈 리스트"""
    if not os.path.exists(EXPOSURE_FILE):
        return []
    try:
        with open(EXPOSURE_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            return list(reader)
    except Exception:
        return []


def find_exposure(entity: str, exposure_data: list) -> list:
    """entity와 종목명 매칭 — 매칭된 행 리스트 반환"""
    if not entity or not exposure_data:
        return []
    return [row for row in exposure_data if entity in row.get("종목명", "") or row.get("종목명", "") in entity]


def load_competitor_notices() -> list:
    """경쟁사 공지사항 CSV에서 당일 신용·대출 관련 공지 로드"""
    CREDIT_KEYWORDS = [
        "신용한도", "신용융자", "신용공여", "신용거래",
        "증거금률", "증거금 변경", "반대매매",
        "대출한도", "신용대출", "신용 중단", "한도 축소",
        "신용 재개", "신용거래 제한"
    ]
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    result = []
    # broker_notices.py 출력 파일들 확인
    data_dir = "data"
    if not os.path.exists(data_dir):
        return []
    try:
        import csv as _csv
        for fname in os.listdir(data_dir):
            if not fname.endswith(".csv") or fname == "broker_notices_merged.csv":
                continue
            fpath = os.path.join(data_dir, fname)
            with open(fpath, "r", encoding="utf-8-sig") as f:
                reader = _csv.DictReader(f)
                for row in reader:
                    date = row.get("date", "")
                    title = row.get("title", "")
                    company = row.get("company", "")
                    if date != today:
                        continue
                    if any(kw in title for kw in CREDIT_KEYWORDS):
                        result.append({
                            "company": company,
                            "title": title,
                            "date": date,
                            "url": row.get("url", ""),
                        })
    except Exception as e:
        print(f"  경쟁사 공지 로드 오류: {e}")
    return result


def build_summary_html(ai_summary: str) -> str:
    """AI 분석 요약을 테이블 형식으로 렌더링"""
    lines = [l.strip() for l in ai_summary.split("\n") if l.strip()]
    rows_html = ""
    current_label = ""
    current_items = []

    def flush_row(label, items):
        if not label:
            return ""
        content_html = "<br>".join(items) if items else ""
        return f"""<tr>
          <td width="80" valign="top" style="padding:7px 10px;font-size:13px;font-weight:bold;color:#3b5491;border-bottom:1px solid #eef2ff;white-space:nowrap;">{label}</td>
          <td style="padding:7px 10px;font-size:13px;color:#334155;line-height:1.7;border-bottom:1px solid #eef2ff;">{content_html}</td>
        </tr>"""

    for line in lines:
        if line.startswith("▸"):
            rows_html += flush_row(current_label, current_items)
            current_label = line.replace("▸", "").strip()
            current_items = []
        elif line.startswith("·") or line.startswith("•"):
            current_items.append(line)
        else:
            current_items.append(line)

    rows_html += flush_row(current_label, current_items)

    return f"""<p style="margin:0 0 10px 0;font-size:15px;font-weight:bold;color:#3b5491;">AI 분석 요약</p>
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        {rows_html}
      </table>"""


def build_competitor_html(notices: list, today_str: str) -> str:
    """경쟁사 신용·대출 특이사항 HTML — 없으면 빈 문자열"""
    if not notices:
        return ""
    rows_html = ""
    for i, n in enumerate(notices):
        border = "border-bottom:1px solid #dce8ff;" if i < len(notices) - 1 else ""
        url = n.get('url', '')
        title_cell = f'<a href="{url}" style="color:#334155;text-decoration:none;">{n["title"]}</a>' if url else n['title']
        rows_html += f"""<tr>
          <td width="100" valign="middle" style="padding:7px 4px;font-size:13px;font-weight:bold;color:#1e293b;{border}">{n['company']}</td>
          <td valign="middle" style="padding:7px 4px;font-size:13px;{border}">{title_cell}</td>
          <td align="right" valign="middle" style="padding:7px 4px;font-size:11px;color:#94a3b8;white-space:nowrap;{border}">{n['date'][5:].replace('-', '/')}</td>
        </tr>"""
    return f"""<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f0f5ff;border-bottom:1px solid #e2e8f0;">
      <tr>
        <td style="padding:14px 22px 4px 22px;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td><span style="font-size:15px;font-weight:bold;color:#3b5491;">경쟁사 신용·대출 특이사항</span></td>
              <td align="right"><span style="font-size:11px;color:#94a3b8;">{today_str} 당일 기준</span></td>
            </tr>
          </table>
        </td>
      </tr>
      <tr>
        <td style="padding:6px 22px 14px 22px;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            {rows_html}
          </table>
        </td>
      </tr>
    </table>"""


def load_seen_urls() -> set:
    """당일 날짜 기준으로 seen URL 로드 — 전날 데이터 자동 제거"""
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 구버전(리스트) 호환 처리
        if isinstance(data, list):
            return set()
        # 당일 날짜 URL만 반환
        return set(data.get(today, []))
    return set()


def save_seen_urls(seen: set):
    """당일 날짜로 seen URL 저장"""
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({today: list(seen)}, f, ensure_ascii=False)


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
        for crawl_attempt in range(3):
            try:
                res = requests.get(
                    "https://openapi.naver.com/v1/search/news.json",
                    headers=headers,
                    params=params,
                    timeout=15,
                )
                res.raise_for_status()
                data = res.json()
                break
            except Exception as e:
                if crawl_attempt < 2:
                    print(f"[{keyword}] API 오류 — {5}초 후 재시도 ({crawl_attempt+1}/3): {e}")
                    time.sleep(5)
                else:
                    print(f"[{keyword}] API 오류 — 3회 실패, 건너뜀: {e}")
                    data = {"items": []}
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
                    "desc"   : (desc[:80].rsplit(" ", 1)[0] if len(desc) > 80 and " " in desc[:80] else desc[:80]) if desc else "",
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

    prompt = f"""당신은 한국투자증권 eBiz본부 리스크 담당자입니다.
eBiz본부는 비대면 주식거래(온라인 MTS·HTS)를 핵심 사업으로 하며, 리츠·펀드·발행어음·신용융자·IMA 등 금융상품을 온라인 채널로 판매합니다.
고객 자산 손실, 당사 익스포저 손실, 금융당국 제재, 비대면 거래 시스템 리스크에 특히 민감합니다.
아래 기사가 이 네 가지 중 하나로 이어질 직접적 가능성이 있는지 엄격하게 판단하세요. 가능성이 낮으면 과감히 제외하세요.

[반드시 제외 — relevant: false 조건]
다음 중 하나라도 해당하면 즉시 제외하세요:
- 단순히 "파산", "위기" 등 용어만 언급하는 분석·전망·칼럼·오피니언 기사
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
- "전망", "예상", "가능성", "우려" 등 추측성 표현만 있고 확정된 사건이 없는 기사
- 특정 기업·사건 없이 업계 전반의 리스크를 일반론으로만 다룬 기사
- 인터뷰·기고·칼럼 형식의 기사 (기자 의견 또는 전문가 인터뷰 위주)
- 해외 기업·시장 이슈로 국내 증권사 직접 익스포저가 없는 기사
- 리스크 관련 정책·제도 변경 예고 기사 (시행 전 단계)
- 증권사 실적·수수료·영업 관련 기사 (손실·부실 아닌 것)
- "이 시각 주요 뉴스", "뉴스브리핑" 등 뉴스 모음·요약 기사
- 기획기사·시리즈 기사 (제목에 ①②③ 또는 [기획] [시리즈] [르포] 등 표시된 것)
- 리스크가 "안정화", "개선", "해소", "완화" 됐다는 긍정적 결과 기사
- 충당금 감소·자산건전성 개선·부실 축소 등 리스크 해소 방향의 기사
- M&A·사업 확장·신성장 동력 관련 기사 (부실 우려가 주된 내용이 아닌 것)
- 금융당국 태스크포스·모니터링 가동 등 예방적 조치 기사 (실제 제재·손실 없는 것)
- 보험사·캐피탈·저축은행 등 증권사와 직접 관련 없는 타 금융업권 기사 (증권사 익스포저 없는 것)
- 제목이나 본문에서 리스크를 언급하지만 전체 맥락이 긍정적 전망인 기사
- 제약·바이오·유통·제조업 등 비금융 기업 회생·파산 기사 (증권사 직접 여신·보증·판매 상품 익스포저 없는 것)
- 해외 운용사·펀드·캐피탈 관련 기사 (국내 증권사 직접 익스포저 없는 것)
- 이미 알려진 회생·부도 사건의 후속 현황·지역 영향 기사 (새로운 리스크 아닌 것)
- 수익률 1위·공모가 초과·성과 우수 등 성과 관련 기사 (본문에 타 기업 부정적 언급 있어도 제목 주인공이 호재성이면 제외)

[관련 있음 — relevant: true 조건]
위 제외 조건에 해당하지 않고 아래 중 하나라도 해당하면 관련 있음:
- 기업 부도·파산·회생·워크아웃·상장폐지가 확정되었거나 신청·징후 단계인 기사
- 금융당국(금감원·금융위)의 증권사 대상 조사·검사·제재·규제 강화 기사
- 부동산PF, 브릿지론, 미매각채권 관련 손실·부실 기사
- 반대매매 급증, 마진콜, 신용융자 한도 소진·중단, 증거금 부족 관련 시장 충격 기사
- 서킷브레이커 발동, 종목 증거금률 대폭 상향 등 시장 거래 제한 기사
- 발행어음·IMA 관련 증권사 유동성 위기·만기 불일치 기사
- 비대면 주식거래 시스템 장애·해킹·전산 사고 관련 기사
- 신용융자 잔고 급증으로 인한 반대매매 우려·증권사 리스크 관리 강화 기사
- 특정 기업·업종의 신용등급 강등·부실로 증권사 익스포저 손실 우려 기사
- 증권사가 직접 당사자인 제재·손실·건전성 악화 기사

[등급 기준] — relevant: true인 경우만 적용
- 긴급: 아래 중 하나 해당
  · 부도·파산·회생·상폐 확정 또는 신청 (신청 단계도 긴급)
  · 리츠·펀드·ETF 등 증권사 판매 금융상품의 기초자산 부실·회생·상폐 신청
  · 금융당국 조사·제재 착수 (예고·검토 단계 제외, 실제 착수 확정만)
  · 증권사 직접 손실 발생 또는 임박
  · 서킷브레이커 발동 또는 시장 전반 충격 현실화
  · 반대매매 급증·신용융자 한도 전면 중단 등 시장 충격 현실화
  · 특정 기업이 100억원 이상 채무 미상환·디폴트 선언
  · 종목 증거금률 100% 상향 등 극단적 거래 제한 조치
- 주의: 아래 중 하나 해당
  · 회생·부도·상폐 가능성이 언론에서 처음 언급된 단계 (확정·신청 전)
  · 금융당국 조사·제재 예고·검토 단계 (착수 확정 전)
  · 신용등급 강등 경고(Negative Watch·Outlook) 단계
  · PF·브릿지론 부실 징후가 있으나 손실 미확정 단계
  · 반대매매 우려·신용융자 한도 부분 축소 등 시장 충격 징후 단계
- 참고: 업황 파악에 유용하나 직접 위험은 낮은 것

[중복 기사 처리 — 반드시 엄격히 적용]
- 동일한 사건·이슈를 다른 언론사가 보도한 경우, id 숫자 가장 작은 것 1건만 relevant:true
- 나머지 동일 사건 기사는 무조건 relevant:false
- 제목이 달라도 핵심 사건(기업명+사건유형)이 동일하면 중복
- 동일 정책·제도 변경(예: 동전주 상장폐지, 신용융자 잔고 현황 등)은 1건만 선택
- 같은 기업의 같은 날 다른 측면을 다룬 기사도 가장 핵심적인 1건만 선택
- 중복 의심 시 반드시 제외 (차라리 제외하는 게 나음)

반드시 JSON 배열만 반환하세요. 마크다운 코드블록(```) 없이 순수 JSON만.
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
- entity: 기사의 핵심 기업명 또는 종목명을 공식 명칭 기준으로 1개 추출 (예: 태영건설, 홈플러스, 제이알글로벌리츠, 한화솔루션). 금감원·금융위 등 기관명은 제외하고 기업·종목명만 추출. (relevant=false면 null)
반환 형식 예시 (긴급/주의/참고/제외 각 1건):
[
  {{"id":1,"relevant":true,"grade":"긴급","reason":"리츠 기초자산 회생신청·손실 확정","action":"해당 리츠 보유 고객 전수 파악 및 금일 내 평가손 산출","entity":"제이알글로벌리츠"}},
  {{"id":2,"relevant":true,"grade":"주의","reason":"PF 부실 징후·손실 미확정 단계","action":"주 1회 PF 잔액 추이 점검, 연체 발생 시 즉시 대응","entity":"태영건설"}},
  {{"id":3,"relevant":true,"grade":"참고","reason":"업계 발행어음 증가 동향","action":"동종업계 발행어음 만기 구조 비교, 자사 유동성 비율 점검","entity":"미래에셋증권"}},
  {{"id":4,"relevant":false,"grade":null,"reason":null,"action":null,"entity":null}}
]

뉴스 목록:
{numbered}"""

    for attempt in range(3):
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
                    "temperature": 0.0,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            if res.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"  Rate limit 429 — {wait}초 대기 후 재시도 ({attempt+1}/3)")
                time.sleep(wait)
                continue
            res.raise_for_status()
            raw = res.json()["content"][0]["text"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
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
            if attempt < 2:
                time.sleep(30)
                continue
            return []
    return []


def dedup_by_title(articles: list) -> list:
    """배치 경계 걸친 중복 기사 제거 — Claude API로 최종 중복 제거"""
    if not articles:
        return []

    numbered = "\n".join([f"{i+1}. {a['title']}" for i, a in enumerate(articles)])
    prompt = f"""아래 뉴스 제목 목록에서 중복 기사를 제거하세요.

[중복 판단 기준 — 아래 중 하나라도 해당하면 중복]
1. 동일 기업명 + 동일 사건유형 (예: 한화솔루션 유상증자, 동전주 상장폐지, 신용융자 급증)
2. 제목 핵심 내용이 80% 이상 동일
3. 동일 정책·제도 변경을 여러 언론사가 보도한 경우 (예: 동전주 상장폐지 7월 시행 관련 기사 다수)
4. 동일 인물·기업의 동일 사건을 다른 각도로 보도한 경우

중복이면 id가 가장 작은 것(먼저 나온 것) 1건만 남기고 나머지는 제거하세요.

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
                "max_tokens": 1000,
                "temperature": 0.0,
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
        if i + batch_size < len(articles):
            time.sleep(5)  # 배치 간 5초 대기

    if len(result) > 1:
        print(f"  중복 제거 중... (필터링 후 {len(result)}건)")
        result = dedup_by_title(result)
        print(f"  중복 제거 후 {len(result)}건")

    return result


def build_exposure_html(entity: str, exposure_data: list, ref_date: str) -> str:
    """익스포저 현황 HTML 생성 — 매칭 없으면 빈 문자열"""
    rows = find_exposure(entity, exposure_data)
    if not rows:
        return ""
    date_label = f" (기준일: {ref_date})" if ref_date else ""
    items_html = "".join([
        f'<div style="font-size:13px;color:#1e293b;margin-bottom:3px;">{row.get("종목유형","")} : {int(row.get("잔고(억)","0")):,}억원 / {int(row.get("고객수","0")):,}명</div>'
        for row in rows
    ])
    return f'''<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:8px;background:#f0f4ff;border:1px solid #c7d7f5;">
      <tr><td style="padding:8px 12px;">
        <p style="margin:0 0 4px 0;font-size:12px;font-weight:bold;color:#3b5491;">eBiz본부 익스포저 현황{date_label}</p>
        {items_html}
      </td></tr>
    </table>'''


def build_email_html(articles: list, total_count: int = 0, ai_summary: str = '', exposure_data: list = None, ref_date: str = '', competitor_notices: list = None, today_str: str = ''):
    exposure_data = exposure_data or []
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
    GRADE_LIMIT = {"긴급": 999, "주의": 10, "참고": 999}  # 긴급 전건, 주의 10건, 참고 전건
    GRADE_DESC = {"긴급": "확정된 손실·부실·제재 — 당일 내 확인·점검 필요", "주의": "손실·부실 가능성 — 주시 및 선제 점검 권고", "참고": "직접 손실 없는 동향 — 참고 파악용"}
    for grade in ["긴급", "주의", "참고"]:
        items = sections[grade]
        if not items:
            continue
        gs = GRADE_STYLE[grade]
        limit = GRADE_LIMIT[grade]
        display_items = items[:limit]
        extra_items = items[limit:]
        rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:14px;border:1px solid {gs["card_border"]};border-bottom:none;background:{gs["header_bg"]};border-left:4px solid {gs["border_left"]};">
          <tr>
            <td style="padding:10px 14px;">
              <span style="font-size:15px;font-weight:bold;color:{gs["label_color"]};">{grade} · {len(items)}건</span>
            </td>
            <td align="right" style="padding:10px 14px;">
              <span style="font-size:11px;color:#94a3b8;">{GRADE_DESC[grade]}</span>
            </td>
          </tr>
        </table>'''
        for a in display_items:
            if grade == "참고":
                rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-left:1px solid {gs["card_border"]};border-right:1px solid {gs["card_border"]};border-bottom:1px solid {gs["card_border"]};background:#fafafa;">
          <tr>
            <td style="padding:7px 16px;">
              <a href="{a['url']}" style="font-size:13px;color:#475569;text-decoration:none;">{a['title']}</a>
              {f'<span style="font-size:11px;color:#94a3b8;margin-left:8px;">{a["pub_str"]}</span>' if a.get("pub_str") else ""}
            </td>
          </tr>
        </table>'''
            else:
                rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:{gs["card_bg"]};margin-bottom:10px;">
          <tr>
            <td style="padding:14px 16px;">
              <a href="{a['url']}" style="font-weight:bold;font-size:16px;text-decoration:none;color:#1e3a6e;line-height:1.6;">{a['title']}</a>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:6px 0;">
                <tr>
                  <td style="font-size:12px;"><a href="{a['url']}" style="color:#3b5491;text-decoration:none;">↗ 기사 보기</a></td>
                  <td align="right" style="font-size:11px;color:#94a3b8;">{a.get("pub_str","")}</td>
                </tr>
              </table>
              {f'<p style="margin:0 0 8px 0;font-size:13px;color:#64748b;">{a["desc"]}</p>' if a.get("desc") else ""}
              {f'<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #e8d5d5;margin-top:8px;"><tr><td style="padding-top:8px;"><p style="margin:0 0 4px 0;font-size:12px;font-weight:bold;color:{gs["label_color"]};letter-spacing:0.5px;">대응방안</p><p style="margin:0;font-size:14px;color:#1e293b;line-height:1.6;font-weight:500;">{a["action"]}</p></td></tr></table>' if a.get("action") else ""}
              {build_exposure_html(a.get("entity",""), exposure_data or [], ref_date)}
            </td>
          </tr>
        </table>'''
        if extra_items:
            extra_rows = "".join([f'''
            <tr>
              <td style="padding:4px 0;font-size:13px;color:#475569;border-bottom:1px solid #f0f0f0;">
                <a href="{e['url']}" style="color:#475569;text-decoration:none;">· {e['title'][:60]}{"..." if len(e['title']) > 60 else ""}</a>
                {f'<span style="font-size:11px;color:#94a3b8;margin-left:6px;">{e["pub_str"]}</span>' if e.get("pub_str") else ""}
              </td>
            </tr>''' for e in extra_items])
            rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:#fafafa;margin-bottom:10px;">
          <tr>
            <td style="padding:10px 16px 4px 16px;">
              <p style="margin:0 0 8px 0;font-size:12px;font-weight:bold;color:#64748b;">추가 {len(extra_items)}건</p>
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                {extra_rows}
              </table>
            </td>
          </tr>
        </table>'''  

    html = f"""<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->
</head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'맑은 고딕',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f6f9;">
<tr><td align="center" style="padding:16px;">
<table width="640" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">

  <!-- 헤더 -->
  <tr>
    <td style="background:#3b5491;padding:22px 26px;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td>
            <p style="margin:0 0 8px 0;font-size:20px;font-weight:bold;color:#ffffff;">🤖 eBiz본부 리스크 탐지봇
              <span style="font-size:12px;color:#ffffff;padding:2px 8px;background:#5a7abf;margin-left:8px;">Powered by Claude AI</span>
            </p>
            <p style="margin:0 0 12px 0;font-size:14px;color:#c8d8f0;line-height:1.6;white-space:nowrap;">
              {now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 (한국시간) · 수집 {total_count}건 → AI 필터링 후 {len(articles)}건 선별 ({round((1 - len(articles)/total_count)*100) if total_count else 0}% 제거)
            </p>
            <table cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="background:#c0392b;padding:4px 14px;">
                  <span style="font-size:13px;font-weight:bold;color:#ffffff;">긴급 {len(sections['긴급'])}건</span>
                </td>
                <td width="8"></td>
                <td style="background:#d97706;padding:4px 14px;">
                  <span style="font-size:13px;font-weight:bold;color:#ffffff;">주의 {len(sections['주의'])}건</span>
                </td>
                <td width="8"></td>
                <td style="background:#276749;padding:4px 14px;">
                  <span style="font-size:13px;font-weight:bold;color:#ffffff;">참고 {len(sections['참고'])}건</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- AI 분석 요약 -->
  {('<tr><td style="padding:14px 22px;border-bottom:1px solid #e2e8f0;background:#f8fbff;">' + build_summary_html(ai_summary) + '</td></tr>') if ai_summary else ""}

  <!-- 경쟁사 특이사항 -->
  {('<tr><td>' + build_competitor_html(competitor_notices or [], today_str) + '</td></tr>') if competitor_notices else ""}

  <!-- 뉴스 카드 -->
  <tr><td style="padding:0 22px 16px 22px;">{rows}</td></tr>

  <!-- 푸터 -->
  <tr>
    <td style="padding:14px 22px;background:#fff;border-top:1px solid #e2e8f0;">
      <p style="margin:0;font-size:12px;color:#94a3b8;line-height:2.0;">
        본 이메일은 네이버API로 수집한 뉴스를 Claude AI가 eBiz본부의 관점으로 리스크 분석하여 선별, 발송하였습니다.<br>
        담당자<br>
        &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(정) 최진후 차장<br>
        &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(부) 이원세 대리 · 장인호 대리
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body></html>"""

    return html


def build_empty_html(now) -> str:
    return f"""<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'맑은 고딕',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f6f9;">
<tr><td align="center" style="padding:16px;">
<table width="640" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">
  <tr>
    <td style="background:#3b5491;padding:22px 26px;">
      <p style="margin:0 0 6px 0;font-size:20px;font-weight:bold;color:#ffffff;">🤖 eBiz본부 리스크 탐지봇
        <span style="font-size:12px;color:#ffffff;padding:2px 8px;background:#5a7abf;margin-left:8px;">Powered by Claude AI</span>
      </p>
      <p style="margin:0;font-size:14px;color:#c8d8f0;">{now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 (한국시간)</p>
    </td>
  </tr>
  <tr>
    <td align="center" style="padding:40px 24px;">
      <p style="margin:0;font-size:17px;color:#64748b;line-height:1.8;">AI 리스크 탐지 결과<br>해당하는 뉴스가 없습니다.</p>
    </td>
  </tr>
  <tr>
    <td style="padding:14px 22px;border-top:1px solid #e2e8f0;">
      <p style="margin:0;font-size:12px;color:#94a3b8;line-height:2.0;">
        본 이메일은 네이버API로 수집한 뉴스를 Claude AI가 eBiz본부의 관점으로 리스크 분석하여 선별, 발송하였습니다.<br>
        담당자<br>
        &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(정) 최진후 차장<br>
        &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(부) 이원세 대리 · 장인호 대리
      </p>
    </td>
  </tr>
</table>
</td></tr>
</table>
</body></html>"""


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

    if not filtered:
        print("증권사 리스크 관련 뉴스 없음 — 결과 없음 메일 발송")
        now = datetime.now(timezone(timedelta(hours=9)))
        now_str = now.strftime("%m월%d일 %H시")
        subject = f"(eBiz본부) 리스크 탐지 결과_{now_str} 기준"
        send_email(subject, build_empty_html(now))
        return

    # 본문 크롤링 + 대응방안 재생성 — 선별된 기사에만 적용
    print("  본문 크롤링 중...")
    for a in filtered:
        a["body"] = fetch_article_body(a["url"])
    # 본문 기반으로 대응방안 재생성
    print("  대응방안 재생성 중...")
    for a in filtered:
        if a.get("grade") == "참고":  # 참고는 제목 목록만 표시 — 대응방안 불필요
            continue
        body_text = a.get("body", "")
        if not body_text:  # 본문 크롤링 실패 시 기존 action 유지
            continue
        try:
            action_res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 150,
                    "temperature": 0.0,
                    "messages": [{"role": "user", "content": f"""한국투자증권 eBiz본부 리스크 담당자 입장에서 아래 기사의 본문을 읽고 즉시 취해야 할 구체적 조치를 작성하세요.

규칙:
- 보고·공유·전달 등 보고 행위 제외
- 실제 확인·점검·산출 등 실무 행동만 기재
- 한 문장, 50자 이내

등급별 기준:
- 긴급: [확인 대상] + [즉시 조치] + [기한]. 예) "OO 채권 담보 현황 즉시 파악, 금일 내 평가손 산출"
- 주의: [모니터링 주기] + [악화 시 트리거]. 예) "주 1회 잔고 추이 점검, 신용등급 추가 강등 시 즉시 대응"
- 참고: [시사점] + [선제 점검]. 예) "동종업계 PF 만기 구조 비교, 자사 익스포저 비중 점검"

유형별 참고:
- 회생·파산: 보유 채권 담보 현황 및 선순위 여부 파악
- 금감원 제재: 컴플라이언스 소명자료 및 관련 계약 점검
- PF·브릿지론: 만기 도래 현황 및 미매각 잔액 파악
- 신용등급 강등: 해당 채권 듀레이션 및 평가손 산출
- 반대매매·신용융자: 반대매매 가능 규모 및 담보 부족 계좌 파악
- 리츠·펀드 부실: 기초자산 담보가치 및 선순위 채권 확인
- 시스템 장애·해킹: 영향 범위 즉시 확인 및 고객 피해 현황 파악

등급: {a['grade']}
제목: {a['title']}
본문: {body_text[:400]}

조치만 한 문장으로 반환하세요."""}],
                },
                timeout=10,
            )
            new_action = action_res.json()["content"][0]["text"].strip()
            if new_action:
                a["action"] = new_action
        except Exception:
            pass

    now = datetime.now(timezone(timedelta(hours=9)))
    today_str = now.strftime("%m월 %d일")
    exposure_data = load_exposure_data()
    competitor_notices = load_competitor_notices()
    if competitor_notices:
        print(f"  경쟁사 신용·대출 특이사항 {len(competitor_notices)}건 발견")
    else:
        print("  경쟁사 신용·대출 특이사항 없음")
    if exposure_data:
        ref_date = exposure_data[0].get("기준일", "")
        print(f"  익스포저 데이터 로드 완료 ({len(exposure_data)}건, 기준일: {ref_date})")
    else:
        ref_date = ""
        print("  익스포저 데이터 없음 — CSV 파일 미확인")

    now_str = now.strftime("%m월%d일 %H시")
    subject = f"(eBiz본부) 리스크 탐지 결과_{now_str} 기준"
    total_count = len(raw_articles)

    # AI 전체 요약 생성
    grade_summary = []
    if len([a for a in filtered if a["grade"]=="긴급"]) > 0:
        grade_summary.append(f"긴급 {len([a for a in filtered if a['grade']=='긴급'])}건")
    if len([a for a in filtered if a["grade"]=="주의"]) > 0:
        grade_summary.append(f"주의 {len([a for a in filtered if a['grade']=='주의'])}건")
    # AI에게 오늘 리스크 성격 요약 요청
    filtered_titles = "\n".join([f"- [{a['grade']}] {a['title']}" for a in filtered])
    if not filtered_titles.strip():
        ai_summary = ""
    else:
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
                    "max_tokens": 400,
                    "messages": [{"role": "user", "content": f"아래 오늘의 리스크 기사 목록을 보고, 증권사 리스크 담당자를 위해 아래 형식으로 작성하세요.\n\n▸ 리스크 성격\n(오늘 전반적인 리스크 흐름을 30자 이내 한 문장)\n\n▸ 주요 포인트\n(담당자가 주목할 핵심 사항을 · 로 구분, 항목당 30자 이내, 최대 3개)\n\n반드시 짧고 핵심만. 문장 늘이지 말 것.\n\n{filtered_titles}"}],
                },
                timeout=15,
            )
            ai_summary = sum_res.json()["content"][0]["text"].strip()
        except Exception:
            ai_summary = ""

    html = build_email_html(filtered, total_count=total_count, ai_summary=ai_summary, exposure_data=exposure_data, ref_date=ref_date, competitor_notices=competitor_notices, today_str=today_str)
    send_email(subject, html)


if __name__ == "__main__":
    main()
