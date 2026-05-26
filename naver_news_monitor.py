"""
네이버 뉴스 키워드 모니터링 & Claude AI 필터링 & 이메일 알림
GitHub Actions 전용 버전 / 네이버 검색 API 사용
"""

import requests
import re
import random
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
MAX_NEWS_PER_KEYWORD = 300   # 네이버 API 페이지 제한 (100건×3페이지)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]
SEEN_FILE = "seen_news.json"
EXPOSURE_FILE = "exposure_data.csv"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

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
        for i in range(24)
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
    """최근 7시간 내 발송된 (entity, keyword) 조합 로드"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(24)
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


def load_seen_context() -> dict:
    """최근 7시간 내 발송된 기사의 title_norms·desc_norms 로드 — 맥락 기반 중복 감지"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(24)
    }
    title_norms = []
    desc_norms  = []
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {"title_norms": [], "desc_norms": []}
        if isinstance(data, list):
            return {"title_norms": [], "desc_norms": []}
        for k in valid_keys:
            entry = data.get(k, {})
            if isinstance(entry, dict):
                title_norms.extend(entry.get("title_norms", []))
                desc_norms.extend(entry.get("desc_norms",  []))
    return {"title_norms": title_norms, "desc_norms": desc_norms}


def save_seen_urls(seen: set, combos: set = None, title_norms: list = None, desc_norms: list = None):
    """현재 시각 키(YYYY-MM-DD HH)로 seen URL + 발송 조합 저장 — 최근 7시간 키만 보존"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    current_key = now.strftime("%Y-%m-%d %H")
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(24)
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
        cur = {"urls": cur, "combos": [], "title_norms": [], "desc_norms": []}
    # URL merge
    existing_urls   = set(cur.get("urls", []))
    existing_combos = [tuple(x) for x in cur.get("combos", [])]
    existing_titles = cur.get("title_norms", [])
    existing_descs  = cur.get("desc_norms", [])
    merged_urls = list(existing_urls | seen)
    # combo merge
    if combos:
        for combo in combos:
            if combo not in [tuple(x) for x in existing_combos]:
                existing_combos.append(list(combo))
    # title_norms·desc_norms merge (최근 50건만 유지 — 메모리 절약)
    if title_norms:
        existing_titles = (existing_titles + title_norms)[-50:]
    if desc_norms:
        existing_descs  = (existing_descs  + desc_norms)[-50:]
    existing[current_key] = {
        "urls":        merged_urls,
        "combos":      existing_combos,
        "title_norms": existing_titles,
        "desc_norms":  existing_descs,
    }
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
    """기사 본문 크롤링 — Session + 헤더 강화로 WAF 대응"""
    try:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(max_retries=2)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        res = session.get(url, timeout=12, headers={
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://search.naver.com/",
            "Connection": "keep-alive",
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


# ─────────────────────────────────────────────
# 하드 제외 패턴 — AI 호출 전 사전 필터
# ─────────────────────────────────────────────
# 제목에서만 체크 — 오탐 위험 낮은 강력 패턴
TITLE_ONLY_PATTERNS = [
    "시황", "마감", "장마감", "마켓",
    "목표가", "목표주가", "투자의견", "매수", "매도", "중립",
    "브리핑", "뉴스브리핑", "이모저모",
    "특징주", "투자전략", "포트폴리오",
    "신고가", "급등", "상한가", "흑자전환", "실적개선", "호실적",
    "목표달성", "수주", "계약체결", "MOU", "협약",
    "순매수", "순매도", "외국인매수", "외국인매도", "거래대금",
    "부고", "인사", "승진", "선임", "취임", "퇴임",
    "경고음", "경고등", "빨간불", "황신호", "신호탄", "뇌관",
    "(完)", "(완)", "①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
    "현직이 푸는", "전문가가 보는", "기자가 간다",
    "후보", "공약", "선거", "시의원", "구의원", "도의원", "국회의원", "시장 출마", "당선",
    "복합문화", "재개발", "부지 활용", "도시재생", "리모델링",
]

# 제목+desc 모두 체크 — 비교적 안전한 패턴만
TEXT_PATTERNS = [
    "전망", "분석", "리포트", "보고서", "추천",
    "인터뷰", "기획", "특집", "르포", "칼럼", "오피니언", "사설", "논설",
    "소식",
]

# desc에서만 체크하면 오탐 위험 — 기자수첩 등은 제목에만 적용
TITLE_ONLY_PATTERNS += [
    "기자수첩", "기자의 눈", "기자노트", "취재후기", "현장에서", "데스크에서",
]

EXCLUDE_TITLE_RE_PATTERNS = [
    r"\[단독\].*인터뷰",
    r"\[기획\]",
    r"\[특집\]",
    r"①|②|③|④|⑤",
    r"^\d+위\s",
]

def is_hard_excluded(title: str, desc: str = "") -> tuple:
    """하드 제외 패턴 매칭 — (excluded: bool, reason: str) 반환
    TITLE_ONLY_PATTERNS: 제목만 검사 (오탐 방지)
    TEXT_PATTERNS: 제목+desc 검사 (안전한 패턴만)
    """
    import re as _re
    # 제목 전용 패턴
    for pat in TITLE_ONLY_PATTERNS:
        if pat in title:
            return True, pat
    # 제목+desc 패턴 (보수적)
    text = title + " " + (desc or "")
    for pat in TEXT_PATTERNS:
        if pat in text:
            return True, pat
    # 정규식 패턴 (제목만)
    for pat in EXCLUDE_TITLE_RE_PATTERNS:
        if _re.search(pat, title):
            return True, pat
    return False, None


def ai_filter_batch(batch: list, offset: int = 0) -> list:
    """50건씩 배치로 AI 필터링"""
    if not batch:
        return []

    numbered = "\n".join([
        f"{i+offset+1}. {a['title']}\n   요약: {a.get('desc','')}"
        for i, a in enumerate(batch)
    ])

    prompt = f"""당신은 한국투자증권 eBiz본부 리스크 담당자입니다.
뱅키스(MTS·HTS) 고객의 자산 손실 또는 당사 직접 손실로 이어지는 기사만 선별합니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━
[포함 기준 — 아래 6가지 중 하나에 해당해야만 relevant:true]
━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 코스피·코스닥 상장사의 상장폐지·거래정지·부도·파산·기업회생 신청 또는 확정
2. 금융당국(금감원·금융위)이 한국투자증권 또는 증권업계를 직접 대상으로 조사 착수·제재·과태료 부과 확정
3. 한국투자증권 MTS·HTS 시스템 직접 장애·보안사고·오류 발생
4. 증권사가 직접 참여한 PF·채권·발행어음의 부실·만기 미상환·미매각 확정
5. 반대매매 실제 급증·역대 최대 등 수치 확정 또는 신용융자 한도 전면 중단 시행
6. 한국투자증권이 직접 언급된 기사로 고객 피해·법적 제재·금전 손실 발생

위 6가지 중 하나에도 해당하지 않으면 무조건 relevant:false.
모호하거나 확신이 없으면 relevant:false.

━━━━━━━━━━━━━━━━━━━━━━━━━━━
[판단 예시 — 반드시 참고]
━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ relevant:true  | 긴급 | "금양, 상장폐지 효력정지 가처분 신청" → 기준1 (상폐 절차 진행 중)
✅ relevant:true  | 긴급 | "한국투자증권 전산사고 과태료 1억 제재" → 기준2+3 (당사 직접 제재)
✅ relevant:true  | 긴급 | "제이알글로벌리츠 400억 채무불이행, 법원에 회생 신청" → 기준1 (상장 리츠 회생 신청)
✅ relevant:true  | 주의 | "금감원, 증권사 PF 브릿지론 현장검사 착수" → 기준2 (증권업계 직접 조사 착수)
✅ relevant:true  | 긴급 | "빚투 우려 현실로…반대매매 역대 최대, 하루 3000억 강제청산" → 기준5 (실제 수치 확정)
✅ relevant:true  | 긴급 | "한국투자증권 MTS 접속 장애, 매매 1시간 중단" → 기준3 (당사 시스템 장애)

❌ relevant:false | "[미국발 고금리] 불안한 빚투…코스피 뇌관 되나" → 전망·경고성, 확정 사건 없음
❌ relevant:false | "외국인 44조 순매도, 개인이 받아냈다" → 수급 동향 기사
❌ relevant:false | "빚투 잔고 26조 돌파, 삼성전자 쏠림" → 통계 보도, 반대매매 확정 아님
❌ relevant:false | "고금리·환율·유가 3高에 기업들 비명" → 거시경제 분석, 직접 손실 없음
❌ relevant:false | "증권사 실적 양극화 심화" → 성과 비교 기사
❌ relevant:false | "우리은행 인도네시아 충당금 1380억" → 타 금융업권, 증권사 익스포저 없음
❌ relevant:false | "다원시스 협력사 줄도산 위기" → 비상장 협력사, 증권사 익스포저 없음
❌ relevant:false | "태영건설 PF 천안 20년 악몽" → 이미 알려진 사건 후속 분석, 새 리스크 없음
❌ relevant:false | "[한투증권 실적과 질문들]④ 신용융자로 흡수한 빚투 호황" → 시리즈 기획, 손실 아닌 실적
❌ relevant:false | "BTS 정국, 대기업 임원 해킹 피해" → 증권사 시스템 무관
❌ relevant:false | "[기자수첩] '포모'가 부추긴 빚투 경고음" → 칼럼·기자 의견, 실제 반대매매 확정 없음
❌ relevant:false | "신용융자 36조 역대 최대…전문가 경고" → 통계 보도, 반대매매 확정 아님
❌ relevant:false | "코스피 8천 돌파 후 빚투 경고등…신용융자 36조 사상 최대" → 잔고 통계 보도, 실제 반대매매 발생 아님
❌ relevant:false | "도 의원 후보, 폐점한 홈플러스 복합문화플랫폼으로" → 선거 공약 기사, 증권사 익스포저와 무관
❌ relevant:false | "HL D&I 주가 신바람…건설주 정책 기대감에 매수" → 기사 주인공이 HL D&I, 태양건설은 본문 언급만, 직접 리스크 없음
❌ relevant:false | "현직이 푸는 사모펀드 환매중단 사태 3(完)" → 연재 칼럼, 직접 손실 사건 아님
❌ relevant:false | "세제 40% 공제 내세운 국민성장펀드…광풍 뒤 숨은 리스크" → 리스크 우려·분석, 직접 손실 미확정
❌ relevant:false | "[롯데건설 PF 점검] 홈플러스 후순위 1조 시한폭탄" → 시리즈 기획, 이미 알려진 사건 반복 분석

━━━━━━━━━━━━━━━━━━━━━━━━━━━
[등급 기준] — relevant:true인 경우만 적용
━━━━━━━━━━━━━━━━━━━━━━━━━━━
긴급 (즉각 대응 필요):
  - 상장폐지·부도·파산·회생 신청 또는 확정
  - 금융당국 제재·과태료 확정
  - 당사 MTS·HTS 시스템 장애 발생
  - 반대매매 역대 최대 등 실제 수치 확정
  - 100억 이상 채무 미상환·디폴트 선언

주의 (모니터링 필요) — 아래 조건 모두 충족해야 함:
  - 구체적 기업명 + 구체적 금액이 기사에 명시된 경우만
  - 회생·부도·상폐 가능성 처음 언급 (신청 전 단계)
  - 금융당국 조사·검사 예고·착수 (구체적 대상 명시)
  - 신용등급 강등 경고(Negative Watch) 신규 발생
  - PF·채권 부실 징후 첫 보도 (기존 알려진 사건 반복 아닌 것)
  ※ 이미 알려진 사건의 반복 보도·심층 분석·칼럼은 주의에서도 제외

참고 (업황 파악용):
  - 직접 손실 없으나 모니터링 필요한 동향


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
- confidence: relevant 판단 확신도 0.0~1.0 (1.0=완전확신, 0.5=애매함). relevant=false도 반드시 포함.
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
- event_type: 사건 유형을 아래 중 1개로 분류. (relevant=false면 null)
  상장폐지 / 거래정지 / 기업회생 / 파산부도 / PF부실 / 신용등급강등 / 반대매매 / 금감원제재 / 시스템장애 / 발행어음부실 / 기타리스크
반환 형식 예시 (긴급/주의/참고/제외 각 1건):
[
  {{"id":1,"relevant":true,"grade":"긴급","reason":"리츠 기초자산 회생신청·손실 확정","confidence":0.97,"action":"해당 리츠 보유 고객 전수 파악 및 금일 내 평가손 산출","entity":"제이알글로벌리츠","event_type":"기업회생"}},
  {{"id":2,"relevant":true,"grade":"주의","reason":"PF 부실 징후·손실 미확정 단계","confidence":0.82,"action":"주 1회 PF 잔액 추이 점검, 연체 발생 시 즉시 대응","entity":"태영건설","event_type":"PF부실"}},
  {{"id":3,"relevant":true,"grade":"참고","reason":"업계 발행어음 증가 동향","confidence":0.71,"action":"동종업계 발행어음 만기 구조 비교, 자사 유동성 비율 점검","entity":"미래에셋증권","event_type":"발행어음부실"}},
  {{"id":4,"relevant":false,"grade":null,"reason":null,"confidence":0.12,"action":null,"entity":null,"event_type":null}}
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
                    "model": CLAUDE_MODEL,
                    "max_tokens": 4000,
                    "temperature": 0.0,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
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
            end_idx   = raw.rfind("]") + 1
            if start_idx == -1 or end_idx <= 0:
                raise ValueError("JSON 배열을 찾을 수 없음")
            raw = raw[start_idx:end_idx]
            try:
                try:
                    grades = json.loads(raw)
                except json.JSONDecodeError:
                    # trailing comma, 설명문 등 Claude 응답 불완전 시 repair
                    from json_repair import repair_json
                    repaired = repair_json(raw)
                    grades = json.loads(repaired)
                    print(f"  JSON repair 적용됨 (배치 {offset//50+1})")
            except json.JSONDecodeError as je:
                # salvage 대신 명시적 에러 — retry 루프가 처리
                raise ValueError(f"JSON 파싱 실패: {je}") from je
            grade_map = {g["id"]: g for g in grades}
            result = []
            for i, article in enumerate(batch):
                info = grade_map.get(i + offset + 1, {})
                article["_ai_confidence"] = info.get("confidence", None)
                if info.get("relevant") and info.get("grade"):
                    article["grade"]      = info["grade"]
                    article["reason"]     = info.get("reason", "")
                    article["action"]     = info.get("action", "")
                    article["entity"]     = info.get("entity", "")
                    article["event_type"] = info.get("event_type", "")
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
    """3단계 중복 제거 — 제목 유사도 + 기업명·키워드 조합 + desc 유사도
    rapidfuzz 사용 (없으면 SequenceMatcher fallback)
    """
    import unicodedata
    import re as _re
    try:
        from rapidfuzz import fuzz as _fuzz
        def _ratio(a, b): return _fuzz.ratio(a, b) / 100.0
    except ImportError:
        from difflib import SequenceMatcher
        def _ratio(a, b): return SequenceMatcher(None, a, b).ratio()

    def normalize(text: str) -> str:
        t = unicodedata.normalize("NFKC", text)
        t = _re.sub(r"\[.*?\]|\(.*?\)", "", t)   # [속보] (연합) 등 제거
        t = _re.sub(r"속보|단독|긴급|종합", "", t)    # 언론사 접두어 제거
        t = _re.sub(r"[^가-힣a-zA-Z0-9]", "", t)      # 특수문자·공백 제거
        return t.strip()

    seen_norms    = []
    seen_entities = []   # entity 조건 결합용
    seen_combos   = {}
    seen_descs    = []
    result = []

    # 사건 진행 단계 키워드 — dedup 예외 처리용
    _NEXT_STAGE = {
        "가처분","효력정지","집행정지","이의신청","항고","판결",
        "보류","재개","재상장","거래재개","상장유지",
        "파산선고","청산","폐업","회생인가","회생계획",
        "배당","변제","채무조정","추가제재","과징금","검찰고발",
    }
    def _is_next_stage_det(title: str) -> bool:
        return any(kw in title for kw in _NEXT_STAGE)

    for a in articles:
        title_norm = normalize(a.get("title", ""))
        desc_norm  = normalize(a.get("desc", ""))
        entity     = a.get("entity", "").strip()
        keyword    = a.get("keyword", "").strip()
        combo      = (entity, keyword) if entity else None

        # 사건 진행 단계 기사 — 유사도 검사 스킵
        if _is_next_stage_det(a.get("title", "")):
            seen_norms.append(title_norm)
            seen_entities.append(entity)
            seen_descs.append(desc_norm)
            if combo:
                seen_combos[combo] = desc_norm
            result.append(a)
            continue

        matched = False

        # 1단계: 제목 유사도 (0.95 이상) + 동일 entity 조건 결합
        #         entity 다른 기사는 유사도 높아도 제거하지 않음
        for existing_norm, existing_entity in zip(seen_norms, seen_entities):
            if _ratio(title_norm, existing_norm) >= 0.95:
                if not entity or not existing_entity or entity == existing_entity:
                    matched = True
                    break

        # 2단계: 기업명 + 키워드 동일 조합
        if not matched and combo and combo in seen_combos:
            existing_desc = seen_combos[combo]
            if desc_norm and existing_desc:
                if _ratio(desc_norm, existing_desc) >= 0.70:
                    matched = True
            else:
                matched = True

        # 3단계: desc 유사도 (0.80 이상)
        if not matched and desc_norm and len(desc_norm) > 20:
            for existing_desc in seen_descs:
                if _ratio(desc_norm, existing_desc) >= 0.80:
                    matched = True
                    break

        if not matched:
            seen_norms.append(title_norm)
            seen_entities.append(entity)
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
                "model": CLAUDE_MODEL,
                "max_tokens": 1000,
                "temperature": 0.0,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
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


# 등급별 최대 노출 건수
GRADE_LIMITS = {"긴급": 2, "주의": 3, "참고": 5}

# 리스크 점수 계산 — 키워드 우선순위 가중치
RISK_PRIORITY = {
    "한국투자증권": 2.0,               # 당사 직접 언급 최우선
    "MTS": 1.8, "HTS": 1.8,           # 당사 시스템 장애
    "전산장애": 1.8, "전산사고": 1.8,
    "상장폐지": 1.5, "파산": 1.5,     # 확정 사건
    "부도": 1.5, "거래정지": 1.5,
    "반대매매": 1.4, "강제청산": 1.4, # 실제 발생
    "기업회생": 1.3, "워크아웃": 1.2, # 절차 진행
}

def calc_risk_score(article: dict) -> float:
    """리스크 점수 = confidence × 키워드 가중치 + 익스포저 보정"""
    conf  = article.get("_ai_confidence") or 0.3  # 미반환 시 보수적 기본값
    title = article.get("title", "") + article.get("reason", "")
    # 가장 높은 키워드 가중치 적용
    kw_weight = max(
        [v for k, v in RISK_PRIORITY.items() if k in title],
        default=1.0
    )
    # 익스포저 있으면 +0.1 보정
    exp_boost = 0.1 if article.get("_has_exposure") else 0
    return round(conf * kw_weight + exp_boost, 4)

def regrade_by_score(articles: list) -> list:
    """등급별 상한 초과 시 리스크 점수 기반으로 하위 등급 강등
    긴급 → 최대 2건 (초과분 주의로 강등)
    주의 → 최대 3건 (초과분 참고로 강등)
    참고 → 최대 5건 (초과분 제거)
    점수 = confidence × 키워드 가중치 + 익스포저 보정
    """
    # 등급별 분리 + 리스크 점수 내림차순 정렬
    for a in articles:
        a["_risk_score"] = calc_risk_score(a)

    urgent  = sorted([a for a in articles if a.get("grade") == "긴급"],
                     key=lambda x: x["_risk_score"], reverse=True)
    caution = sorted([a for a in articles if a.get("grade") == "주의"],
                     key=lambda x: x["_risk_score"], reverse=True)
    ref     = sorted([a for a in articles if a.get("grade") == "참고"],
                     key=lambda x: x["_risk_score"], reverse=True)

    result = []

    # 긴급 — 상위 2건 유지, 나머지 주의로 강등
    for i, a in enumerate(urgent):
        if i < GRADE_LIMITS["긴급"]:
            result.append(a)
        else:
            a["grade"] = "주의"
            a["customer_notice"] = None
            caution.append(a)
            print(f"  [강등] 긴급→주의: {a['title'][:35]}")

    # 주의 — 상위 3건 유지, 나머지 참고로 강등
    caution_sorted = sorted(caution, key=lambda x: x.get("_risk_score") or 0, reverse=True)
    for i, a in enumerate(caution_sorted):
        if i < GRADE_LIMITS["주의"]:
            result.append(a)
        else:
            a["grade"] = "참고"
            ref.append(a)
            print(f"  [강등] 주의→참고: {a['title'][:35]}")

    # 참고 — 상위 5건만 유지
    ref_sorted = sorted(ref, key=lambda x: x.get("_risk_score") or 0, reverse=True)
    for i, a in enumerate(ref_sorted):
        if i < GRADE_LIMITS["참고"]:
            result.append(a)
        else:
            print(f"  [제외] 참고 초과: {a['title'][:35]}")

    urgent_cnt  = sum(1 for a in result if a.get("grade") == "긴급")
    caution_cnt = sum(1 for a in result if a.get("grade") == "주의")
    ref_cnt     = sum(1 for a in result if a.get("grade") == "참고")
    print(f"  등급 조정 완료 → 긴급 {urgent_cnt}건 / 주의 {caution_cnt}건 / 참고 {ref_cnt}건")

    return result


def ai_filter_and_grade(articles: list) -> list:
    """전체 기사를 50건씩 배치로 나눠 AI 필터링 후 중복 제거"""
    if not articles:
        return []
    result = []
    batch_size = 50
    ai_fail_count = 0
    MAX_AI_FAILS = 3  # circuit breaker — 연속 3회 실패 시 중단
    for i in range(0, len(articles), batch_size):
        if ai_fail_count >= MAX_AI_FAILS:
            print(f"  ⚠️ AI 연속 {MAX_AI_FAILS}회 실패 — circuit breaker 작동, 필터링 중단")
            break
        batch = articles[i:i+batch_size]
        print(f"  배치 {i//batch_size+1}/{-(-len(articles)//batch_size)} 처리 중... ({len(batch)}건)")
        batch_result = ai_filter_batch(batch, offset=i)
        if not batch_result and batch:  # 배치 결과 없으면 실패 카운트
            ai_fail_count += 1
            print(f"  배치 실패 ({ai_fail_count}/{MAX_AI_FAILS})")
        else:
            ai_fail_count = 0  # 성공 시 초기화
            result.extend(batch_result)
        if i + batch_size < len(articles):
            time.sleep(1)

    if len(result) > 1:
        print(f"  중복 제거 중... (필터링 후 {len(result)}건)")
        result = dedup_deterministic(result)
        print(f"  dedup 후 {len(result)}건")

    # 긴급 3건 초과 시 중요도 판단 후 주의로 강등
    result = regrade_by_score(result)

    return result


def build_exposure_html(entity: str, exposure_data: list, ref_date: str) -> str:
    """익스포저 현황 HTML 생성 — 보유현황/여신 각각 독립 행으로 같은 레벨 표시"""
    rows = find_exposure(entity, exposure_data)
    if not rows:
        return ""
    date_label = f"기준일: {ref_date}" if ref_date else ""

    stock_rows = [r for r in rows if r.get("종목유형","") != "여신"]
    loan_rows  = [r for r in rows if r.get("종목유형","") == "여신"]

    def _fmt_row(r, show_type=True):
        잔고 = float(str(r.get("잔고(억)","0")).replace(",",""))
        고객 = int(float(str(r.get("고객수","0")).replace(",","")))
        type_str = f' <span style="color:#94a3b8;font-size:11px;">({r.get("종목유형","")})</span>' if show_type else ''
        return (
            f'<div style="font-size:13px;color:#1e293b;margin-bottom:2px;">'
            f'<span style="font-weight:bold;">{r.get("종목명","")}</span>{type_str}'
            f' &nbsp;{잔고:,.1f}억원 / {고객:,}명</div>'
        )

    result = ""

    if stock_rows:
        result += f'''<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-left:4px solid #c0392b;background:#ffffff;">
      <tr><td style="padding:10px 14px;">
        <p style="margin:0 0 2px 0;font-size:10px;font-weight:bold;color:#1e293b;letter-spacing:0.5px;">뱅키스 고객 보유현황
          <span style="font-weight:400;color:#94a3b8;">{date_label}</span></p>
        <div style="margin-top:5px;">{"".join([_fmt_row(r) for r in stock_rows])}</div>
      </td></tr>
    </table>'''

    if loan_rows:
        # 여신 잔고 헤더에 종목명 포함 (종목명이 하나면 헤더에, 여럿이면 각 행에)
        if len(loan_rows) == 1:
            loan_name = loan_rows[0].get("종목명", "")
            loan_header = f'{loan_name} 여신 잔고'
            loan_잔고 = float(str(loan_rows[0].get("잔고(억)","0")).replace(",",""))
            loan_고객 = int(float(str(loan_rows[0].get("고객수","0")).replace(",","")))
            loan_body = f'<div style="font-size:13px;color:#1e293b;margin-top:5px;">{loan_잔고:,.1f}억원 / {loan_고객:,}명</div>'
        else:
            loan_header = '여신 잔고'
            loan_body = f'<div style="margin-top:5px;">{"".join([_fmt_row(r, show_type=False) for r in loan_rows])}</div>'

        result += f'''<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-left:4px solid #c0392b;background:#ffffff;margin-top:1px;">
      <tr><td style="padding:10px 14px;">
        <p style="margin:0 0 2px 0;font-size:10px;font-weight:bold;color:#1e293b;letter-spacing:0.5px;">{loan_header}
          <span style="font-weight:400;color:#94a3b8;">{date_label}</span></p>
        {loan_body}
      </td></tr>
    </table>'''

    return result


def build_email_html(articles: list, total_count: int = 0, ai_summary: str = '', exposure_data: dict = None, ref_date: str = '', competitor_notices: list = None, today_str: str = ''):
    exposure_data = exposure_data or {}
    now = datetime.now(timezone(timedelta(hours=9)))  # 한국시간 KST
    sections = {"긴급": [], "주의": [], "참고": []}
    for a in articles:
        sections[a["grade"]].append(a)

    GRADE_STYLE = {
        "긴급": {"header_bg":"#fdf0ef","border_left":"#e57373","label_color":"#c0392b","card_bg":"#fff8f8","card_border":"#f5c6c6"},
        "주의": {"header_bg":"#fefce8","border_left":"#f0b429","label_color":"#b7791f","card_bg":"#fffdf0","card_border":"#f5e09a"},
        "참고": {"header_bg":"#f8fafc","border_left":"#94a3b8","label_color":"#475569","card_bg":"#f8fbff","card_border":"#cbd5e1"},
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
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:14px;border:1px solid {gs["card_border"]};border-bottom:none;background:{gs["header_bg"]};border-top:{f'4px solid {gs["border_left"]}' if grade == '긴급' else f'1px solid {gs["card_border"]}'};border-left:{f'6px solid {gs["border_left"]}' if grade == '긴급' else f'4px solid {gs["border_left"]}'};">
          <tr>
            <td style="padding:10px 14px;">
              <span style="font-size:15px;font-weight:bold;color:{gs["label_color"]};">{grade}</span>
              <span style="display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;background:{gs["border_left"]};color:#fff;font-size:11px;font-weight:700;border-radius:50%;margin-left:6px;vertical-align:middle;">{len(items)}</span>
            </td>
            <td align="right" class="grade-header-right" style="padding:10px 14px;white-space:nowrap;">
              <span style="font-size:10px;{'background:#fee2e2;color:#c0392b;padding:2px 10px;border-radius:10px;font-weight:600;' if grade == '긴급' else 'color:#94a3b8;'}">{GRADE_DESC[grade]}</span>
            </td>
          </tr>
        </table>'''
        for a in display_items:
            if grade == "참고":
                rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;border-left:3px solid #cbd5e1;background:#f8fbff;">
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
                    badges += f'<span style="display:inline-block;font-size:10px;color:#3b5491;background:#e8f0fe;padding:2px 7px;margin-right:4px;margin-bottom:6px;border-radius:3px;">{a["keyword"]}</span>'
                if a.get("entity") and a.get("entity") != a.get("keyword"):
                    badges += f'<span style="display:inline-block;font-size:10px;color:#64748b;background:#f1f5f9;padding:2px 7px;margin-right:4px;margin-bottom:6px;border-radius:3px;">{a["entity"]}</span>'

                if grade == "주의":
                    # 주의 카드 — 긴급과 동일한 행 구조로 정렬
                    c_exp_html = build_exposure_html(a.get("entity",""), exposure_data or {}, ref_date)
                    c_action_row = f'<tr><td style="padding:10px 18px;background:#fff0ee;border-top:1px solid {gs["card_border"]};border-bottom:1px solid {gs["card_border"]};border-left:4px solid #c0392b;"><p style="margin:0 0 3px 0;font-size:10px;font-weight:700;color:{gs["label_color"]};letter-spacing:0.5px;">대응방안</p><p style="margin:0;font-size:12px;color:#1e293b;line-height:1.6;font-weight:500;word-break:keep-all;">{a["action"]}</p></td></tr>' if a.get("action") else ""
                    c_exp_row   = f'<tr><td style="padding:0;">{c_exp_html}</td></tr>' if c_exp_html else ""
                    rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:{gs["card_bg"]};margin-bottom:10px;">
          <tr>
            <td style="padding:10px 18px;">
              {f"<p style='margin:0 0 4px 0;'>{badges}</p>" if badges else ""}
              <a href="{a['url']}" class="title-link caution-title" style="font-weight:bold;font-size:14px;text-decoration:none;color:#1e3a6e;line-height:1.6;word-break:keep-all;display:block;">{_esc(a['title'])}</a>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 0 0;">
                <tr>
                  <td style="font-size:11px;"><a href="{a['url']}" style="color:#3b5491;text-decoration:none;">↗ 기사 보기</a></td>
                  <td align="right" style="font-size:11px;color:#94a3b8;">{a.get("pub_str","")}</td>
                </tr>
              </table>
            </td>
          </tr>
          {c_action_row}{c_exp_row}
        </table>'''
                else:
                    # 긴급 풀카드 B-4 — 리스크점수 + 뱃지 강화 + 좌측 6px
                    exposure_html = build_exposure_html(a.get("entity",""), exposure_data or {}, ref_date)
                    if exposure_html:
                        a["_has_exposure"] = True
                    risk_score = a.get("_risk_score", "")
                    risk_score_html = f'<div style="text-align:right;"><div style="font-size:9px;color:#94a3b8;margin-bottom:1px;">리스크 점수</div><div style="font-size:13px;font-weight:700;color:#3b5491;">{risk_score:.2f}</div></div>' if risk_score else ""
                    # 순위 배지 (긴급 카드 내 순서)
                    urgent_idx = [i for i,x in enumerate(display_items) if x.get("grade")=="긴급"].index(display_items.index(a)) + 1 if a in display_items else 0
                    rank_badge = f'<span style="display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;background:#c0392b;color:#fff;font-size:10px;font-weight:700;border-radius:50%;margin-right:6px;vertical-align:middle;">#{urgent_idx}</span>' if urgent_idx else ""
                    exp_badge = ""  # 익스포저 배지 제거
                    # 긴급 뱃지 — 다크그레이 (빨간 집중은 대응방안 좌측선만)
                    urgent_badges = rank_badge
                    if a.get("keyword"):
                        urgent_badges += f'<span style="font-size:11px;background:#1e293b;color:#fff;padding:3px 9px;border-radius:3px;margin-right:5px;font-weight:700;letter-spacing:0.3px;">{a["keyword"]}</span>'
                    if a.get("entity") and a.get("entity") != a.get("keyword"):
                        urgent_badges += f'<span style="font-size:10px;background:#f1f5f9;color:#475569;padding:2px 7px;border-radius:3px;font-weight:600;">{a["entity"]}</span>'
                    urgent_badges += exp_badge
                    action_row = f'<tr><td bgcolor="#fff0ee" style="padding:10px 18px;border-bottom:2px solid {gs["card_border"]};background:#fff0ee;border-left:4px solid #c0392b;"><p style="margin:0 0 3px 0;font-size:10px;font-weight:bold;color:{gs["label_color"]};letter-spacing:0.5px;">대응방안</p><p style="margin:0;font-size:12px;color:#1e293b;line-height:1.6;font-weight:600;word-break:keep-all;">{a["action"]}</p></td></tr>' if a.get("action") else ""
                    exposure_row = f'<tr><td style="padding:0;border-bottom:1px solid {gs["card_border"]};">{exposure_html}</td></tr>' if exposure_html else ""
                    notice_text = (a["customer_notice"][:200] + "...") if a.get("customer_notice") and len(a["customer_notice"]) > 200 else a.get("customer_notice","")
                    notice_row = f'<tr><td bgcolor="#eff6ff" style="padding:10px 16px;background:#eff6ff;border-top:1px solid #f5c6c6;"><p style="margin:0 0 5px 0;font-size:11px;font-weight:bold;letter-spacing:0.3px;"><span style="background:#2563eb;color:#fff;padding:2px 6px;font-size:10px;margin-right:5px;border-radius:3px;">✦ AI</span><span style="color:#1d4ed8;">고객케어 안내 추천 문구</span></p><p style="margin:0;font-size:12px;color:#1e3a6e;line-height:1.7;white-space:pre-line;word-break:keep-all;">{notice_text}</p></td></tr>' if a.get("customer_notice") else ""
                    bottom_box = f'<tr><td bgcolor="#fff8f8" style="background:#fff8f8;border-top:1px solid {gs["card_border"]};padding:0;"><table width="100%" cellpadding="0" cellspacing="0" border="0">{action_row}{exposure_row}{notice_row}</table></td></tr>' if (action_row or exposure_row or notice_row) else ""
                    rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:{gs["card_bg"]};margin-bottom:10px;">
          <tr>
            <td bgcolor="#fff8f8" style="padding:14px 18px;background:#fff8f8;border-bottom:1px solid #f5c6c6;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:8px;">
                <tr>
                  <td>{f"{urgent_badges}" if urgent_badges else ""}</td>
                  <td align="right" valign="top">{risk_score_html}</td>
                </tr>
              </table>
              <a href="{a['url']}" class="title-link" style="font-weight:700;font-size:15px;text-decoration:none;color:#1e293b;line-height:1.6;word-break:keep-all;display:block;">{_esc(a['title'])}</a>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:5px 0 8px 0;">
                <tr>
                  <td style="font-size:12px;"><a href="{a['url']}" style="color:#3b5491;text-decoration:none;font-weight:500;">↗ 기사 보기</a></td>
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
    .grade-header-right {{ font-size: 10px !important; white-space: normal !important; }}
    .ref-date {{ display: none !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'맑은 고딕',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f6f9;">
<tr><td align="center" class="outer" style="padding:16px;">
<table width="640" cellpadding="0" cellspacing="0" border="0" class="main" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">

  <!-- 헤더 H-3 -->
  <tr>
    <td class="header-td" style="background:#3b5491;padding:18px 26px 14px;">
      <!-- 타이틀 -->
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:12px;">
        <tr>
          <td>
            <p style="margin:0 0 3px 0;font-size:18px;font-weight:bold;color:#ffffff;">eBiz본부 리스크 탐지봇</p>
            <p style="margin:0;font-size:12px;color:#c8d8f0;">{now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 (KST)</p>
          </td>
          <td align="right" valign="top">
            <span style="font-size:10px;color:#c8d8f0;padding:2px 8px;background:#5a7abf;border-radius:2px;white-space:nowrap;">Powered by Claude AI</span>
          </td>
        </tr>
      </table>
      <!-- 필터링 통계 바 -->
      <div style="margin-bottom:12px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:4px;">
          <tr>
            <td style="font-size:11px;color:#c8d8f0;">수집 {total_count}건</td>
            <td align="right" style="font-size:11px;color:#6ee7b7;font-weight:600;">{len(articles)}건 선별 ({round((1 - len(articles)/total_count)*100) if total_count else 0}% 필터링)</td>
          </tr>
        </table>
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#1e3370;border-radius:3px;overflow:hidden;">
          <tr>
            <td width="{max(1, round(len(sections["긴급"])/total_count*100)) if total_count else 1}%" style="background:#ff6b6b;padding:3px 0;"></td>
            <td width="{max(1, round(len(sections["주의"])/total_count*100)) if total_count else 1}%" style="background:#fbbf24;padding:3px 0;"></td>
            <td width="{max(1, round(len(sections["참고"])/total_count*100)) if total_count else 1}%" style="background:#6ee7b7;padding:3px 0;"></td>
            <td style="background:#1e3370;padding:3px 0;"></td>
          </tr>
        </table>
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:4px;">
          <tr>
            <td style="font-size:10px;color:#ff6b6b;">■ 긴급 {len(sections["긴급"])}</td>
            <td style="font-size:10px;color:#fbbf24;">■ 주의 {len(sections["주의"])}</td>
            <td style="font-size:10px;color:#6ee7b7;">■ 참고 {len(sections["참고"])}</td>
            <td align="right" style="font-size:10px;color:#4a6099;">■ 필터링 {total_count - len(articles)}</td>
          </tr>
        </table>
      </div>
      <!-- 대시보드 -->
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#2d4278;">
        <tr>
          <td align="center" style="padding:10px 8px;border-right:1px solid #4a6099;">
            <p class="dash-num" style="margin:0 0 1px 0;font-size:26px;font-weight:bold;color:#ff6b6b;">{len(sections['긴급'])}</p>
            <p style="margin:0 0 2px 0;font-size:12px;color:#d0dcf0;">긴급</p>
            <p style="margin:0;font-size:10px;color:#7a9abf;">당일 확인</p>
          </td>
          <td align="center" style="padding:10px 8px;border-right:1px solid #4a6099;">
            <p class="dash-num" style="margin:0 0 1px 0;font-size:26px;font-weight:bold;color:#fbbf24;">{len(sections['주의'])}</p>
            <p style="margin:0 0 2px 0;font-size:12px;color:#d0dcf0;">주의</p>
            <p style="margin:0;font-size:10px;color:#7a9abf;">모니터링</p>
          </td>
          <td align="center" style="padding:10px 8px;">
            <p class="dash-num" style="margin:0 0 1px 0;font-size:26px;font-weight:bold;color:#6ee7b7;">{len(sections['참고'])}</p>
            <p style="margin:0 0 2px 0;font-size:12px;color:#d0dcf0;">참고</p>
            <p style="margin:0;font-size:10px;color:#7a9abf;">파악용</p>
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



def save_filter_log(raw_articles: list, hard_excluded: list, ai_filtered: list, final_sent: list):
    """필터링 로그 저장 — reason code + confidence 포함"""
    import hashlib
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    log_path = f"filter_log_{now.strftime('%Y%m%d_%H%M')}.json"

    sent_titles          = {a.get("title","") for a in final_sent}
    hard_excl_map        = {a.get("title",""): a.get("_excl_reason","") for a in hard_excluded}
    ai_filtered_titles   = {a.get("title","") for a in ai_filtered}
    ai_conf_map          = {a.get("title",""): a.get("_ai_confidence") for a in ai_filtered}

    # hard_excluded도 raw_articles에 합쳐서 전체 추적
    all_articles = raw_articles + hard_excluded

    logs = []
    for a in all_articles:
        title = a.get("title","")
        h     = hashlib.sha256(title.encode()).hexdigest()[:8]

        if title in hard_excl_map:
            decision   = "HARD_EXCLUDED"
            reason     = hard_excl_map[title]   # 어떤 패턴에 걸렸는지
            confidence = None
        elif title not in ai_filtered_titles:
            decision   = "AI_EXCLUDED"
            reason     = "AI 필터링 제외"
            confidence = ai_conf_map.get(title)  # 낮은 confidence로 제외된 경우 추적
        elif title not in sent_titles:
            decision   = "DEDUP_EXCLUDED"
            reason     = "중복 제거"
            confidence = ai_conf_map.get(title)
        else:
            decision   = "SENT"
            reason     = a.get("grade","")
            confidence = ai_conf_map.get(title)

        logs.append({
            "hash"      : h,
            "title"     : title[:60],
            "keyword"   : a.get("keyword",""),
            "decision"  : decision,
            "reason"    : reason,       # reason code — 통계 집계 가능
            "confidence": confidence,   # AI 확신도 — 0.5~0.7 구간 수동 검토용
        })

    # 제외 사유별 통계
    from collections import Counter
    excl_stats = Counter(
        l["reason"] for l in logs if l["decision"] == "HARD_EXCLUDED"
    )

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump({
                "time"       : now.isoformat(),
                "total"      : len(all_articles),
                "sent"       : len(final_sent),
                "hard_excl"  : len(hard_excluded),
                "ai_excl"    : len(all_articles) - len(hard_excluded) - len(ai_filtered),
                "excl_stats" : dict(excl_stats),   # 제외 사유별 빈도
                "logs"       : logs,
            }, f, ensure_ascii=False, indent=2)
        print(f"  필터링 로그 저장: {log_path} (하드제외 {len(hard_excluded)}건 / 발송 {len(final_sent)}건)")
        if excl_stats:
            top3 = excl_stats.most_common(3)
            top3_str = " | ".join([f"{k}:{v}건" for k, v in top3])
            print(f"  제외 사유 Top3: {top3_str}")
    except Exception as e:
        print(f"  로그 저장 실패: {e}")


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
    now_str_full    = now_kst.strftime("%m월 %d일 %H시 %M분")  # 메일 제목용
    seen_urls       = load_seen_urls()
    seen_combos     = load_seen_combos()   # 실행 간 중복 사건 방지
    seen_context    = load_seen_context()  # 이전 실행 발송 기사 맥락 (title/desc norms)
    sent_urls = set()  # 실제 발송된 기사 URL만 저장 (크롤링 단계 X)
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
                crawl_seen = set()
                for article in articles:
                    if article["url"] not in crawl_seen and article["url"] not in seen_urls:
                        new.append(article)
                        crawl_seen.add(article["url"])
                raw_articles.extend(new)
                print(f"  [{keyword}] 신규 {len(new)}건")
            except Exception as e:
                print(f"  크롤링 오류 [{futures[future]}]: {e}")

    if not raw_articles:
        print("신규 뉴스 없음 — 결과 없음 메일 발송 (특정인만)")
        now = datetime.now(timezone(timedelta(hours=9)))
        subject = f"[리스크 탐지] {now_str_full} 기준 — 신규 뉴스 없음"
        send_email_no_result(subject, build_empty_html(now))
        save_seen_urls(set())
        return

    # 하드 제외룰 적용 — AI 호출 전 사전 필터
    before_hard = len(raw_articles)
    hard_excluded_articles = []
    raw_articles_kept      = []
    for _a in raw_articles:
        _excl, _reason = is_hard_excluded(_a.get("title",""), _a.get("desc",""))
        if _excl:
            _a["_excl_reason"] = _reason   # reason code 저장
            hard_excluded_articles.append(_a)
        else:
            raw_articles_kept.append(_a)
    raw_articles = raw_articles_kept
    if before_hard != len(raw_articles):
        print(f"  하드 제외룰: {before_hard}건 → {len(raw_articles)}건 ({before_hard - len(raw_articles)}건 제거)")

    print(f"\nAI 필터링 중... (총 {len(raw_articles)}건)")
    filtered = ai_filter_and_grade(raw_articles)
    ai_filtered_articles = list(filtered)  # 로그용 AI 통과 기사 저장
    # exposure_data 먼저 로드 — _has_exposure 플래그 설정 + regrade_by_score 보정용
    exposure_data = load_exposure_data()
    for _a in filtered:
        if find_exposure(_a.get("entity",""), exposure_data):
            _a["_has_exposure"] = True
    # 실행 간 중복 사건 필터 — combo + 맥락(title/desc) 기반
    import unicodedata as _ud
    import re as _re2
    try:
        from rapidfuzz import fuzz as _rfuzz
        def _sim(a, b): return _rfuzz.ratio(a, b) / 100.0
    except ImportError:
        from difflib import SequenceMatcher as _SM2
        def _sim(a, b): return _SM2(None, a, b).ratio()

    def _norm(text):
        t = _ud.normalize("NFKC", text or "")
        t = _re2.sub(r"\[.*?\]|\(.*?\)", "", t)
        t = _re2.sub(r"[^가-힣a-zA-Z0-9]", "", t)
        return t.strip()

    # 사건 진행 단계 키워드 — 루프 밖에 선언 (매 기사마다 재생성 방지)
    NEXT_STAGE_KEYWORDS = [
        "가처분", "효력정지", "집행정지", "이의신청", "항고", "재항고",
        "취하", "철회", "기각", "인용", "판결",
        "보류", "재개", "재상장", "거래재개", "상장유지",
        "파산선고", "청산", "폐업", "법정관리", "회생인가", "회생계획",
        "배당", "변제", "채무조정", "출자전환",
        "추가제재", "과징금", "검찰고발", "수사착수",
        "확정판결", "최종확정", "선고확정",
    ]

    def is_next_stage(title: str, desc: str) -> bool:
        text = (title or "") + (desc or "")
        return any(kw in text for kw in NEXT_STAGE_KEYWORDS)

    before_combo = len(filtered)
    filtered_final = []
    # seen_keywords_this_run 제거 — 동일 키워드 내 다른 사건 누락 방지
    # (금양 상폐 + 이화전기 상폐가 같은 "상장폐지" 키워드라도 둘 다 통과해야 함)
    prev_title_norms = seen_context.get("title_norms", [])
    prev_desc_norms  = seen_context.get("desc_norms",  [])
    new_title_norms  = []
    new_desc_norms   = []

    for a in filtered:
        entity   = a.get("entity", "").strip()
        keyword  = a.get("keyword", "").strip()
        event_type = a.get("event_type", "").strip()
        # event_type 있으면 (entity, event_type) — 더 정밀한 사건 구분
        # 없으면 (entity, keyword) fallback
        combo    = (entity, event_type) if entity and event_type else                    (entity, keyword) if entity and keyword else None
        kw_only  = ("", keyword) if keyword else None
        t_norm   = _norm(a.get("title", ""))
        d_norm   = _norm(a.get("desc",  ""))
        matched  = False
        reason   = ""

        # ① 7시간 내 이미 발송된 (entity+keyword) 조합
        if combo and combo in seen_combos:
            if is_next_stage(a.get("title",""), a.get("desc","")):
                pass  # 다음 절차 기사 — 중복이어도 통과
            else:
                matched = True; reason = "동일 사건(entity+kw) 이미 발송"

        # ② keyword만 조합 (entity 없는 경우)
        if not matched and not entity and kw_only and kw_only in seen_combos:
            matched = True; reason = "동일 키워드 이미 발송"

        # ④ 이전 실행 발송 기사와 제목 유사도 (0.88 이상) — 다음 절차 기사는 제외
        if not matched and t_norm and not is_next_stage(a.get("title",""), a.get("desc","")):
            for prev_t in prev_title_norms:
                if _sim(t_norm, prev_t) >= 0.90:
                    matched = True; reason = "이전 실행 발송 기사와 제목 유사"
                    break

        # ⑤ 이전 실행 발송 기사와 desc 유사도 (0.80 이상) — 다음 절차 기사는 제외
        if not matched and d_norm and len(d_norm) > 20 and not is_next_stage(a.get("title",""), a.get("desc","")):
            for prev_d in prev_desc_norms:
                if _sim(d_norm, prev_d) >= 0.82:
                    matched = True; reason = "이전 실행 발송 기사와 내용 유사"
                    break

        if matched:
            print(f"  [{a['grade']}] '{a['title'][:30]}' — {reason}, 스킵")
            continue

        filtered_final.append(a)
        new_title_norms.append(t_norm)
        new_desc_norms.append(d_norm)

    filtered = filtered_final
    if before_combo != len(filtered):
        print(f"  중복 사건 제거: {before_combo}건 → {len(filtered)}건")
    print(f"필터링 후 {len(filtered)}건 선별")

    if not filtered:
        print("AI 필터링 결과 없음 — 결과 없음 메일 발송 (특정인만)")
        now = datetime.now(timezone(timedelta(hours=9)))
        subject = f"[리스크 탐지] {now_str_full} 기준 — 해당 뉴스 없음"
        send_email_no_result(subject, build_empty_html(now))
        save_seen_urls(set())
        return

    print("  본문 크롤링 중... (긴급·주의만)")
    def crawl_body(article):
        # 참고 등급은 본문 불필요 — 속도 개선
        if article.get("grade") == "참고":
            article["body"] = ""
            return article
        body = fetch_article_body(article["url"])
        if body:
            article["body"] = body
            article["_body_failed"] = False
        else:
            # 본문 크롤링 실패 — desc fallback + 플래그 설정
            article["body"] = article.get("desc", "")
            article["_body_failed"] = True
        return article

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(crawl_body, a): a for a in filtered}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  본문 크롤링 오류: {e}")
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
                    "model": CLAUDE_MODEL,
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
- 본문: {body_text[:400]}{" (※ 본문 크롤링 실패 — 제목·요약 기반만 사용, 추측 금지)" if article.get("_body_failed") else ""}
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
        except Exception as e:
            print(f"  대응방안 생성 오류 ({article.get('title','')[:20]}): {e}")

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(generate_action_and_notice, a) for a in filtered]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  대응방안 ThreadPool 오류: {e}")

    now = datetime.now(timezone(timedelta(hours=9)))
    today_str = now.strftime("%m월 %d일")
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

    subject = f"[리스크 탐지] {now_str_full} 기준"
    total_count = len(raw_articles) + len(hard_excluded_articles)  # 하드제외 포함 전체 수집 기준

    # AI 전체 요약 생성
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
                    "model": CLAUDE_MODEL,
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

    # 발송된 기사의 URL + (entity, keyword) 조합 저장
    for a in filtered:
        sent_urls.add(a.get("url", ""))   # 실제 발송 URL만 seen 처리
        entity     = a.get("entity", "").strip()
        keyword    = a.get("keyword", "").strip()
        event_type = a.get("event_type", "").strip()
        # event_type 기반 combo 저장 — 동일 기업 다른 사건 유형 구분
        if event_type and entity:
            new_combos_this_run.add((entity, event_type))
        elif keyword:
            new_combos_this_run.add((entity, keyword))
    save_seen_urls(sent_urls, new_combos_this_run,
                   title_norms=new_title_norms, desc_norms=new_desc_norms)
    # 필터링 로그 저장 (튜닝·역추적용)
    save_filter_log(raw_articles, hard_excluded_articles,
                    ai_filtered_articles, filtered)


if __name__ == "__main__":
    main()
