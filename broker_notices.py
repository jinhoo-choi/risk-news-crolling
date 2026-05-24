"""
네이버 뉴스 키워드 모니터링 & Claude AI 필터링 & 이메일 알림
GitHub Actions 전용 버전 / 네이버 검색 API 사용
"""

import requests
from html import escape as _esc
from bs4 import BeautifulSoup
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import csv
import time
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime as _pdt

# ─────────────────────────────────────────────
# 설정 — GitHub Secrets에서 자동으로 읽어옴
# ─────────────────────────────────────────────
EMAIL_SENDER      = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]
EMAIL_RECEIVERS   = [e.strip() for e in os.environ["EMAIL_RECEIVER"].split(",")]
NO_RESULT_RECEIVER = os.environ.get("NO_RESULT_RECEIVER", "").strip()  # 결과 없을 때 수신자
ANTHROPIC_KEY     = os.environ["ANTHROPIC_API_KEY"]
NAVER_CLIENT_ID   = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]

KEYWORDS = ["부실 리스크", "신용 리스크", "유동성 리스크", "디폴트 리스크", "기업회생", "상장폐지", "파산", "워크아웃", "부도", "거래정지", "반대매매 급증", "신용등급 강등", "PF 부실", "미매각", "신용융자", "발행어음", "서킷브레이커", "한국투자증권오류", "한국투자증권 장애", "한국투자증권 접속불가"]
MAX_NEWS_PER_KEYWORD = 1000  # 최근 6시간 기사 수집 (cutoff_kst 필터로 제한됨)
SEEN_FILE = "seen_news.json"
EXPOSURE_FILE = "exposure_data.csv"

# ─────────────────────────────────────────────


def load_exposure_data() -> dict:
    """CSV에서 eBiz본부 익스포저 데이터 로드 — 종목명 기준 딕셔너리 반환"""
    if not os.path.exists(EXPOSURE_FILE):
        return {}
    try:
        with open(EXPOSURE_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            result = {}
            for row in reader:
                name = row.get("종목명", "").strip()
                if name:
                    result[name] = row
            return result
    except Exception:
        return {}


def find_exposure(entity: str, exposure_data: dict) -> list:
    """entity와 종목명 딕셔너리 매칭 — 단어 경계 기반 정밀 매칭"""
    import re
    if not entity or not exposure_data:
        return []
    # 1) 정확히 일치하면 즉시 반환
    if entity in exposure_data:
        return [exposure_data[entity]]
    # 2) 단어 경계 기반 부분 매칭
    #    앞뒤가 한글/영숫자가 아닌 경우만 허용
    #    예: "화신" → "무궁화신탁" 불일치, "화신" → "화신정공" 일치
    results = []
    entity_pattern = re.compile(
        r'(?<![가-힣a-zA-Z0-9])' + re.escape(entity) + r'(?![가-힣a-zA-Z0-9])'
    )
    for name, row in exposure_data.items():
        name_pattern = re.compile(
            r'(?<![가-힣a-zA-Z0-9])' + re.escape(name) + r'(?![가-힣a-zA-Z0-9])'
        )
        if entity_pattern.search(name) or name_pattern.search(entity):
            results.append(row)
    return results


def load_competitor_notices() -> list:
    """경쟁사 공지사항 CSV에서 당일 신용·대출 관련 공지 로드"""
    CREDIT_KEYWORDS = [
        "신용한도", "신용융자", "신용공여", "신용거래",
        "증거금률", "증거금 변경", "반대매매 급증",
        "대출한도", "신용대출", "신용 중단", "한도 축소",
        "신용 재개", "신용거래 제한"
    ]
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    valid_dates = {(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(2)}
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
                    if date not in valid_dates:
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

    # 중복 제거 — (company, title) 기준
    seen_keys = set()
    deduped = []
    for item in result:
        key = (item["company"].strip(), item["title"].strip())
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(item)
    return deduped


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
    """최근 7시간 키(YYYY-MM-DD HH) 기준 seen URL 로드 — 오래된 키 자동 제거"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(7)
    }
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return set()
        if isinstance(data, list):
            return set()
        urls = set()
        for k in valid_keys:
            entry = data.get(k, {})
            if isinstance(entry, list):
                urls |= set(entry)
            elif isinstance(entry, dict):
                urls |= set(entry.get("urls", []))
        return urls
    return set()


def load_seen_combos() -> set:
    """최근 7시간 내 발송된 (entity, keyword) 조합 로드 — 실행 간 중복 사건 방지"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(7)
    }
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return set()
        if isinstance(data, list):
            return set()
        combos = set()
        for k in valid_keys:
            entry = data.get(k, {})
            if isinstance(entry, dict):
                for combo in entry.get("combos", []):
                    combos.add(tuple(combo))
        return combos
    return set()


def save_seen_urls(seen: set, combos: set = None):
    """현재 시각 키(YYYY-MM-DD HH)로 seen URL + 발송 조합 저장 — 최근 7시간 키만 보존"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    current_key = now.strftime("%Y-%m-%d %H")
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(7)
    }
    existing = {}
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                existing = {k: v for k, v in raw.items() if k in valid_keys}
            else:
                existing = {}
        except Exception:
            existing = {}
    # 현재 키 기존값 로드
    cur = existing.get(current_key, {})
    if isinstance(cur, list):
        cur = {"urls": cur, "combos": []}
    # URL merge
    existing_urls = set(cur.get("urls", []))
    existing_combos = [tuple(x) for x in cur.get("combos", [])]
    merged_urls = list(existing_urls | seen)
    # combo merge
    if combos:
        for combo in combos:
            if list(combo) not in existing_combos and combo not in [tuple(x) for x in existing_combos]:
                existing_combos.append(list(combo))
    existing[current_key] = {"urls": merged_urls, "combos": existing_combos}
    # atomic write — mkstemp으로 동시 실행 시 tmp 충돌 방지
    import tempfile as _tmpfile, os as _os
    fd, tmp_path = _tmpfile.mkstemp(prefix="seen_", suffix=".tmp",
                                    dir=_os.path.dirname(_os.path.abspath(SEEN_FILE)) or ".")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False)
        _os.replace(tmp_path, SEEN_FILE)
    except Exception:
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass
        raise


def crawl_naver_news(keyword: str) -> list:
    """네이버 검색 API로 뉴스 수집 — 최근 6시간 기사만"""
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    cutoff_kst = now_kst - timedelta(hours=6)
    today_kst = now_kst.date()

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
                pub_dt = now_kst
                pub_date = today_kst

            # 6시간 이전 기사 — 중단
            if pub_dt < cutoff_kst:
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
                    "body"   : "",
                })

        total = data.get("total", 0)
        start += 100
        if stop or len(items) < 100 or start >= 301:  # 마지막 페이지·300건 제한
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
    """50건씩 배치로 AI 필터링"""
    if not batch:
        return []

    numbered = "\n".join([
        f"{i+offset+1}. {a['title']}\n   요약: {a.get('desc','')}"
        for i, a in enumerate(batch)
    ])

    prompt = f"""당신은 한국투자증권 eBiz본부 리스크 담당자입니다.
eBiz본부는 비대면 주식거래(온라인 MTS·HTS)를 핵심 사업으로 하며, 리츠·펀드·발행어음·신용융자·IMA 등 금융상품을 온라인 채널로 판매합니다.
고객 자산 손실, 당사 익스포저 손실, 금융당국 제재, 비대면 거래 시스템 리스크에 특히 민감합니다.

[판단 원칙 — 가장 중요]
기사를 볼 때 아래 질문에 "YES"가 나와야만 relevant:true입니다.
"이 기사가 오늘 당장 한국투자증권 고객의 자산 손실 또는 당사 손실로 이어질 수 있는가?"
위 질문에 확신이 없으면 relevant:false입니다. 모호하면 무조건 제외하세요.

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
- 외국인·기관·개인 매수·매도·순매도·순매수 동향 기사 (수급 기사)
- 신용융자·빚투 잔고 증가·감소 동향 기사 (규모·통계 보도 수준, 실제 반대매매 확정 아닌 것)
- 반대매매 우려·경고·전망 기사 (실제 반대매매 급증·역대 최대 등 사실 확정 아닌 것)
- 코스피·코스닥 변동성·등락 기록 기사 (지수 자체의 변동성 보도)
- 증권사 실적·수수료·영업이익 비교 기사 (손실·부실 아닌 성과 비교)
- "이모저모", "브리핑", "소식" 등 단순 업계 동향 모음 기사
- 금융당국 경고·권고 수준 기사 (실제 제재·조사 착수 아닌 것)
- "~되나", "~우려", "~가능성", "~비명", "~뇌관", "~심화" 등 불확실한 전망·경고성 제목의 기사
- 고금리·환율·유가 등 거시경제 변수로 인한 기업 영향 분석 기사 (직접 손실 미확정)
- "양극화", "불안", "우려" 등 감성적 표현 위주로 특정 손실 사건 없는 기사
- 시리즈·기획 기사 ([미국발 고금리], [특집] 등 연속 기획물로 특정 리스크 사건 없는 것)
- 연예인·유명인·개인 대상 해킹·보이스피싱·금융사기 피해 기사 (증권사 시스템·인프라 무관)
- 증권사 IT 시스템과 직접 관련 없는 일반 사이버 범죄·해킹 사건
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
- 변호사·법조인 인터뷰·수상·선정·특집 기사 (Rising Stars, 베스트 변호사 등)
- 제목에 [특집], [기획], [인터뷰], [르포], [Rising Stars], [수상] 등이 포함된 기사
- 특정 사건의 소송·법률 자문 업무 소개 기사 (리스크 당사자가 아닌 법률 서비스 소개)

[직접 익스포저 판단 기준 — 핵심 필터]
아래 중 하나라도 해당해야 관련 있음으로 판단. 해당 없으면 무조건 제외:
- 증권사 채권 인수·PF 대출 참여·신용공여 직접 손실 가능
- 뱅키스(MTS·HTS) 고객이 현재 보유 중인 상장상품(주식·리츠·ETF·펀드) 손실 직접 발생
- 발행어음·IMA 운용자산 부실로 고객 손실 가능
- 금융당국이 한국투자증권 등 증권사를 직접 대상으로 조사·제재
- 비대면 거래 시스템(MTS·HTS) 장애·보안사고로 고객 피해 직접 발생

[리스크 순도 기준 — 아래 경우는 익스포저 없음으로 제외]
- 뉴스에 언급된 기업이 코스피·코스닥 상장사가 아닌 경우 (비상장 기업 부도·회생은 제외)
- 부도·회생 기업에 증권사 직접 여신·보증·판매 상품이 없는 경우
- 단순히 업황 악화·실적 부진 기사 (실제 손실 확정 아닌 것)
- 해당 기업 주가 하락만 있고 상폐·거래정지·부도가 아닌 경우
- "우려", "가능성", "전망", "위험성" 등 추측 표현만 있는 기사 (사실 확정 아닌 것)
- 지역 경제·산업 영향 분석 기사 (한국투자증권 직접 손실과 무관한 것)
- 타사(경쟁 증권사) 리스크 기사로 한국투자증권에 직접 영향 없는 것
- 채권·PF 관련 기사 중 한국투자증권이 인수·참여하지 않은 것으로 보이는 경우
- 기사에 구체적 금액·기업명·날짜 없이 일반론만 서술한 경우

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

[매우 중요 — 핵심 주제 판단]
기사의 핵심 주제·제목의 중심이 리스크가 아니면 relevant:false.
본문 일부에 리스크 단어가 있어도 기사 주제가 호재·실적·전망·성과이면 제외.
제목 주인공이 리스크 상황이 아닌데 본문에 타 기업 리스크가 언급된 경우도 제외.

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
            payload = res.json()
            content = payload.get("content", [])
            if not content:
                raise ValueError("Claude 응답 content 비어있음")
            raw = content[0].get("text", "").strip()
            if not raw:
                raise ValueError("Claude 응답 text 비어있음")
            raw = raw.replace("```json", "").replace("```", "").strip()
            start_idx = raw.find("[")
            end_idx = raw.rfind("]") + 1
            if start_idx == -1 or end_idx == 0:
                raise ValueError("JSON 배열을 찾을 수 없음")
            raw = raw[start_idx:end_idx]
            try:
                grades = json.loads(raw)
            except json.JSONDecodeError as je:
                # salvage 대신 명시적 에러 — retry 루프가 처리
                raise ValueError(f"JSON 파싱 실패: {je}") from je
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


def dedup_deterministic(articles: list) -> list:
    """3단계 중복 제거 — 제목 유사도 + 기업명·키워드 조합 + desc 유사도"""
    import unicodedata
    import re as _re
    from difflib import SequenceMatcher

    def normalize(text: str) -> str:
        t = unicodedata.normalize("NFKC", text)
        t = _re.sub(r"\[.*?\]|\(.*?\)", "", t)   # [속보] (연합) 등 제거
        t = _re.sub(r"[^가-힣a-zA-Z0-9]", "", t)      # 특수문자·공백 제거
        return t.strip()

    seen_norms   = []   # 정규화된 제목
    seen_combos  = {}   # (entity, keyword) 조합
    seen_descs   = []   # 정규화된 desc
    result = []

    for a in articles:
        title_norm = normalize(a.get("title", ""))
        desc_norm  = normalize(a.get("desc", ""))
        entity     = a.get("entity", "").strip()
        keyword    = a.get("keyword", "").strip()
        combo      = (entity, keyword) if entity else None

        matched = False

        # 1단계: 제목 유사도 (0.92 이상)
        for existing in seen_norms:
            if SequenceMatcher(None, title_norm, existing).ratio() >= 0.92:
                matched = True
                break

        # 2단계: 기업명 + 키워드 동일 조합 (같은 사건 다른 제목)
        if not matched and combo and combo in seen_combos:
            # combo 일치하면 desc로 추가 검증
            existing_desc = seen_combos[combo]
            if desc_norm and existing_desc:
                if SequenceMatcher(None, desc_norm, existing_desc).ratio() >= 0.70:
                    matched = True
            else:
                # desc 없으면 combo만으로 중복 판정
                matched = True

        # 3단계: desc 유사도 (0.80 이상) — 제목 달라도 내용 같은 기사
        if not matched and desc_norm and len(desc_norm) > 20:
            for existing_desc in seen_descs:
                if SequenceMatcher(None, desc_norm, existing_desc).ratio() >= 0.80:
                    matched = True
                    break

        if not matched:
            seen_norms.append(title_norm)
            seen_descs.append(desc_norm)
            if combo:
                seen_combos[combo] = desc_norm
            result.append(a)

    return result


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
        payload = res.json()
        content = payload.get("content", [])
        raw = content[0].get("text", "").strip() if content else ""
        if not raw:
            return articles
        raw = raw.replace("```json", "").replace("```", "").strip()
        start_idx = raw.find("[")
        end_idx = raw.rfind("]") + 1
        raw = raw[start_idx:end_idx]
        keep_ids = {item["id"] for item in json.loads(raw)}
        return [a for i, a in enumerate(articles) if (i + 1) in keep_ids]
    except Exception as e:
        print(f"중복 제거 오류: {e} — 중복 제거 생략")
        return articles


def regrade_urgent(articles: list) -> list:
    """긴급 3건 초과 시 중요도 판단 후 주의로 강등"""
    urgent = [a for a in articles if a.get("grade") == "긴급"]
    others = [a for a in articles if a.get("grade") != "긴급"]

    if len(urgent) <= 3:
        return articles

    print(f"  긴급 {len(urgent)}건 → 상위 3건 선별 중...")

    numbered = "\n".join([f"{i+1}. {a['title']}" for i, a in enumerate(urgent)])
    prompt = f"""아래 긴급 리스크 기사들의 중요도를 판단하여 상위 3건만 선별하세요.

중요도 우선순위:
1. 부도·파산·회생·상폐 확정 또는 신청
2. 증권사 직접 제재·손실 발생 확정
3. 기초자산(리츠·펀드) 부실·상폐 신청
4. 위 해당 없는 나머지 (주의로 강등)

반드시 JSON 배열만 반환하세요. 마크다운 코드블록 없이 순수 JSON만.
형식: [{{"id": 유지할id}}, ...] — 긴급 유지할 상위 3건 id만 포함

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
                "max_tokens": 200,
                "temperature": 0.0,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        res.raise_for_status()
        payload = res.json()
        content = payload.get("content", [])
        raw = content[0].get("text", "").strip() if content else ""
        if not raw:
            return articles
        raw = raw.replace("```json", "").replace("```", "").strip()
        start_idx = raw.find("[")
        end_idx = raw.rfind("]") + 1
        raw = raw[start_idx:end_idx]
        keep_ids = {item["id"] for item in json.loads(raw)}

        result = []
        for i, a in enumerate(urgent):
            if (i + 1) in keep_ids:
                result.append(a)
            else:
                a["grade"] = "주의"
                a["customer_notice"] = None  # 주의로 강등 시 고객 안내 문구 제거
                result.append(a)

        print(f"  긴급 유지 {len(keep_ids)}건 / 주의 강등 {len(urgent)-len(keep_ids)}건")
        return result + others

    except Exception as e:
        print(f"  긴급 강등 오류: {e} — 원본 유지")
        return articles


def ai_filter_and_grade(articles: list) -> list:
    """전체 기사를 50건씩 배치로 나눠 AI 필터링 후 중복 제거"""
    if not articles:
        return []
    result = []
    batch_size = 50
    for i in range(0, len(articles), batch_size):
        batch = articles[i:i+batch_size]
        print(f"  배치 {i//batch_size+1}/{-(-len(articles)//batch_size)} 처리 중... ({len(batch)}건)")
        result.extend(ai_filter_batch(batch, offset=i))
        # rate limit 없으면 딜레이 없이 진행
        if i + batch_size < len(articles):
            time.sleep(1)  # 최소 1초만 대기

    if len(result) > 1:
        print(f"  중복 제거 중... (필터링 후 {len(result)}건)")
        result = dedup_deterministic(result)
        print(f"  1차 dedup 후 {len(result)}건")
        if len(result) >= 10:
            result = dedup_by_title(result)
            print(f"  최종 중복 제거 후 {len(result)}건")
        else:
            print(f"  {len(result)}건 이하 — Claude dedup 스킵")

    # 긴급 3건 초과 시 중요도 판단 후 주의로 강등
    result = regrade_urgent(result)

    return result


def build_exposure_html(entity: str, exposure_data: list, ref_date: str) -> str:
    """익스포저 현황 HTML 생성 — 매칭 없으면 빈 문자열"""
    rows = find_exposure(entity, exposure_data)
    if not rows:
        return ""
    date_label = f" (기준일: {ref_date})" if ref_date else ""
    items_html = "".join([
        f'<div style="font-size:13px;color:#1e293b;margin-bottom:3px;"><span style="font-weight:bold;">{row.get("종목명","")}</span> ({row.get("종목유형","")}) : {float(str(row.get("잔고(억)","0")).replace(",","")):,.1f}억원 / {int(float(str(row.get("고객수","0")).replace(",",""))):,}명</div>'
        for row in rows
    ])
    return f'''<table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#fff8f8" style="background:#fff8f8;border-left:3px solid #c0392b;">
      <tr><td bgcolor="#fff8f8" style="padding:10px 16px;background:#fff8f8;">
        <p style="margin:0 0 4px 0;font-size:11px;font-weight:bold;color:#c0392b;letter-spacing:0.3px;">뱅키스 고객 보유현황{date_label}</p>
        {items_html}
      </td></tr>
    </table>'''


def build_email_html(articles: list, total_count: int = 0, ai_summary: str = '', exposure_data: dict = None, ref_date: str = '', competitor_notices: list = None, today_str: str = ''):
    exposure_data = exposure_data or {}
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
    GRADE_LIMIT = {"긴급": 999, "주의": 5, "참고": 999}  # 긴급 전건, 주의 5건, 참고 전건
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
            <td align="right" class="grade-header-right" style="padding:10px 14px;white-space:nowrap;">
              <span style="font-size:11px;color:#94a3b8;">{GRADE_DESC[grade]}</span>
            </td>
          </tr>
        </table>'''
        for a in display_items:
            if grade == "참고":
                rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-left:1px solid {gs["card_border"]};border-right:1px solid {gs["card_border"]};border-bottom:1px solid {gs["card_border"]};background:#fafafa;">
          <tr>
            <td style="padding:7px 16px;font-size:13px;word-break:keep-all;">
              <a href="{_esc(a['url'])}" style="color:#475569;text-decoration:none;line-height:1.5;">{_esc(a['title'])}</a>
            </td>
            <td align="right" valign="middle" style="padding:7px 16px 7px 4px;font-size:11px;color:#94a3b8;white-space:nowrap;">{a.get("pub_str","")}</td>
          </tr>
        </table>'''
            else:
                # AI 키워드 뱃지
                badges = ""
                if a.get("keyword"):
                    badges += f'<span style="display:inline-block;font-size:10px;color:#3b5491;background:#e8f0fe;padding:2px 7px;margin-right:4px;margin-bottom:6px;">{a["keyword"]}</span>'
                if a.get("entity") and a.get("entity") != a.get("keyword"):
                    badges += f'<span style="display:inline-block;font-size:10px;color:#64748b;background:#f1f5f9;padding:2px 7px;margin-right:4px;margin-bottom:6px;">{a["entity"]}</span>'

                if grade == "주의":
                    # 주의 압축 카드 — 제목 + 대응방안만
                    rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:{gs["card_bg"]};margin-bottom:6px;">
          <tr>
            <td style="padding:10px 18px;">
              {f"<p style='margin:0 0 4px 0;'>{badges}</p>" if badges else ""}
              <a href="{a['url']}" class="title-link caution-title" style="font-weight:bold;font-size:14px;text-decoration:none;color:#1e3a6e;line-height:1.6;word-break:keep-all;display:block;">{_esc(a['title'])}</a>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 6px 0;">
                <tr>
                  <td style="font-size:11px;"><a href="{a['url']}" style="color:#3b5491;text-decoration:none;">↗ 기사 보기</a></td>
                  <td align="right" style="font-size:11px;color:#94a3b8;">{a.get("pub_str","")}</td>
                </tr>
              </table>
              {f'<p style="margin:0 0 2px 0;font-size:11px;font-weight:bold;color:{gs["label_color"]};">대응방안</p><p style="margin:0;font-size:13px;color:#1e293b;line-height:1.5;">{a["action"]}</p>' if a.get("action") else ""}
              {build_exposure_html(a.get("entity",""), exposure_data or {}, ref_date)}
            </td>
          </tr>
        </table>'''
                else:
                    # 긴급 풀카드 — A안 통합 박스
                    exposure_html = build_exposure_html(a.get("entity",""), exposure_data or {}, ref_date)
                    action_row = f'<tr><td bgcolor="#fff8f8" style="padding:10px 18px;border-bottom:1px dashed {gs["card_border"]};background:#fff8f8;"><p style="margin:0 0 3px 0;font-size:11px;font-weight:bold;color:{gs["label_color"]};letter-spacing:0.3px;">대응방안</p><p style="margin:0;font-size:13px;color:#1e293b;line-height:1.6;font-weight:500;word-break:keep-all;">{a["action"]}</p></td></tr>' if a.get("action") else ""
                    exposure_row = f'<tr><td style="padding:10px 18px;border-bottom:1px dashed {gs["card_border"]};">{exposure_html}</td></tr>' if exposure_html else ""
                    notice_text = (a["customer_notice"][:200] + "...") if a.get("customer_notice") and len(a["customer_notice"]) > 200 else a.get("customer_notice","")
                    notice_row = f'<tr><td bgcolor="#eff6ff" style="padding:10px 16px;background:#eff6ff;border-top:1px solid #f5c6c6;"><p style="margin:0 0 5px 0;font-size:11px;font-weight:bold;letter-spacing:0.3px;"><span style="background:#2563eb;color:#fff;padding:2px 6px;font-size:10px;margin-right:5px;border-radius:3px;">✦ AI</span><span style="color:#1d4ed8;">고객케어 안내 추천 문구</span></p><p style="margin:0;font-size:12px;color:#1e3a6e;line-height:1.7;white-space:pre-line;word-break:keep-all;">{notice_text}</p></td></tr>' if a.get("customer_notice") else ""
                    bottom_box = f'<tr><td bgcolor="#fff8f8" style="background:#fff8f8;border-top:1px solid {gs["card_border"]};padding:0;"><table width="100%" cellpadding="0" cellspacing="0" border="0">{action_row}{exposure_row}{notice_row}</table></td></tr>' if (action_row or exposure_row or notice_row) else ""
                    rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:{gs["card_bg"]};margin-bottom:10px;">
          <tr>
            <td bgcolor="#fff8f8" style="padding:12px 16px;background:#fff8f8;border-bottom:1px solid #f5c6c6;">
              {f"<p style='margin:0 0 8px 0;'>{badges}</p>" if badges else ""}
              <a href="{a['url']}" class="title-link" style="font-weight:bold;font-size:15px;text-decoration:none;color:#1e3a6e;line-height:1.6;word-break:keep-all;display:block;">{_esc(a['title'])}</a>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:5px 0 8px 0;">
                <tr>
                  <td style="font-size:12px;"><a href="{a['url']}" style="color:#3b5491;text-decoration:none;">↗ 기사 보기</a></td>
                  <td align="right" style="font-size:11px;color:#94a3b8;">{a.get("pub_str","")}</td>
                </tr>
              </table>
              {f'<p style="margin:0;font-size:12px;color:#64748b;line-height:1.6;word-break:keep-all;">{_esc(a["desc"])}</p>' if a.get("desc") else ""}
            </td>
          </tr>
          {bottom_box}
        </table>'''
        if extra_items:
            extra_rows = "".join([f'''
            <tr>
              <td style="padding:4px 0;font-size:13px;color:#475569;border-bottom:1px solid #f0f0f0;">
                <a href="{e['url']}" style="color:#475569;text-decoration:none;">{e['title'][:60]}{"..." if len(e['title']) > 60 else ""}</a>
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
<style>
  @media only screen and (max-width: 600px) {{
    .outer {{ padding: 8px !important; }}
    .main {{ width: 100% !important; max-width: 100% !important; }}
    .header-td {{ padding: 16px 16px !important; }}
    .card-td {{ padding: 10px 14px !important; }}
    .summary-td {{ padding: 12px 14px !important; }}
    .rows-td {{ padding: 0 12px 12px 12px !important; }}
    .footer-td {{ padding: 12px 14px !important; }}
    .title-link {{ font-size: 15px !important; line-height: 1.5 !important; }}
    .caution-title {{ font-size: 13px !important; }}
    .desc-p {{ font-size: 12px !important; }}
    .action-p {{ font-size: 13px !important; }}
    .dash-num {{ font-size: 20px !important; }}
    .grade-header-right {{ display: none !important; }}
    .ref-date {{ display: none !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'맑은 고딕',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f6f9;">
<tr><td align="center" class="outer" style="padding:16px;">
<table width="640" cellpadding="0" cellspacing="0" border="0" class="main" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">

  <!-- 헤더 -->
  <tr>
    <td class="header-td" style="background:#3b5491;padding:22px 26px;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td>
            <p style="margin:0 0 4px 0;font-size:20px;font-weight:bold;color:#ffffff;">🤖 eBiz본부 리스크 탐지봇
              <span style="font-size:12px;color:#ffffff;padding:2px 8px;background:#5a7abf;margin-left:8px;">Powered by Claude AI</span>
            </p>
            <p style="margin:0 0 14px 0;font-size:13px;color:#c8d8f0;">
              {now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 · 수집 {total_count}건 → {len(articles)}건 선별 ({round((1 - len(articles)/total_count)*100) if total_count else 0}% 필터링)
            </p>
            <!-- 대시보드 -->
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#2d4278;">
              <tr>
                <td align="center" style="padding:12px 8px;border-right:1px solid #4a6099;">
                  <p class="dash-num" style="margin:0 0 2px 0;font-size:22px;font-weight:bold;color:#ff6b6b;">{len(sections['긴급'])}</p>
                  <p style="margin:0;font-size:12px;color:#d0dcf0;">긴급</p>
                </td>
                <td align="center" style="padding:12px 8px;border-right:1px solid #4a6099;">
                  <p class="dash-num" style="margin:0 0 2px 0;font-size:22px;font-weight:bold;color:#fbbf24;">{len(sections['주의'])}</p>
                  <p style="margin:0;font-size:12px;color:#d0dcf0;">주의</p>
                </td>
                <td align="center" style="padding:12px 8px;">
                  <p class="dash-num" style="margin:0 0 2px 0;font-size:22px;font-weight:bold;color:#6ee7b7;">{len(sections['참고'])}</p>
                  <p style="margin:0;font-size:12px;color:#d0dcf0;">참고</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- AI 분석 요약 -->
  {('<tr><td class="summary-td" style="padding:14px 22px;border-bottom:1px solid #e2e8f0;background:#f8fbff;">' + build_summary_html(ai_summary) + '</td></tr>') if ai_summary else ""}

  <!-- 경쟁사 특이사항 -->
  {('<tr><td>' + build_competitor_html(competitor_notices or [], today_str) + '</td></tr>') if competitor_notices else ""}

  <!-- 뉴스 카드 -->
  <tr><td class="rows-td" style="padding:0 22px 16px 22px;">{rows}</td></tr>

  <!-- 푸터 -->
  <tr>
    <td class="footer-td" style="padding:14px 22px;background:#fff;border-top:1px solid #e2e8f0;">
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
<tr><td align="center" class="outer" style="padding:16px;">
<table width="640" cellpadding="0" cellspacing="0" border="0" class="main" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">
  <tr>
    <td class="header-td" style="background:#3b5491;padding:22px 26px;">
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



def send_email_no_result(subject: str, html_body: str):
    """결과 없을 때 특정인(NO_RESULT_RECEIVER)에게만 발송"""
    receiver = NO_RESULT_RECEIVER if NO_RESULT_RECEIVER else EMAIL_SENDER
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = receiver
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, [receiver], msg.as_string())
        print(f"  결과없음 메일 발송 완료 → {receiver}")
    except smtplib.SMTPException as e:
        print(f"  결과없음 메일 발송 실패 (SMTP): {e}")
    except Exception as e:
        print(f"  결과없음 메일 발송 실패: {e}")

def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(EMAIL_RECEIVERS)
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                server.sendmail(EMAIL_SENDER, EMAIL_RECEIVERS, msg.as_string())
            print("이메일 발송 완료")
            return
        except smtplib.SMTPException as e:
            wait = 10 * (2 ** attempt)
            print(f"이메일 발송 실패 (SMTP, {attempt+1}/3): {e} — {wait}초 후 재시도")
            if attempt < 2:
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            print(f"이메일 발송 실패: {e}")
            raise


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 뉴스 모니터링 시작")
    now_kst         = datetime.now(timezone(timedelta(hours=9)))  # 전역 기준 시각
    seen_urls       = load_seen_urls()
    seen_combos     = load_seen_combos()  # 실행 간 중복 사건 방지
    new_seen_this_run = set()  # 이번 실행에서 신규 수집한 URL만 별도 관리
    new_combos_this_run = set()  # 이번 실행에서 발송된 (entity, keyword) 조합
    raw_articles    = []

    def crawl_keyword(keyword):
        articles = crawl_naver_news(keyword)
        kst_tz = timezone(timedelta(hours=9))
        result = []
        for article in articles:
            if article["url"]:
                try:
                    pub_dt = _pdt(article.get("pubDate","")).astimezone(kst_tz)
                    elapsed = now_kst - pub_dt
                    hours = int(elapsed.total_seconds() // 3600)
                    mins = int((elapsed.total_seconds() % 3600) // 60)
                    elapsed_str = f"{hours}시간 전" if hours > 0 else f"{mins}분 전"
                    article["pub_str"] = f"{pub_dt.strftime('%m/%d %H:%M')} ({elapsed_str})"
                except Exception:
                    article["pub_str"] = ""
                result.append(article)
        return keyword, result

    print(f"  키워드 {len(KEYWORDS)}개 병렬 크롤링 중...")
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(crawl_keyword, kw): kw for kw in KEYWORDS}
        for future in as_completed(futures):
            try:
                keyword, articles = future.result()
                new = []
                for article in articles:
                    if article["url"] not in seen_urls:
                        new.append(article)
                        seen_urls.add(article["url"])
                        new_seen_this_run.add(article["url"])
                raw_articles.extend(new)
                print(f"  [{keyword}] 신규 {len(new)}건")
            except Exception as e:
                print(f"  크롤링 오류: {e}")

    if not raw_articles:
        print("신규 뉴스 없음 — 결과 없음 메일 발송 (특정인만)")
        now = datetime.now(timezone(timedelta(hours=9)))
        now_str = now.strftime("%m월%d일 %H시")
        subject = f"[리스크 탐지] {now_str_full} 기준 — 신규 뉴스 없음"
        send_email_no_result(subject, build_empty_html(now))
        save_seen_urls(new_seen_this_run)
        return

    print(f"\nAI 필터링 중... (총 {len(raw_articles)}건)")
    filtered = ai_filter_and_grade(raw_articles)
    # 실행 간 중복 사건 필터 — 동일 entity+keyword 또는 keyword만으로도 중복 감지
    before_combo = len(filtered)
    filtered_final = []
    seen_keywords_this_run = set()  # 이번 실행 내 동일 keyword 중복 방지

    for a in filtered:
        entity  = a.get("entity", "").strip()
        keyword = a.get("keyword", "").strip()
        combo   = (entity, keyword) if entity and keyword else None
        kw_only = ("", keyword) if keyword else None

        # 7시간 내 이미 발송된 (entity+keyword) 조합
        if combo and combo in seen_combos:
            print(f"  [{a['grade']}] '{a['title'][:30]}' — 동일 사건(entity+kw) 이미 발송, 스킵")
            continue

        # 7시간 내 이미 발송된 keyword만 조합 (entity 없는 경우)
        if not entity and kw_only and kw_only in seen_combos:
            print(f"  [{a['grade']}] '{a['title'][:30]}' — 동일 키워드 이미 발송, 스킵")
            continue

        # 이번 실행 내 동일 keyword 중복 (반대매매·빚투 등 entity 없이 몰리는 경우)
        if keyword and keyword in seen_keywords_this_run:
            print(f"  [{a['grade']}] '{a['title'][:30]}' — 이번 실행 내 동일 키워드 중복, 스킵")
            continue

        filtered_final.append(a)
        if keyword:
            seen_keywords_this_run.add(keyword)

    filtered = filtered_final
    if before_combo != len(filtered):
        print(f"  중복 사건 제거: {before_combo}건 → {len(filtered)}건")
    print(f"필터링 후 {len(filtered)}건 선별")

    if not filtered:
        print("AI 필터링 결과 없음 — 결과 없음 메일 발송 (특정인만)")
        now = datetime.now(timezone(timedelta(hours=9)))
        now_str = now.strftime("%m월%d일 %H시")
        subject = f"[리스크 탐지] {now_str_full} 기준 — 해당 뉴스 없음"
        send_email_no_result(subject, build_empty_html(now))
        save_seen_urls(new_seen_this_run)
        return

    exposure_data = load_exposure_data()  # regenerate_action에서 참조하므로 먼저 로드
    print("  본문 크롤링 중... (긴급·주의만)")
    def crawl_body(article):
        # 참고 등급은 본문 불필요 — 속도 개선
        if article.get("grade") == "참고":
            article["body"] = ""
            return article
        body = fetch_article_body(article["url"])
        # 본문 크롤링 실패(WAF 차단 등) 시 desc로 fallback
        article["body"] = body if body else article.get("desc", "")
        return article

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(crawl_body, a): a for a in filtered}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass
    # 대응방안 재생성 병렬처리 (참고 제외)
    print("  대응방안 재생성 중...")

    # 긴급: action + LMS 통합 생성 (API 2회→1회, 비용 절감)
    # 주의: 1차 AI action 유지
    print("  대응방안·고객안내 생성 중... (긴급만)")

    def generate_action_and_notice(article):
        if article.get("grade") != "긴급":
            return
        body_text = article.get("body", "")
        entity    = article.get("entity", "")
        keyword   = article.get("keyword", "")
        exp_rows  = find_exposure(entity, exposure_data)
        def _fmt_exp(r):
            잔고 = float(str(r.get('잔고(억)', '0')).replace(',', ''))
            고객 = int(float(str(r.get('고객수', '0')).replace(',', '')))
            return f"{r.get('종목유형','')} {잔고:,.1f}억원/{고객:,}명"
        exp_str = ", ".join([_fmt_exp(r) for r in exp_rows]) if exp_rows else ""
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
                    "temperature": 0.0,
                    "messages": [{"role": "user", "content": f"""한국투자증권 eBiz본부 리스크 담당자입니다.
아래 기사를 바탕으로 두 가지를 JSON으로 반환하세요.

1. action: 즉시 취해야 할 실무 조치 (50자 이내, 보고·공유·전달 제외, 실제 행동만)
   - [확인 대상] + [즉시 조치] + [기한] 포함
   - 유형별: 회생·파산→담보현황파악, 금감원→컴플라이언스점검, PF→미매각잔액파악, 신용등급강등→평가손산출, 반대매매→담보부족계좌파악, 리츠→기초자산확인

2. customer_notice: 고객 안내 문구 (5줄 이내)

   [작성 원칙 — 고객 중심]
   - 고객 입장에서 "나에게 왜 중요한가", "지금 어떻게 해야 하나"가 즉시 이해되도록 작성
   - 회사 중심(당사, 저희) 표현 대신 고객 중심(고객님의, 고객님께서 보유하신) 표현 사용
   - 불안 조성 없이 상황을 명확하게 전달하고 행동 방향 제시
   - 5줄 이내, 짧고 명확하게

   [쉬운 용어 치환 — 반드시 적용]
   - 담보유지율·담보 유지율 → 담보비율
   - 반대매매 → 강제 매도
   - 증거금 → 보증금
   - 신용융자 → 신용 대출
   - 만기 도래 → 만기가 다가옴
   - 기초자산 → 투자 기반 자산
   - 익스포저 → 투자 규모
   - 채무불이행 → 빚을 갚지 못함
   - 기업회생 → 법원의 회생 절차
   - 유동성 위기 → 현금 부족 위기
   - 상장폐지 → 상장 폐지(거래 불가)

   [유형별 구조]
   - 회생·파산·상폐: [한국투자증권] 중요 안내
     → 상황 1줄 + "고객님의 투자에 영향이 있을 수 있습니다" + 확인 권유 + 문의처
   - ETF·펀드 상폐: [한국투자증권] 보유상품 안내
     → 상황 1줄 + 고객님이 취해야 할 행동 1줄 + 문의처
   - 신용융자·반대매매: [한국투자증권] 담보비율 안내
     → 현재 상황 1줄 + 담보비율 확인 및 대응 권유 1줄 + 문의처
   - PF·채권·제재: [한국투자증권] 시장 현황 안내
     → 상황 요약 1줄 + 보유 상품 점검 권유 1줄 + 문의처
   끝에 반드시 "문의: 고객센터 1544-5000" 포함

반드시 JSON만 반환. 마크다운 없이.
{{"action":"...", "customer_notice":"..."}}

기사 정보:
- 키워드: {keyword}
- 기업명: {entity}
- 등급: {article['grade']}
- 제목: {article['title']}
- 본문: {body_text[:400]}
{f"- eBiz 익스포저: {exp_str}" if exp_str else ""}"""}],
                },
                timeout=20,
            )
            payload = res.json()
            content = payload.get("content", [])
            raw = content[0].get("text", "").strip() if content else ""
            if not raw:
                return
            raw = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            if result.get("action"):
                article["action"] = result["action"]
            if result.get("customer_notice"):
                article["customer_notice"] = result["customer_notice"]
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(generate_action_and_notice, a) for a in filtered]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    now = datetime.now(timezone(timedelta(hours=9)))
    today_str = now.strftime("%m월 %d일")
    now_str_full = now.strftime("%m월 %d일 %H시 %M분")
    # exposure_data는 본문 크롤링 전에 이미 로드됨
    competitor_notices = load_competitor_notices()
    if competitor_notices:
        print(f"  경쟁사 신용·대출 특이사항 {len(competitor_notices)}건 발견")
    else:
        print("  경쟁사 신용·대출 특이사항 없음")
    if exposure_data:
        ref_date = next(iter(exposure_data.values())).get("기준일", "")
        print(f"  익스포저 데이터 로드 완료 ({len(exposure_data)}건, 기준일: {ref_date})")
    else:
        ref_date = ""
        print("  익스포저 데이터 없음 — CSV 파일 미확인")

    now_str = now.strftime("%m월%d일 %H시")
    urgent_count = len([a for a in filtered if a.get("grade") == "긴급"])
    subject = f"[리스크 탐지] {now_str_full} 기준"
    total_count = len(raw_articles)

    # AI 전체 요약 생성
    grade_summary = []
    if len([a for a in filtered if a["grade"]=="긴급"]) > 0:
        grade_summary.append(f"긴급 {len([a for a in filtered if a['grade']=='긴급'])}건")
    if len([a for a in filtered if a["grade"]=="주의"]) > 0:
        grade_summary.append(f"주의 {len([a for a in filtered if a['grade']=='주의'])}건")
    # AI에게 오늘 리스크 성격 요약 요청
    urgent_cnt = len([a for a in filtered if a["grade"]=="긴급"])
    caution_cnt = len([a for a in filtered if a["grade"]=="주의"])
    ref_cnt = len([a for a in filtered if a["grade"]=="참고"])
    filtered_titles = f"[등급 분포] 긴급 {urgent_cnt}건 / 주의 {caution_cnt}건 / 참고 {ref_cnt}건\n\n" + "\n".join([f"- [{a['grade']}] {a['title']}" for a in filtered])
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

    # 발송된 기사의 (entity, keyword) 조합 저장 — 실행 간 중복 사건 방지
    for a in filtered:
        entity  = a.get("entity", "").strip()
        keyword = a.get("keyword", "").strip()
        if keyword:
            new_combos_this_run.add((entity, keyword))  # entity 없어도 keyword만으로 저장
    save_seen_urls(new_seen_this_run, new_combos_this_run)


if __name__ == "__main__":
    main()
