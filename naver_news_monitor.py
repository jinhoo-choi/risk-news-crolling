RELATED_STOCK_MAP = {
    # 증권사 → 상장 지주·모회사
    "한국투자증권":   "한국금융지주",
    "한투증권":       "한국금융지주",
    "미래에셋증권":   "미래에셋증권",
    "삼성증권":       "삼성증권",
    "NH투자증권":     "NH투자증권",
    "KB증권":         "KB금융",
    "신한투자증권":   "신한지주",
    "하나증권":       "하나금융지주",
    "키움증권":       "키움증권",
    "대신증권":       "대신증권",
    "유안타증권":     "유안타증권",
    "메리츠증권":     "메리츠금융지주",
    "교보증권":       "교보생명",
    "IBK투자증권":    "기업은행",
    "SK증권":         "SK증권",
    # 은행 → 지주
    "국민은행":       "KB금융",
    "신한은행":       "신한지주",
    "하나은행":       "하나금융지주",
    "우리은행":       "우리금융지주",
    "기업은행":       "기업은행",
    "농협은행":       "NH투자증권",
    "카카오뱅크":     "카카오뱅크",
    "케이뱅크":       "케이뱅크",
    # 카드·보험 → 지주
    "국민카드":       "KB금융",
    "신한카드":       "신한지주",
    "삼성생명":       "삼성생명",
    "한화생명":       "한화생명",
    "교보생명":       "교보생명",
    "메리츠화재":     "메리츠금융지주",
    # 주요 대기업 계열
    "삼성물산":       "삼성물산",
    "SK이노베이션":   "SK이노베이션",
    "현대캐피탈":     "현대차",
    "현대카드":       "현대차",
    "롯데카드":       "롯데지주",
    # 감독·규제 기관 (관련주 없음)
    "금융감독원":     None,
    "금감원":         None,
    "금융위원회":     None,
    "한국은행":       None,
    "금융위":         None,
    "한국거래소":     None,
    "예탁결제원":     None,
}

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
import unicodedata
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

# 해외주식 포괄 리스크 키워드 — 종목명 불필요, AI가 entity 추출
OVERSEAS_KEYWORDS = [
    "반도체주 급락",      # 브로드컴·마이크론 등 섹터 기사
    "해외주식 실적쇼크",  # 실적 쇼크 포괄
    "나스닥 급락",        # 나스닥 전반 급락
    "미국주식 상장폐지",  # 해외 상폐
    "미국주식 파산",      # 해외 파산
    "해외주식 급락",      # 해외 전반
]

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

# 중복 제거 유사도 임계값 — 운영 중 조정 가능
TITLE_SIM_THRESHOLD = 0.92  # 제목 유사도 (연합뉴스 재인용 대응)
DESC_SIM_THRESHOLD  = 0.84  # 본문 요약 유사도 (0.84: 안정적, 0.76은 정상 기사 누락 위험)

# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# 해외주식 티커 → 한글 종목명 매핑
# exposure_data.csv 해외 종목이 티커(NVDA 등)로 입력된 경우 자동 변환
# ─────────────────────────────────────────────
TICKER_TO_NAME = {
    # ticker_map.json 미존재 시 최소 fallback — Actions에서 자동 갱신
    "TSLA":"테슬라",    "NVDA":"엔비디아",  "GOOGL":"알파벳",   "AAPL":"애플",
    "MSFT":"마이크로소프트","META":"메타",   "AMZN":"아마존",    "AVGO":"브로드컴",
    "MU":"마이크론",    "INTC":"인텔",      "AMD":"AMD",         "QCOM":"퀄컴",
    "TSM":"TSMC",       "PLTR":"팔란티어",  "IONQ":"아이온큐",   "SOXL":"반도체레버리지ETF",
    "QQQ":"나스닥100 ETF","TQQQ":"나스닥3배 ETF","VOO":"뱅가드S&P500 ETF","SPY":"SPDR S&P500 ETF",
}
NAME_TO_TICKER = {v: k for k, v in TICKER_TO_NAME.items()}

# ticker_map.json 런타임 로드 (ticker_mapper.py가 생성)
def _load_ticker_map() -> dict:
    """ticker_map.json 로드 — 없으면 TICKER_TO_NAME fallback"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticker_map.json")
        if os.path.exists(p):
            m = json.load(open(p, encoding="utf-8"))
            return {**TICKER_TO_NAME, **m}  # json이 우선
    except Exception:
        pass
    return TICKER_TO_NAME

TICKER_MAP_RUNTIME = _load_ticker_map()



# 언론사 신뢰도 가중치 — AI confidence 보정
# 주요 금융·경제 전문 언론: +0.05 / 출처 불명·커뮤니티: -0.05
# 도메인 기반 언론사 신뢰도 가중치
MEDIA_TRUST = {
    # 신뢰도 높음 (+0.05) — 금융·경제 전문 언론
    "yna.co.kr":     +0.05,  # 연합뉴스
    "news1.kr":      +0.05,  # 뉴스1
    "hankyung.com":  +0.05,  # 한국경제
    "mk.co.kr":      +0.05,  # 매일경제
    "edaily.co.kr":  +0.05,  # 이데일리
    "thebell.co.kr": +0.05,  # 더벨
    "mt.co.kr":      +0.05,  # 머니투데이
    "fnnews.com":    +0.05,  # 파이낸셜뉴스
    "wowtv.co.kr":   +0.04,  # 한국경제TV
    "mbc.com":       +0.04,  # MBC
    "kbs.co.kr":     +0.04,  # KBS
    "sbs.co.kr":     +0.04,  # SBS
    "chosun.com":    +0.04,  # 조선비즈
    "joongang.co.kr":+0.04,  # 중앙일보
    "donga.com":     +0.04,  # 동아일보
    "asiae.co.kr":   +0.03,  # 아시아경제
    "sedaily.com":   +0.03,  # 서울경제
    "heraldcorp.com":+0.03,  # 헤럴드경제
    "bizwatch.co.kr":+0.03,  # 비즈워치
    "infostock.co.kr":+0.03, # 인포스탁
    # 신뢰도 낮음 (-0.05) — 출처 불명
    "blog.naver.com":-0.05,
    "tistory.com":   -0.05,
    "cafe.daum.net": -0.05,
    "investing.com": -0.03,
}

def get_media_boost(url: str) -> float:
    """URL 도메인 기반 언론사 신뢰도 보정값 반환"""
    url_lower = url.lower()
    for domain, boost in MEDIA_TRUST.items():
        if domain in url_lower:
            return boost
    return 0.0

def get_price_change(entity: str) -> float | None:
    """해외주식 당일 등락률 조회 (yfinance) — 실패 시 None 반환
    entity: 한글 종목명 (내부적으로 티커로 역변환)
    반환: 등락률 (예: -12.6) 또는 None
    """
    try:
        import yfinance as yf
        # 한글명 → 티커 역변환
        ticker = NAME_TO_TICKER.get(entity)
        if not ticker:
            # TICKER_MAP_RUNTIME 역방향 조회
            for t, n in TICKER_MAP_RUNTIME.items():
                if n == entity:
                    ticker = t
                    break
        if not ticker:
            return None
        data = yf.Ticker(ticker).fast_info
        change = getattr(data, "three_month_change", None)
        # 당일 등락률: (현재가 - 전일종가) / 전일종가 * 100
        prev = getattr(data, "previous_close", None)
        curr = getattr(data, "last_price", None)
        if prev and curr and prev > 0:
            return round((curr - prev) / prev * 100, 2)
    except Exception:
        pass
    return None


def normalize_ticker(name: str) -> str:
    """종목명이 티커(영문 대문자)이면 한글명으로 변환, 아니면 그대로 반환
    ticker_map.json → TICKER_TO_NAME 순으로 조회
    예: 'NVDA' → '엔비디아', 'IONQ' → '아이온큐', '삼성전자' → '삼성전자'
    """
    stripped = name.strip()
    if re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', stripped):
        return TICKER_MAP_RUNTIME.get(stripped, stripped)
    return stripped

def load_exposure_data() -> dict:
    """CSV에서 eBiz본부 익스포저 데이터 로드 — {종목명: [row, ...]} 리스트 딕셔너리 반환
    컬럼 순서: 기준일, 종목명, 종목유형, 잔고(억), 고객수[, 시장]
    시장 컬럼 없으면 국내로 자동 처리 (하위 호환)
    헤더 깨진 경우 positional 파싱으로 자동 fallback"""
    if not os.path.exists(EXPOSURE_FILE):
        return {}
    try:
        with open(EXPOSURE_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows_all = list(reader)
            if rows_all and "종목명" in (rows_all[0] or {}):
                result = {}
                for row in rows_all:
                    name = normalize_ticker(row.get("종목명", "").strip())
                    row["종목명"] = name  # 티커→한글명 정규화
                    if not name:
                        continue
                    # 시장 컬럼 없으면 국내 기본값
                    if "시장" not in row:
                        row["시장"] = "국내"
                    result.setdefault(name, []).append(row)
                return result
    except Exception:
        pass
    # fallback: positional 파싱
    try:
        result = {}
        with open(EXPOSURE_FILE, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            has_market = "시장" in header
            for row in reader:
                if len(row) < 5:
                    continue
                d = {
                    "기준일":   row[0].strip(),
                    "종목명":   row[1].strip(),
                    "종목유형": row[2].strip(),
                    "잔고(억)": row[3].strip(),
                    "고객수":   row[4].strip(),
                    "시장":     row[5].strip() if has_market and len(row) > 5 else "국내",
                }
                name = normalize_ticker(d["종목명"])
                d["종목명"] = name
                if name:
                    result.setdefault(name, []).append(d)
        return result
    except Exception:
        return {}

def get_overseas_keywords(exposure_data: dict = None, top_n: int = 30) -> list:
    """해외주식 포괄 리스크 키워드 반환
    종목명 기반 동적 키워드 대신 포괄 키워드 사용
    → ticker_mapper 선행 불필요, AI가 기사에서 entity 직접 추출
    """
    return list(OVERSEAS_KEYWORDS)

def find_exposure(entity: str, exposure_data: dict) -> list:
    """entity와 종목명 딕셔너리 매칭 — O(1) 정확 매칭 우선, fallback prefix 6자
    성능 최적화: 정확 매칭(O1) → 부분포함 문자열(O(n)) → prefix 6자(O(n))
    2만행에서도 정확 매칭은 즉시, 미등록 종목도 빠르게 반환
    """
    if not entity or not exposure_data:
        return []

    # 1) 정확 매칭 — O(1)
    if entity in exposure_data:
        return exposure_data[entity]

    # 2) 부분포함 + prefix 6자 — entity가 name에 포함되거나 그 반대
    #    정규식 컴파일 1회만 수행 (루프 밖)
    results = []
    seen_names = set()
    clean_e = re.sub(r'[(주)㈜\s]', '', entity)
    ce_len = len(clean_e)

    for name, rows in exposure_data.items():
        if name in seen_names:
            continue

        # 단순 부분 문자열 포함 검사 (정규식 대신 → 10배 빠름)
        if entity in name:
            results.extend(rows)
            seen_names.add(name)
            continue

        # prefix 6자 매칭 — 법인명 축약 대응 (제이알글로벌리츠 ↔ 제이알글로벌위탁관리...)
        if ce_len >= 4:
            clean_n = re.sub(r'[(주)㈜\s]', '', name)
            if len(clean_n) >= 4:
                plen = 0
                for a, b in zip(clean_e, clean_n):
                    if a == b:
                        plen += 1
                    else:
                        break
                    if plen >= 6:
                        results.extend(rows)
                        seen_names.add(name)
                        break

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
    data_dir = "data"
    if not os.path.exists(data_dir):
        return []
    try:
        import csv as _csv
        for fname in os.listdir(data_dir):
            if not fname.endswith(".csv") or fname == "broker_notices_merged.csv":
                continue
            fpath = os.path.join(data_dir, fname)
            if os.path.getsize(fpath) < 50:
                continue
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
        return f"""<div style="margin-bottom:10px;">
          <p style="margin:0 0 3px 0;font-size:11px;font-weight:700;color:#334155;letter-spacing:0.3px;">{label}</p>
          <p style="margin:0;font-size:12px;color:#334155;line-height:1.7;">{content_html}</p>
        </div>"""

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

    return f"""<p style="margin:0 0 10px 0;font-size:13px;font-weight:bold;color:#334155;letter-spacing:0.3px;">AI 분석 요약</p>
      <div>{rows_html}</div>"""

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
    """최근 24시간 키(YYYY-MM-DD HH) 기준 seen URL 로드 — 오래된 키 자동 제거"""
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
    """최근 24시간 내 발송된 (entity, keyword) 조합 로드"""
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
    """최근 24시간 내 발송된 기사의 title_norms·desc_norms 로드 — 맥락 기반 중복 감지"""
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
    """현재 시각 키(YYYY-MM-DD HH)로 seen URL + 발송 조합 저장 — 최근 24시간 키만 보존"""
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
    cur = existing.get(current_key, {})
    if isinstance(cur, list):
        cur = {"urls": cur, "combos": [], "title_norms": [], "desc_norms": []}
    existing_urls   = set(cur.get("urls", []))
    existing_combos = [tuple(x) for x in cur.get("combos", [])]
    existing_titles = cur.get("title_norms", [])
    existing_descs  = cur.get("desc_norms", [])
    merged_urls = list(existing_urls | seen)
    merged_urls = merged_urls[-500:]
    if combos:
        for combo in combos:
            if combo not in [tuple(x) for x in existing_combos]:
                existing_combos.append(list(combo))
    existing_combos = existing_combos[-300:]
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
                    "desc"   : (desc[:120].rsplit(" ", 1)[0] if len(desc) > 120 and " " in desc[:120] else desc[:120]) if desc else "",
                    "url"    : link,
                    "pubDate": pub,
                    "keyword": keyword,
                    "body"   : "",
                })

        total = data.get("total", 0)
        start += 100
        if stop or len(items) < 100 or start >= 301:
            break

    return articles

def fetch_article_body(url: str) -> str:
    """기사 본문 크롤링 — Session + 헤더 강화로 WAF 대응 / 2MB 초과 시 스킵"""
    try:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(max_retries=2)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        res = session.get(url, timeout=12, stream=True, headers={
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://search.naver.com/",
            "Connection": "keep-alive",
        })
        content_length = int(res.headers.get("Content-Length", 0))
        if content_length > 2_000_000:
            return ""
        res_text = res.text
        res.raise_for_status()
        soup = BeautifulSoup(res_text, "html.parser")
        for selector in ["#dic_area", "#articleBodyContents", ".article-body", "#articeBody", "article"]:
            tag = soup.select_one(selector)
            if tag:
                text = tag.get_text(separator=" ", strip=True)
                return text[:600]
        paragraphs = soup.find_all("p")
        text = " ".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20)
        return text[:600]
    except Exception:
        return ""

# ─────────────────────────────────────────────
# 하드 제외 패턴 — AI 호출 전 사전 필터
# ─────────────────────────────────────────────
TITLE_ONLY_PATTERNS = [
    "시황", "장마감", "마감 시황", "마감 종합", "마켓",
    "베스트&워스트", "베스트 워스트", "주간 상승", "주간 하락",  # 주간 랭킹·요약 기사
    "이주의 베스트", "이주의 워스트", "주간 수익률",
    "목표가", "목표주가", "투자의견", "매수", "매도", "중립",
    "브리핑", "뉴스브리핑", "이모저모",
    "특징주", "투자전략", "포트폴리오",
    "신고가", "급등", "상한가", "흑자전환", "실적개선", "호실적",
    "목표달성", "수주", "계약체결", "MOU", "협약",
    "순매수", "순매도", "외국인매수", "외국인매도", "거래대금",
    "팔자", "사자", "개미", "외인", "시총", "세계 ",
    "개인 투자", "개인투자자", "ETF",
    "잔고 최고", "잔고 최대", "잔고 돌파", "잔고 역대", "잔고 사상 최대",
  
    "당기순익", "당기순이익", "영업이익", "순이익",
    "실적 개선", "실적 호조", "실적 발표", "연간 실적",
    # ※ "실적 쇼크", "실적 예상치 하회" 등 해외 쇼크성 기사는 통과
    # 분기 단독 패턴 제거 — 해외 실적 쇼크 기사("2분기 실적 쇼크") 오차단 방지
    # 국내 분기 실적은 영업이익·순이익·실적개선 패턴으로 커버
    "성장세", "성장률", "증가율", "전년比", "전년비",
    "보험사", "은행권", "저축은행", "캐피탈",
    "부고", "인사", "승진", "선임", "취임", "퇴임",
    "할인", "이벤트", "휴무일", "영업시간", "프로모션",  # 마케팅·이벤트 기사
    "밸류에이션", "고평가", "저평가",  # 가치평가 분석·전망 기사
    "경고음", "빨간불", "신호탄",
    "가능성에", "가능성 제기", "우려 커", "걱정 커", "불안 커",
    "뉴욕증시", "나스닥 혼조", "뉴욕 혼조", "월가",
  
    "가상자산", "암호화폐", "코인", "비트코인", "이더리움", "알트코인",
    "솔라나", "리플", "도지코인", "NFT", "디파이", "Web3",
    "다우존스", "S&P500 하락", "나스닥 하락세", "나스닥 소폭 하락",
    "나스닥 약세", "나스닥 혼조", "나스닥 하락세",
    "빅테크 약세", "빅테크 혼조", "빅테크 전반",
    "2금융권", "저축은행권", "캐피탈업",
    "주요공시", "주요 공시", "공시 모음", "공시브리핑",
    "오늘의 공시", "장마감 공시", "장전 공시", "오전 공시", "오후 공시",
    "월 일 주식시장", "주식시장 주요",
    "풍문레이다", "풍문 레이더", "루머", "카더라",
    "휴업설", "폐점설", "철수설", "매각설", "파산설",
    "시사풍월", "시장풍월", "직격인터뷰", "논설위원", "사설",
    "위기 탈출", "위기탈출", "거래정지 해제", "거래재개", "매매거래 정지 해제",
    "액면병합", "주권 변경상장",
    "전망", "소식",
    "(完)", "(완)", "①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
    "현직이 푸는", "전문가가 보는", "기자가 간다",
    "후보", "공약", "선거", "시의원", "구의원", "도의원", "국회의원", "시장 출마", "당선",
    "복합문화", "재개발", "부지 활용", "도시재생", "리모델링",
]

TEXT_PATTERNS = [
    "분석", "리포트", "보고서", "추천",
    "인터뷰", "기획", "특집", "르포", "칼럼", "오피니언", "사설", "논설",
]

TITLE_ONLY_PATTERNS += [
    "기자수첩", "기자의 눈", "기자노트", "취재후기", "현장에서", "데스크에서",
    "잡아낸", "변호사", "법률가", "판사", "검사", "사례로 보는", "이야기",
    "하고 싶으면", "하려면", "하는 법", "Q&A", "궁금증",
    "갑질", "피해자", "제보",
  
    "만든 ", "만들었", "버는 법", "번 ", "모은 ", "불린 ",
    "싱글맘", "직장인", "주부", "2030", "MZ",
    "마용성", "강남", "부동산 투자",
  
    "분쟁사례", "투자 사례", "실패 사례", "투자경보",
    "따라하기", "팬덤", "묻지마",

    # ── 스팩(SPAC) — 정상 청산, 실질 리스크 없음 ──
    "스팩", "SPAC", "기업인수목적",

    # ── 호재·긍정 기사 — 리스크 아님 ──
    "밸류업",                        # 기업가치 제고 기획 기사
    "방카",                          # 방카슈랑스 판매 호조 기사
    "인수 효과",                     # M&A 시너지·성과 기사
    "체질 변신", "체질 개선",        # 구조조정 긍정 평가 기사
    "알짜",                          # '알짜 체질', '알짜 자산' 등 긍정 기사
    "주식병합",                      # 병합은 상폐 아님 (액면병합 기존 패턴 보완)

    # ── 시황·거시 일반론 — 리스크 특정 불가 ──
    "약세장", "강세장", "조정장",
    "금리 인하", "금리 인상", "기준금리",
    "환율 ", "달러 강세", "달러 약세",
    "무역전쟁", "관세 부과",

    # ── 정책·제도 발표 — 당사 직접 영향 없음 ──
    "공시 의무", "공시 강화", "제도 개선", "규정 개정",
    "금융위 발표", "금감원 발표",
]

EXCLUDE_TITLE_RE_PATTERNS = [
    r"\[단독\].*인터뷰",
    r"\[기획\]",
    r"\[특집\]",
    r"①|②|③|④|⑤",
    r"^\d+위\s",
    r"\[.{2,15}(분쟁|사례|점검|리뷰|진단|해설|칼럼)\]",
    r"\[.{2,15}(기획|특집|연재|시리즈)\]",
    # 더벨 시리즈 기획 기사 — [더벨][부제] 이중 브래킷 형태
    r"\[더벨\]\[.+\]",
]

def is_hard_excluded(title: str, desc: str = "") -> tuple:
    """하드 제외 패턴 매칭 — (excluded: bool, reason: str) 반환"""

    # 치명적 키워드 bypass — AI 판단으로 넘김
    CRITICAL_KW = ["상장폐지", "파산", "부도", "횡령", "배임", "거래정지",
                   "기업회생", "MTS 장애", "MTS 접속 장애"]
    # 스팩·정상상폐·호재성 기사는 CRITICAL_KW bypass 면제 → 하드제외 적용
    CRITICAL_EXEMPT = ["스팩", "SPAC", "기업인수목적", "알짜", "체질 변신", "체질 개선",
                       "방카", "인수 효과", "밸류업", "주식병합"]
    if any(kw in title for kw in CRITICAL_KW):
        if not any(ex in title for ex in CRITICAL_EXEMPT):
            return False, None  # 치명적 키워드 → AI 판단으로 넘김
    # 대형 익스포저 섹터 + 리스크 표현 조합 → 밸류에이션 패턴 있어도 통과
    SECTOR_KW  = ["반도체", "AI", "엔비디아", "테슬라", "배터리", "전기차",
                  "바이오", "금융주", "은행주", "삼성전자", "하이닉스"]
    RISK_EXPR  = ["급락", "쇼크", "위기", "리스크", "균열", "붕괴", "흔들", "패닉"]
    if any(s in title for s in SECTOR_KW) and any(r in title for r in RISK_EXPR):
        return False, None  # 섹터 리스크 기사 → AI 판단

    for pat in TITLE_ONLY_PATTERNS:
        if pat in title:
            return True, pat
    text = title + " " + (desc or "")
    for pat in TEXT_PATTERNS:
        if pat in text:
            return True, pat
    for pat in EXCLUDE_TITLE_RE_PATTERNS:
        if re.search(pat, title):
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

    _fp_tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
        "filter_prompt.txt"), encoding="utf-8").read()
    _fp_static = _fp_tpl.replace("{numbered}", "")  # 캐싱용 고정 부분
    _fp_dynamic = numbered                            # 가변 뉴스 목록
    prompt = _fp_tpl.replace("{numbered}", numbered)  # 기존 호환용
    for attempt in range(3):
        try:
            _t0 = time.time()
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "prompt-caching-2024-07-31",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 6000,
                    "temperature": 0.0,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": _fp_static,
                         "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": _fp_dynamic},
                    ]}],
                },
                timeout=60,
            )
            print(f"  [AI] 배치 {offset//50+1} 응답 {time.time()-_t0:.1f}초")
            if res.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"  Rate limit 429 — {wait}초 대기 후 재시도 ({attempt+1}/3)")
                time.sleep(wait)
                continue
            res.raise_for_status()
            payload = res.json()
            stop_reason = payload.get("stop_reason", "")
            if stop_reason == "max_tokens":
                raise ValueError(f"응답 max_tokens 초과로 잘림 (배치 {offset//50+1}) — max_tokens 증가 필요")
            content = payload.get("content", [])
            if not content:
                raise ValueError("Claude 응답 content 비어있음")
            raw = "\n".join(
                blk.get("text", "") for blk in content
                if blk.get("type") == "text"
            ).strip()
            if not raw:
                raise ValueError("Claude 응답 text 비어있음")
            raw = raw.replace("```json", "").replace("```", "").strip()
            _start = raw.find("[")
            _end   = raw.rfind("]")
            if _start == -1 or _end == -1 or _end <= _start:
                raise ValueError("JSON 배열을 찾을 수 없음 (rfind 불일치)")
            raw = raw[_start:_end + 1]
            try:
                try:
                    grades = json.loads(raw)
                except json.JSONDecodeError:
                    from json_repair import repair_json
                    repaired = repair_json(raw)
                    grades = json.loads(repaired)
                    print(f"  JSON repair 적용됨 (배치 {offset//50+1})")
            except json.JSONDecodeError as je:
                raise ValueError(f"JSON 파싱 실패: {je}") from je
            if not isinstance(grades, list):
                raise ValueError(f"grades가 list가 아님: {type(grades)}")
            grade_map = {g["id"]: g for g in grades}
            result = []
            for i, article in enumerate(batch):
                info = grade_map.get(i + offset + 1, {})
                article["_ai_confidence"] = info.get("confidence", None)
                if info.get("relevant") and info.get("grade"):
                    if not info.get("entity", "").strip():
                        print(f"  [entity 빈값] relevant 무효화: {article.get('title','')[:30]}")
                        continue
                    article["grade"]      = info["grade"]
                    article["reason"]     = info.get("reason", "")
                    article["action"]     = info.get("action", "")
                    article["entity"]     = info.get("entity", "").strip()
                    article["entities"]   = [e.strip() for e in info.get("entities", []) if e.strip()] or [info.get("entity","").strip()]
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
                time.sleep(random.uniform(20, 45))
                continue
            return []
    return []

def dedup_deterministic(articles: list) -> list:
    """3단계 중복 제거 — 제목 유사도 + 기업명·키워드 조합 + desc 유사도"""

    try:
        from rapidfuzz import fuzz as _fuzz
        def _ratio(a, b): return _fuzz.ratio(a, b) / 100.0
    except ImportError:
        from difflib import SequenceMatcher
        def _ratio(a, b): return SequenceMatcher(None, a, b).ratio()

    def normalize(text: str) -> str:
        t = unicodedata.normalize("NFKC", text)
        t = re.sub(r"\[.*?\]|\(.*?\)", "", t)
        t = re.sub(r"속보|단독|긴급|종합", "", t)
        t = re.sub(r"[^가-힣a-zA-Z0-9]", "", t)
        return t.strip()

    seen_norms    = []
    seen_entities = []
    seen_combos   = {}
    seen_descs    = []
    result = []

    _NEXT_STAGE = {
        "가처분","효력정지","집행정지","이의신청","항고","판결",
        "보류","재개","재상장","거래재개","상장유지",
        "파산선고","청산","폐업","회생인가","회생계획",
        "배당","변제","채무조정","추가제재","과징금","검찰고발",
    }
    def _is_next_stage_det(title: str) -> bool:
        return any(kw in title for kw in _NEXT_STAGE)

    seen_urls_local = set()

    for a in articles:
        title_norm = normalize(a.get("title", ""))
        desc_norm  = normalize(a.get("desc", ""))
        entity     = a.get("entity", "").strip()
        keyword    = a.get("keyword", "").strip()
        combo      = (entity, keyword) if entity else None

        url = a.get("url", "").strip()
        if url and url in seen_urls_local:
            continue
        if url:
            seen_urls_local.add(url)

        if _is_next_stage_det(a.get("title", "")):
            seen_norms.append(title_norm)
            seen_entities.append(entity)
            seen_descs.append(desc_norm)
            if combo:
                seen_combos[combo] = desc_norm
            result.append(a)
            continue

        matched = False

        for existing_norm, existing_entity in zip(seen_norms, seen_entities):
            if _ratio(title_norm, existing_norm) >= TITLE_SIM_THRESHOLD:
                if not entity or not existing_entity or entity == existing_entity:
                    matched = True
                    break

        if not matched and combo and combo in seen_combos:
            existing_desc = seen_combos[combo]
            if desc_norm and existing_desc:
                if _ratio(desc_norm, existing_desc) >= DESC_SIM_THRESHOLD - 0.06:
                    matched = True
            else:
                matched = True

        if not matched and desc_norm and len(desc_norm) > 20:
            for existing_desc in seen_descs:
                if _ratio(desc_norm, existing_desc) >= DESC_SIM_THRESHOLD:
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

GRADE_LIMITS = {"긴급": 2, "주의": 3, "참고": 5}

RISK_PRIORITY = {
    "한국투자증권": 2.0,
    "MTS": 1.8, "HTS": 1.8,
    "전산장애": 1.8, "전산사고": 1.8,
    "상장폐지": 1.5, "파산": 1.5,
    "부도": 1.5, "거래정지": 1.5,
    "반대매매": 1.4, "강제청산": 1.4,
    "기업회생": 1.3, "워크아웃": 1.2,
}

# 당사 직접 이슈 키워드 — 익스포저 페널티 면제 + 긴급 강제 지정
DIRECT_INCIDENT_KW = {
    "한국투자증권", "MTS", "HTS",
    "전산장애", "전산사고", "접속장애", "접속불가",
}

def calc_risk_score(article: dict, exposure_data: dict = None) -> float:
    """리스크 점수 = (confidence × 키워드 가중치 + 익스포저 보정) × 5 → 10점 만점
    익스포저 보정(exp_boost): 잔고 합산 구간별 차등
      초대규모 (500억 이상) → +0.20
      대규모 (100~500억)   → +0.15
      중규모 (10~100억)    → +0.10
      소규모 (10억 미만)    → +0.05
      없음 (일반 기사)      → -0.05  ※ 당사 직접 이슈(MTS·전산장애 등) 면제
    국내·해외 동일 기준 적용
    """
    conf  = article.get("_ai_confidence") or 0.3
    title = article.get("title", "") + article.get("reason", "")
    kw_weight = max(
        [v for k, v in RISK_PRIORITY.items() if k in title],
        default=1.0
    )
    is_direct_incident = any(kw in title for kw in DIRECT_INCIDENT_KW)

    # 익스포저 잔고 합산 → 구간별 boost
    exp_boost = 0.0
    if exposure_data is not None:
        entity = article.get("entity", "").strip()
        rows = find_exposure(entity, exposure_data) if entity else []
        if rows:
            article["_has_exposure"] = True
            total_bal = sum(
                float(str(r.get("잔고(억)", "0")).replace(",", ""))
                for r in rows
            )
            if total_bal >= 500:
                exp_boost = 0.20
            elif total_bal >= 100:
                exp_boost = 0.15
            elif total_bal >= 10:
                exp_boost = 0.10
            else:
                exp_boost = 0.05
        elif article.get("_has_exposure"):
            exp_boost = 0.05
        else:
            # 익스포저 없음 — 당사 직접 이슈 아니면 소폭 페널티
            if not is_direct_incident:
                exp_boost = -0.05
    elif article.get("_has_exposure"):
        exp_boost = 0.05
    raw = conf * kw_weight + exp_boost
    return round(min(raw * 5, 10.0), 1)

def regrade_by_score(articles: list, exposure_data: dict = None) -> list:
    """등급별 상한 초과 시 리스크 점수 기반으로 하위 등급 강등"""
    for a in articles:
        a["_risk_score"] = calc_risk_score(a, exposure_data)

    # ── 당사 직접 이슈 긴급 강제 (confidence·GRADE_LIMITS 면제) ──────────
    for a in articles:
        if any(kw in a.get("title", "") for kw in DIRECT_INCIDENT_KW):
            if a.get("grade") != "긴급":
                print(f"  [직접이슈 강제긴급] {a['title'][:40]}")
            a["grade"] = "긴급"
            a["_force_urgent"] = True
    # ─────────────────────────────────────────────────────────────────────

    urgent  = sorted([a for a in articles if a.get("grade") == "긴급"],
                     key=lambda x: x["_risk_score"], reverse=True)
    caution = sorted([a for a in articles if a.get("grade") == "주의"],
                     key=lambda x: x["_risk_score"], reverse=True)
    ref     = sorted([a for a in articles if a.get("grade") == "참고"],
                     key=lambda x: x["_risk_score"], reverse=True)

    result = []

    for a in urgent[:]:
        conf = a.get("_ai_confidence") or 0
        # _force_urgent 플래그 있으면 confidence 강등 면제
        if not a.get("_force_urgent") and conf < 0.85:
            a["grade"] = "주의"
            a["customer_notice"] = None
            urgent.remove(a)
            caution.append(a)
            print(f"  [confidence 강등] 긴급→주의 (conf={conf:.2f}): {a['title'][:30]}")

    for a in caution[:]:
        conf = a.get("_ai_confidence") or 0
        if conf < 0.60:
            a["grade"] = "참고"
            caution.remove(a)
            ref.append(a)
            print(f"  [confidence 강등] 주의→참고 (conf={conf:.2f}): {a['title'][:30]}")

    for i, a in enumerate(urgent):
        # _force_urgent는 GRADE_LIMITS 상한도 면제
        if a.get("_force_urgent") or i < GRADE_LIMITS["긴급"]:
            result.append(a)
        else:
            a["grade"] = "주의"
            a["customer_notice"] = None
            caution.append(a)
            print(f"  [강등] 긴급→주의: {a['title'][:35]}")

    caution_sorted = sorted(caution, key=lambda x: x.get("_risk_score") or 0, reverse=True)
    for i, a in enumerate(caution_sorted):
        if i < GRADE_LIMITS["주의"]:
            result.append(a)
        else:
            a["grade"] = "참고"
            ref.append(a)
            print(f"  [강등] 주의→참고: {a['title'][:35]}")

    ref_sorted = sorted(ref, key=lambda x: x.get("_risk_score") or 0, reverse=True)
    for i, a in enumerate(ref_sorted):
        if i < GRADE_LIMITS["참고"]:
            result.append(a)
        else:
            print(f"  [제외] 참고 초과: {a['title'][:35]}")

    urgent_cnt  = sum(1 for a in result if a.get("grade") == "긴급")
    caution_cnt = sum(1 for a in result if a.get("grade") == "주의")
    ref_cnt     = sum(1 for a in result if a.get("grade") == "참고")

    # 해외주식 실시간 주가 보정 — yfinance 조회 실패 시 무시
    for a in result:
        entity = a.get("entity","")
        exp_rows = find_exposure(entity, exposure_data or {})
        if not any(r.get("종목유형","") in ("해외주식","해외대출") for r in exp_rows):
            continue
        change = get_price_change(entity)
        if change is None:
            continue
        a["_price_change"] = change
        if change <= -10:
            a["_ai_confidence"] = min(a.get("_ai_confidence",0.7) + 0.10, 1.0)
            print(f"  [주가보정] {entity} {change:+.1f}% → conf+0.10")
        elif change <= -5:
            a["_ai_confidence"] = min(a.get("_ai_confidence",0.7) + 0.05, 1.0)
            print(f"  [주가보정] {entity} {change:+.1f}% → conf+0.05")

    # ── 익스포저 없음 등급 강등 ──────────────────────────────────────
    # 당사직접 이슈(_force_urgent) 및 entity 없는 시장전체 이슈는 면제
    # 긴급 → 주의, 주의 → 참고 (한 단계씩)
    for a in result:
        entity_val = a.get("entity", "").strip()
        if a.get("_force_urgent"):
            continue                         # 당사직접 면제
        if not entity_val:
            continue                         # 시장전체 이슈 면제 (반대매매·서킷브레이커 등)
        if find_exposure(entity_val, exposure_data or {}):
            continue                         # 익스포저 있음 — 강등 없음
        # 익스포저 없음 → 한 단계 강등
        if a.get("grade") == "긴급":
            a["grade"] = "주의"
            a["customer_notice"] = None      # 긴급 전용 고객안내 제거
            print(f"  [익스포저없음 강등] 긴급→주의: {a['title'][:40]}")
        elif a.get("grade") == "주의":
            a["grade"] = "참고"
            print(f"  [익스포저없음 강등] 주의→참고: {a['title'][:40]}")
    # ─────────────────────────────────────────────────────────────────

    # 강등 후 등급 카운트 재산출
    urgent_cnt  = sum(1 for a in result if a.get("grade") == "긴급")
    caution_cnt = sum(1 for a in result if a.get("grade") == "주의")
    ref_cnt     = sum(1 for a in result if a.get("grade") == "참고")
    print(f"  등급 조정 완료 → 긴급 {urgent_cnt}건 / 주의 {caution_cnt}건 / 참고 {ref_cnt}건")

    # B안 — 동일 entity + 동일 grade 1건 제한 (리스크 점수 높은 것 유지)
    entity_grade_seen = {}
    result_deduped = []
    for a in sorted(result, key=lambda x: x.get("_risk_score") or 0, reverse=True):
        eg_key = (a.get("entity","").strip(), a.get("grade",""))
        if eg_key[0] and eg_key in entity_grade_seen:
            print(f"  [B안 dedup] 동일entity 동일등급 제거: [{a['grade']}] {a['title'][:35]}")
            continue
        if eg_key[0]:
            entity_grade_seen[eg_key] = True
        result_deduped.append(a)

    return result_deduped

def ai_filter_and_grade(articles: list, exposure_data: dict = None) -> list:
    """전체 기사를 50건씩 배치로 나눠 AI 필터링 후 중복 제거"""
    if not articles:
        return []
    result = []
    batch_size = 50
    ai_fail_count = 0
    MAX_AI_FAILS = 3
    for i in range(0, len(articles), batch_size):
        if ai_fail_count >= MAX_AI_FAILS:
            print(f"  ⚠️ AI 연속 {MAX_AI_FAILS}회 실패 — circuit breaker 작동, 필터링 중단")
            break
        batch = articles[i:i+batch_size]
        print(f"  배치 {i//batch_size+1}/{-(-len(articles)//batch_size)} 처리 중... ({len(batch)}건)")
        batch_result = ai_filter_batch(batch, offset=i)
        if not batch_result and batch:
            ai_fail_count += 1
            print(f"  배치 실패 ({ai_fail_count}/{MAX_AI_FAILS})")
        else:
            ai_fail_count = 0
            result.extend(batch_result)
        if i + batch_size < len(articles):
            time.sleep(1)

    if len(result) > 1:
        print(f"  중복 제거 중... (필터링 후 {len(result)}건)")
        result = dedup_deterministic(result)
        print(f"  dedup 후 {len(result)}건")

    result = regrade_by_score(result, exposure_data=exposure_data)

    return result

def build_exposure_html(entity, exposure_data: dict, ref_date: str, border_color: str = "#c0392b") -> str:
    """익스포저 현황 HTML
    종목유형 체계:
      주식     → 주식잔고   (빨강)
      해외주식 → 해외주식잔고 (빨강)
      채권     → 채권잔고   (보라)
      신용     → 여신잔고   (노랑)  ← 국내·해외 통합
      대출     → 여신잔고   (노랑)  ← 신용+대출 합산
      해외대출 → 여신잔고   (노랑)  ← 해외대출 포함
    표시 규칙:
      - 국내주식 있을 때: 여신잔고 연결 (신용+대출 합산, 없으면 없음 명시)
      - 해외주식 있을 때: 여신잔고 연결 (해외대출, 없으면 없음 명시)
      - rows 자체 없으면 → 잔고 없음
    entities 리스트 지원: 복수 종목 합산
    """
    # entities 복수 지원
    if isinstance(entity, list):
        entities_list = [e for e in entity if e]
    else:
        entities_list = [entity] if entity else []

    all_rows = []
    seen_row_keys = set()
    for ent in entities_list:
        for row in find_exposure(ent, exposure_data):
            row_key = (row.get("종목명",""), row.get("종목유형",""))
            if row_key not in seen_row_keys:
                seen_row_keys.add(row_key)
                all_rows.append(row)

    date_label = f"기준일: {ref_date}" if ref_date else ""

    # 3개 다 없으면 → 관련주 확인 후 없으면 잔고 없음
    if not all_rows:
        related_name = None
        related_rows = []
        for ent in entities_list:
            cand = RELATED_STOCK_MAP.get(ent)
            if cand:
                cand_rows = find_exposure(cand, exposure_data) if exposure_data else []
                if cand_rows:
                    related_name = cand
                    related_rows = cand_rows
                    break
        if related_name and related_rows:
            YEOSIN_L = {"신용", "대출", "해외대출"}
            BOND_L   = {"채권"}
            rs = [r for r in related_rows if r.get("종목유형","") not in YEOSIN_L and r.get("종목유형","") not in BOND_L]
            rl = [r for r in related_rows if r.get("종목유형","") in YEOSIN_L]
            rb = [r for r in related_rows if r.get("종목유형","") in BOND_L]
            def _rrow(r, rn):
                bal = float(str(r.get("잔고(억)","0")).replace(",",""))
                cus = int(float(str(r.get("고객수","0")).replace(",","")))
                return f'<div style="font-size:12px;color:#374151;line-height:1.8;">{rn} {bal:,.0f}억원 / {cus:,}명</div>'
            inner_r = ""
            if rs:
                inner_r += f'<div style="margin-bottom:4px;"><span style="font-size:9px;background:#dbeafe;color:#1d4ed8;padding:1px 6px;border-radius:2px;font-weight:700;">관련주·주식잔고</span> ' + "".join([_rrow(r, related_name) for r in rs]) + "</div>"
            if rl:
                inner_r += f'<div style="margin-bottom:4px;"><span style="font-size:9px;background:#fef3c7;color:#b45309;padding:1px 6px;border-radius:2px;font-weight:700;">관련주·여신잔고</span> ' + "".join([_rrow(r, related_name) for r in rl]) + "</div>"
            if rb:
                inner_r += f'<div style="margin-bottom:4px;"><span style="font-size:9px;background:#ede9fe;color:#5b21b6;padding:1px 6px;border-radius:2px;font-weight:700;">관련주·채권잔고</span> ' + "".join([_rrow(r, related_name) for r in rb]) + "</div>"
            return f'''<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;">
      <tr><td style="padding:10px 16px;">
        <p style="margin:0 0 6px 0;font-size:10px;font-weight:700;color:#1e293b;">뱅키스 익스포저
          <span style="font-weight:400;color:#94a3b8;">{date_label}</span></p>
        {inner_r}
      </td></tr>
    </table>'''
        return f'''<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;">
      <tr><td style="padding:10px 16px;">
        <p style="margin:0 0 4px 0;font-size:10px;font-weight:700;color:#1e293b;">뱅키스 익스포저
          <span style="font-weight:400;color:#94a3b8;">{date_label}</span></p>
        <div style="font-size:12px;color:#94a3b8;">잔고 없음</div>
      </td></tr>
    </table>'''

    # ── 종목유형 분류 ──────────────────────────────────────────────
    STOCK_TYPES     = {"주식"}
    OVERSEAS_TYPES  = {"해외주식"}
    BOND_TYPES      = {"채권"}
    YEOSIN_TYPES    = {"신용", "대출", "해외대출"}  # 여신 통합

    domestic_stock_rows = [r for r in all_rows if r.get("종목유형","") in STOCK_TYPES]
    overseas_stock_rows = [r for r in all_rows if r.get("종목유형","") in OVERSEAS_TYPES]
    bond_rows           = [r for r in all_rows if r.get("종목유형","") in BOND_TYPES]
    yeosin_rows         = [r for r in all_rows if r.get("종목유형","") in YEOSIN_TYPES]

    # 여신잔고 합산 — 종목명별 잔고·고객수 합계 (신용+대출+해외대출 통합)
    def _merge_yeosin(rows):
        merged = {}
        for r in rows:
            name = r.get("종목명","")
            bal = float(str(r.get("잔고(억)","0")).replace(",",""))
            cus = int(float(str(r.get("고객수","0")).replace(",","")))
            if name not in merged:
                merged[name] = {"잔고": 0, "고객수": 0}
            merged[name]["잔고"] += bal
            merged[name]["고객수"] += cus
        return merged  # {종목명: {잔고, 고객수}}

    def _fmt_row(r):
        잔고 = float(str(r.get("잔고(억)","0")).replace(",",""))
        고객 = int(float(str(r.get("고객수","0")).replace(",","")))
        return (
            f'<div style="font-size:12px;color:#1e293b;line-height:1.7;">'
            f'<span style="font-weight:700;">{r.get("종목명","")}</span>'
            f' {잔고:,.0f}억원 / {고객:,}명</div>'
        )

    def _fmt_merged(name, v):
        return (
            f'<div style="font-size:12px;color:#1e293b;line-height:1.7;">'
            f'<span style="font-weight:700;">{name}</span>'
            f' {v["잔고"]:,.0f}억원 / {v["고객수"]:,}명</div>'
        )

    NONE_HTML = '<div style="font-size:12px;color:#94a3b8;line-height:1.7;">잔고 없음</div>'

    def _section(label, bg, color, rows_html):
        return (
            f'<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:2px;">'
            f'<tr>'
            f'<td valign="top" style="padding-top:2px;width:56px;white-space:nowrap;">'
            f'<span style="font-size:9px;background:{bg};color:{color};padding:1px 5px;border-radius:2px;font-weight:700;">{label}</span>'
            f'</td>'
            f'<td style="padding-left:4px;">{rows_html}</td>'
            f'</tr></table>'
        )

    DIVIDER = (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0;">'
        '<tr><td style="height:1px;background:#e2e8f0;font-size:0;line-height:0;">&nbsp;</td></tr>'
        '</table>'
    )

    sections = []
    yeosin_merged = _merge_yeosin(yeosin_rows)
    yeosin_html = "".join([_fmt_merged(n, v) for n, v in yeosin_merged.items()]) if yeosin_merged else NONE_HTML

    # ── 국내주식 블록 ────────────────────────────────────────────
    if domestic_stock_rows:
        sections.append(_section("주식잔고", "#fee2e2", "#c0392b",
                                 "".join([_fmt_row(r) for r in domestic_stock_rows])))
        sections.append(_section("여신잔고", "#fef3c7", "#b45309", yeosin_html))

    # ── 해외주식 블록 ────────────────────────────────────────────
    if overseas_stock_rows:
        sections.append(_section("해외주식잔고", "#fee2e2", "#c0392b",
                                 "".join([_fmt_row(r) for r in overseas_stock_rows])))
        sections.append(_section("여신잔고", "#fef3c7", "#b45309", yeosin_html))

    # ── 채권 블록 ────────────────────────────────────────────────
    if bond_rows:
        sections.append(_section("채권잔고", "#ede9fe", "#5b21b6",
                                 "".join([_fmt_row(r) for r in bond_rows])))

    # ── 주식 없고 여신만 있는 경우 ───────────────────────────────
    if not domestic_stock_rows and not overseas_stock_rows and not bond_rows and yeosin_merged:
        sections.append(_section("여신잔고", "#fef3c7", "#b45309", yeosin_html))

    if not sections:
        inner = '<div style="font-size:12px;color:#94a3b8;">잔고 없음</div>'
    else:
        inner = DIVIDER.join(sections)

    return f'''<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;">
      <tr><td style="padding:10px 16px;">
        <p style="margin:0 0 8px 0;font-size:10px;font-weight:700;color:#1e293b;">뱅키스 익스포저
          <span style="font-weight:400;color:#94a3b8;">{date_label}</span></p>
        {inner}
      </td></tr>
    </table>'''

def _price_badge(a: dict) -> str:
    """해외주식 등락률 뱃지 HTML 반환"""
    chg = a.get("_price_change")
    if chg is None:
        return ""
    pct = str(round(abs(chg), 1))
    if chg <= -3:
        return f'<div style="font-size:10px;color:#2563eb;font-weight:700;margin-top:2px;">▼{pct}%</div>'
    if chg >= 3:
        return f'<div style="font-size:10px;color:#16a34a;font-weight:700;margin-top:2px;">▲{pct}%</div>'
    return ""


def build_email_html(articles: list, total_count: int = 0, ai_summary: str = '', exposure_data: dict = None, ref_date: str = '', competitor_notices: list = None, today_str: str = ''):
    exposure_data = exposure_data or {}
    now = datetime.now(timezone(timedelta(hours=9)))
    sections = {"긴급": [], "주의": [], "참고": []}
    for a in articles:
        sections[a["grade"]].append(a)

    GRADE_STYLE = {
        "긴급": {"header_bg":"#fafafa","border_left":"#ef4444","label_color":"#dc2626","card_bg":"#ffffff","card_border":"#fecaca"},
        "주의": {"header_bg":"#fafafa","border_left":"#f59e0b","label_color":"#b45309","card_bg":"#ffffff","card_border":"#fde68a"},
        "참고": {"header_bg":"#f8fafc","border_left":"#94a3b8","label_color":"#475569","card_bg":"#f8fafc","card_border":"#e2e8f0"},
    }
    rows = ""
    GRADE_LIMIT = {"긴급": 999, "주의": 5, "참고": 999}
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
              <span style="display:inline-block;width:20px;height:20px;line-height:20px;text-align:center;background:{gs["border_left"]};color:#fff;font-size:11px;font-weight:700;border-radius:50%;margin-left:6px;vertical-align:middle;">{len(items)}</span>
            </td>
            <td align="right" class="grade-header-right" style="padding:10px 14px;white-space:nowrap;">
              <span style="font-size:10px;{'background:#fee2e2;color:#c0392b;padding:2px 10px;border-radius:10px;font-weight:600;' if grade == '긴급' else 'color:#94a3b8;'}">{GRADE_DESC[grade]}</span>
            </td>
          </tr>
        </table>'''
        for a in display_items:
            # entities 복수 지원 — 없으면 entity 단수로 fallback
            a_entities = a.get("entities") or ([a.get("entity","")] if a.get("entity") else [])
            if grade == "참고":
                r_risk = a.get("_risk_score", "")
                if r_risk:
                    r_filled = min(int(r_risk), 10)
                    r_bar = "█" * r_filled + "░" * (10 - r_filled)
                    r_score_html = f'<div style="font-size:9px;color:#94a3b8;font-family:monospace;text-align:right;">{r_risk:.1f}<br>{r_bar}</div>'
                else:
                    r_score_html = ""
                rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" class="ref-bg" style="border:1px solid {gs["card_border"]};border-top:none;background:#f8fbff;">
          <tr>
            <td style="padding:6px 16px;font-size:12px;word-break:keep-all;color:#7a9abf;">
              · <a href="{_esc(a['url'])}" style="color:#7a9abf;text-decoration:none;">{_esc(a['title'][:45])}{"..." if len(a['title'])>45 else ""}</a>
              <span style="font-size:10px;color:#94a3b8;margin-left:4px;">{a.get("pub_str","").split("(")[0].strip() if a.get("pub_str") else ""}</span>
            </td>
            <td align="right" valign="middle" style="padding:6px 16px 6px 4px;white-space:nowrap;">{r_score_html}</td>
          </tr>
        </table>'''
            else:
                badges = ""
                if a.get("keyword"):
                    badges += f'<span style="display:inline-block;font-size:10px;color:#3b5491;background:#e8f0fe;padding:2px 7px;margin-right:4px;margin-bottom:6px;border-radius:3px;">{a["keyword"]}</span>'
                if a.get("entity") and a.get("entity") != a.get("keyword"):
                    badges += f'<span style="display:inline-block;font-size:10px;color:#7a9abf;background:#f1f5f9;padding:2px 7px;margin-right:4px;margin-bottom:6px;border-radius:3px;">{a["entity"]}</span>'
                badges += _price_badge(a)  # 등락률 뱃지 — 키워드 옆

                if grade == "주의":
                    c_exp_html = build_exposure_html(a_entities, exposure_data or {}, ref_date, border_color=gs["border_left"])
                    c_action_row = f'<tr><td style="padding:10px 16px;background:#fff0ee;border-top:1px solid {gs["card_border"]};border-bottom:1px solid {gs["card_border"]};"><p style="margin:0 0 3px 0;font-size:10px;font-weight:700;color:{gs["label_color"]};letter-spacing:0.5px;">대응방안</p><p style="margin:0;font-size:12px;color:#1e293b;line-height:1.6;font-weight:500;word-break:keep-all;">{_esc(a["action"])}</p></td></tr>' if a.get("action") else ""
                    c_exp_row   = f'<tr><td style="padding:0;">{c_exp_html}</td></tr>' if c_exp_html else ""
                    c_risk = a.get("_risk_score", "")
                    if c_risk:
                        c_filled = min(int(c_risk), 10)
                        c_bar = "█" * c_filled + "░" * (10 - c_filled)
                        c_score_html = (
                            f'<div style="text-align:right;min-width:90px;">'
                            f'<div style="font-size:9px;color:#94a3b8;margin-bottom:2px;">리스크 점수</div>'
                            f'<div style="font-size:13px;font-weight:700;color:#b45309;margin-bottom:2px;">{c_risk:.1f}<span style="font-size:9px;color:#94a3b8;font-weight:400;"> / 10</span></div>'
                            f'<div style="font-size:9px;color:#f59e0b;letter-spacing:1px;font-family:monospace;">{c_bar}</div>'
                            + f'</div>'
                        )
                    else:
                        c_score_html = ""
                    rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:{gs["card_bg"]};margin-bottom:10px;">
          <tr>
            <td style="padding:12px 16px;border-bottom:1px solid {gs["card_border"]};">
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:{f'6px' if badges else '0'};">
                <tr>
                  <td>{badges}</td>
                  <td align="right" valign="top">{c_score_html}</td>
                </tr>
              </table>
              <a href="{_esc(a['url'])}" class="title-link caution-title" style="font-weight:bold;font-size:14px;text-decoration:none;color:#1e293b;line-height:1.6;word-break:keep-all;display:block;">{_esc(a['title'])}</a>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 0 0;">
                <tr>
                  <td style="font-size:11px;"><a href="{_esc(a['url'])}" style="color:#4a6099;text-decoration:none;">↗ 기사 보기</a></td>
                  <td align="right" style="font-size:11px;color:#94a3b8;">{a.get("pub_str","")}</td>
                </tr>
              </table>
            </td>
          </tr>
          {c_action_row}{c_exp_row}
        </table>'''
                else:
                    exposure_html = build_exposure_html(a_entities, exposure_data or {}, ref_date)
                    if exposure_html and "잔고 없음" not in exposure_html:
                        a["_has_exposure"] = True
                    risk_score = a.get("_risk_score", "")
                    if risk_score:
                        filled = min(int(risk_score), 10)
                        empty  = 10 - filled
                        bar_str = "█" * filled + "░" * empty
                        risk_score_html = (
                            f'<div style="text-align:right;min-width:90px;">'
                            f'<div style="font-size:9px;color:#94a3b8;margin-bottom:2px;">리스크 점수</div>'
                            f'<div style="font-size:13px;font-weight:700;color:#c0392b;margin-bottom:2px;">{risk_score:.1f}<span style="font-size:9px;color:#94a3b8;font-weight:400;"> / 10</span></div>'
                            f'<div style="font-size:9px;color:#c0392b;letter-spacing:1px;font-family:monospace;">{bar_str}</div>'
                            + f'</div>'
                        )
                    else:
                        risk_score_html = ""
                    urgent_badges = ""
                    if a.get("keyword"):
                        urgent_badges += f'<span style="font-size:10px;background:#e8f0fe;color:#3b5491;padding:2px 7px;border-radius:3px;margin-right:4px;font-weight:600;">{a["keyword"]}</span>'
                    if a.get("entity") and a.get("entity") != a.get("keyword"):
                        urgent_badges += f'<span style="font-size:10px;background:#f1f5f9;color:#4a6099;padding:2px 7px;border-radius:3px;font-weight:600;">{a["entity"]}</span>'
                    urgent_badges += _price_badge(a)  # 등락률 뱃지 — 키워드 옆
                    action_row = f'<tr><td class="action-td" bgcolor="#fef2f2" style="padding:10px 16px;border-bottom:1px solid {gs["card_border"]};background:#fef2f2;"><p style="margin:0 0 3px 0;font-size:10px;font-weight:bold;color:{gs["label_color"]};letter-spacing:0.5px;">대응방안</p><p style="margin:0;font-size:12px;color:#1e293b;line-height:1.6;font-weight:600;word-break:keep-all;">{_esc(a["action"])}</p></td></tr>' if a.get("action") else ""
                    exposure_row = f'<tr><td style="padding:0;border-bottom:1px solid {gs["card_border"]};background:#ffffff;">{exposure_html}</td></tr>' if exposure_html else ""
                    notice_text = _esc((a["customer_notice"][:200] + "...") if a.get("customer_notice") and len(a["customer_notice"]) > 200 else a.get("customer_notice",""))
                    notice_row = f'<tr><td class="care-td" bgcolor="#f8fafc" style="padding:10px 16px;background:#f8fafc;border-top:1px solid #e2e8f0;"><p style="margin:0 0 5px 0;font-size:11px;font-weight:bold;letter-spacing:0.3px;"><span style="background:#2563eb;color:#fff;padding:2px 6px;font-size:10px;margin-right:5px;border-radius:3px;">✦ AI</span><span style="color:#334155;">고객케어 안내 추천 문구</span></p><p style="margin:0;font-size:12px;color:#334155;line-height:1.7;white-space:pre-line;word-break:keep-all;">{notice_text}</p></td></tr>' if a.get("customer_notice") else ""
                    bottom_box = f'<tr><td bgcolor="#fff8f8" style="background:#fff8f8;border-top:1px solid {gs["card_border"]};padding:0;"><table width="100%" cellpadding="0" cellspacing="0" border="0">{action_row}{exposure_row}{notice_row}</table></td></tr>' if (action_row or exposure_row or notice_row) else ""
                    is_last = (display_items.index(a) == len([x for x in display_items if x.get("grade")=="긴급"]) - 1 + sum(1 for x in display_items if x.get("grade")!="긴급"))
                    divider = "" if is_last else f'<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0;"><tr><td style="padding:0;height:1px;background:#ef4444;font-size:0;line-height:0;">&nbsp;</td></tr></table>'
                    rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:{gs["card_bg"]};margin-bottom:0;">
          <tr>
            <td class="card-bg" bgcolor="#fff8f8" style="padding:12px 16px;background:#fff8f8;border-bottom:1px solid #f5c6c6;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:8px;">
                <tr>
                  <td>{f"{urgent_badges}" if urgent_badges else ""}</td>
                  <td align="right" valign="top">{risk_score_html}</td>
                </tr>
              </table>
              <a href="{_esc(a['url'])}" class="title-link" style="font-weight:700;font-size:15px;text-decoration:none;color:#1e293b;line-height:1.6;word-break:keep-all;display:block;">{_esc(a['title'])}</a>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:5px 0 8px 0;">
                <tr>
                  <td style="font-size:12px;"><a href="{_esc(a['url'])}" style="color:#4a6099;text-decoration:none;font-weight:500;">↗ 기사 보기</a></td>
                  <td align="right" style="font-size:11px;color:#94a3b8;">{a.get("pub_str","")}</td>
                </tr>
              </table>
              {f'<p style="margin:0;font-size:11px;color:#94a3b8;line-height:1.6;word-break:keep-all;">{_esc(a["desc"])}</p>' if a.get("desc") else ""}
            </td>
          </tr>
          {bottom_box}
        </table>'''
        if extra_items:
            extra_rows = "".join([f'''
            <tr>
              <td style="padding:4px 0;font-size:13px;color:#4a6099;border-bottom:1px solid #f0f0f0;">
                <a href="{e['url']}" style="color:#4a6099;text-decoration:none;">{e['title'][:60]}{"..." if len(e['title']) > 60 else ""}</a>
                {f'<span style="font-size:11px;color:#94a3b8;margin-left:6px;">{e["pub_str"]}</span>' if e.get("pub_str") else ""}
              </td>
            </tr>''' for e in extra_items])
            rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:#fafafa;margin-bottom:10px;">
          <tr>
            <td style="padding:10px 16px 4px 16px;">
              <p style="margin:0 0 8px 0;font-size:12px;font-weight:bold;color:#7a9abf;">추가 {len(extra_items)}건</p>
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
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light">
<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->
<style>
  :root {{ color-scheme: light only; }}
  @media (prefers-color-scheme: dark) {{
    body, table, td, th, p, span, a, div {{ color-scheme: light only !important; }}
    .header-td  {{ background: #3b5491 !important; }}
    .card-bg    {{ background: #ffffff !important; color: #1e293b !important; }}
    .action-td  {{ background: #fef2f2 !important; }}
    .care-td    {{ background: #f8fafc !important; }}
    .ref-bg     {{ background: #f8fbff !important; }}
    a           {{ color: #4a6099 !important; }}
  }}
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
<body style="margin:0;padding:0;background:#f1f5f9;color-scheme:light only;font-family:'Apple SD Gothic Neo','Malgun Gothic',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f1f5f9;">
<tr><td align="center" class="outer" style="padding:16px;">
<table width="640" cellpadding="0" cellspacing="0" border="0" class="main" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">

  <!-- 헤더 H-3 -->
  <tr>
    <td class="header-td" style="background:#3b5491;padding:18px 26px 14px;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:12px;">
        <tr>
          <td valign="middle">
            <p style="margin:0 0 4px 0;font-size:18px;font-weight:bold;color:#ffffff;">🤖 eBiz본부 리스크 탐지봇</p>
            <p style="margin:0 0 3px 0;font-size:10px;color:#c8d8f0;text-align:right;">Powered by Claude AI</p>
            <p style="margin:0;font-size:12px;color:#c8d8f0;">{now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 (KST)</p>
          </td>
        </tr>
      </table>
      <div style="margin-bottom:12px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:4px;">
          <tr>
            <td style="font-size:11px;color:#c8d8f0;">수집 {total_count}건</td>
            <td align="right" style="font-size:11px;color:#6ee7b7;font-weight:600;">{len(articles)}건 선별 ({round((1 - len(articles)/max(total_count,1))*100)}% 필터링)</td>
          </tr>
        </table>
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#1e3370;border-radius:3px;overflow:hidden;">
          <tr>
            <td width="{max(1, round(len(sections["긴급"])/max(total_count,1)*100))}%" style="background:#ef4444;padding:3px 0;"></td>
            <td width="{max(1, round(len(sections["주의"])/max(total_count,1)*100))}%" style="background:#f59e0b;padding:3px 0;"></td>
            <td width="{max(1, round(len(sections["참고"])/max(total_count,1)*100))}%" style="background:#94a3b8;padding:3px 0;"></td>
            <td style="background:#1e3370;padding:3px 0;"></td>
          </tr>
        </table>
      </div>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#2d4278;">
        <tr>
          <td width="33%" align="center" style="padding:10px 0;border-right:1px solid #4a6099;">
            <p class="dash-num" style="margin:0 0 1px 0;font-size:26px;font-weight:bold;color:#ef4444;min-width:32px;display:block;">{len(sections['긴급'])}</p>
            <p style="margin:0 0 2px 0;font-size:12px;color:#c8d8f0;">긴급</p>
            <p style="margin:0;font-size:10px;color:#7a9abf;">당일 확인</p>
          </td>
          <td width="33%" align="center" style="padding:10px 0;border-right:1px solid #4a6099;">
            <p class="dash-num" style="margin:0 0 1px 0;font-size:26px;font-weight:bold;color:#f59e0b;min-width:32px;display:block;">{len(sections['주의'])}</p>
            <p style="margin:0 0 2px 0;font-size:12px;color:#c8d8f0;">주의</p>
            <p style="margin:0;font-size:10px;color:#7a9abf;">모니터링</p>
          </td>
          <td width="34%" align="center" style="padding:10px 0;">
            <p class="dash-num" style="margin:0 0 1px 0;font-size:26px;font-weight:bold;color:#94a3b8;min-width:32px;display:block;">{len(sections['참고'])}</p>
            <p style="margin:0 0 2px 0;font-size:12px;color:#c8d8f0;">참고</p>
            <p style="margin:0;font-size:10px;color:#7a9abf;">파악용</p>
          </td>
        </tr>
      </table>
      {f'<p style="margin:6px 0 0 0;font-size:11px;color:#c8d8f0;text-align:center;letter-spacing:0.2px;">💡 {_esc(ai_summary)}</p>' if ai_summary else ""}
    </td>
  </tr>

  {('<tr><td>' + build_competitor_html(competitor_notices or [], today_str) + '</td></tr>') if competitor_notices else ""}

  <tr><td class="rows-td" style="padding:0 18px 18px 18px;">{rows}</td></tr>

  <tr>
    <td class="footer-td" style="padding:14px 22px;background:#fff;border-top:1px solid #e2e8f0;">
      <p style="margin:0;font-size:12px;color:#94a3b8;line-height:2.0;">
        본 이메일은 네이버API로 수집한 뉴스를 Claude AI가 eBiz본부의 관점으로 리스크 분석하여 선별, 발송하였습니다.<br>
        담당자<br>
        &nbsp;&nbsp;(정) 최진후 차장&nbsp;&nbsp;&nbsp;(부) 이원세 대리 · 장인호 대리
      </p>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:12px;border-top:1px solid #e2e8f0;padding-top:10px;">
        <tr>
          <td style="font-size:11px;font-weight:700;color:#4a6099;padding-bottom:6px;" colspan="2">리스크 점수 산정 기준 (10점 만점)</td>
        </tr>
        <tr>
          <td style="font-size:10px;padding:2px 0;color:#c0392b;font-weight:600;width:80px;">8.0 ~ 10.0</td>
          <td style="font-size:10px;padding:2px 0;color:#7a9abf;">당사 직접 언급 · MTS 장애 · 시스템 사고</td>
        </tr>
        <tr>
          <td style="font-size:10px;padding:2px 0;color:#c0392b;font-weight:600;">6.5 ~ 8.0</td>
          <td style="font-size:10px;padding:2px 0;color:#7a9abf;">상장폐지 · 파산 · 부도 확정</td>
        </tr>
        <tr>
          <td style="font-size:10px;padding:2px 0;color:#b7791f;font-weight:600;">5.0 ~ 6.5</td>
          <td style="font-size:10px;padding:2px 0;color:#7a9abf;">기업회생 · 반대매매 실제 발생</td>
        </tr>
        <tr>
          <td style="font-size:10px;padding:2px 0;color:#7a9abf;">~ 5.0</td>
          <td style="font-size:10px;padding:2px 0;color:#7a9abf;">워크아웃 · 참고 동향</td>
        </tr>
        <tr>
          <td style="font-size:10px;padding:6px 0 0 0;color:#94a3b8;" colspan="2">점수 = AI 확신도 × 리스크 유형 가중치 + 당사 익스포저 보정 (×5 환산)</td>
        </tr>
      </table>
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
<body style="margin:0;padding:0;background:#f1f5f9;color-scheme:light only;font-family:'Apple SD Gothic Neo','Malgun Gothic',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f1f5f9;">
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
      <p style="margin:0;font-size:17px;color:#7a9abf;line-height:1.8;">AI 리스크 탐지 결과<br>해당하는 뉴스가 없습니다.</p>
    </td>
  </tr>
  <tr>
    <td style="padding:14px 22px;border-top:1px solid #e2e8f0;">
      <p style="margin:0;font-size:12px;color:#94a3b8;line-height:2.0;">
        본 이메일은 네이버API로 수집한 뉴스를 Claude AI가 eBiz본부의 관점으로 리스크 분석하여 선별, 발송하였습니다.<br>
        담당자<br>
        &nbsp;&nbsp;(정) 최진후 차장&nbsp;&nbsp;&nbsp;(부) 이원세 대리 · 장인호 대리
      </p>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:12px;border-top:1px solid #e2e8f0;padding-top:10px;">
        <tr>
          <td style="font-size:11px;font-weight:700;color:#4a6099;padding-bottom:6px;" colspan="2">리스크 점수 산정 기준 (10점 만점)</td>
        </tr>
        <tr>
          <td style="font-size:10px;padding:2px 0;color:#c0392b;font-weight:600;width:80px;">8.0 ~ 10.0</td>
          <td style="font-size:10px;padding:2px 0;color:#7a9abf;">당사 직접 언급 · MTS 장애 · 시스템 사고</td>
        </tr>
        <tr>
          <td style="font-size:10px;padding:2px 0;color:#c0392b;font-weight:600;">6.5 ~ 8.0</td>
          <td style="font-size:10px;padding:2px 0;color:#7a9abf;">상장폐지 · 파산 · 부도 확정</td>
        </tr>
        <tr>
          <td style="font-size:10px;padding:2px 0;color:#b7791f;font-weight:600;">5.0 ~ 6.5</td>
          <td style="font-size:10px;padding:2px 0;color:#7a9abf;">기업회생 · 반대매매 실제 발생</td>
        </tr>
        <tr>
          <td style="font-size:10px;padding:2px 0;color:#7a9abf;">~ 5.0</td>
          <td style="font-size:10px;padding:2px 0;color:#7a9abf;">워크아웃 · 참고 동향</td>
        </tr>
        <tr>
          <td style="font-size:10px;padding:6px 0 0 0;color:#94a3b8;" colspan="2">점수 = AI 확신도 × 리스크 유형 가중치 + 당사 익스포저 보정 (×5 환산)</td>
        </tr>
      </table>
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

    try:
        log_files = sorted(
            [f for f in os.listdir(".") if f.startswith("filter_log_") and f.endswith(".json")]
        )
        for old_f in log_files[:-30]:
            os.remove(old_f)
    except Exception:
        pass

    sent_titles          = {a.get("title","") for a in final_sent}
    hard_excl_map        = {a.get("title",""): a.get("_excl_reason","") for a in hard_excluded}
    ai_filtered_titles   = {a.get("title","") for a in ai_filtered}
    ai_conf_map          = {a.get("title",""): a.get("_ai_confidence") for a in ai_filtered}

    all_articles = raw_articles + hard_excluded

    logs = []
    for a in all_articles:
        title = a.get("title","")
        h     = hashlib.sha256(title.encode()).hexdigest()[:8]

        if title in hard_excl_map:
            decision   = "HARD_EXCLUDED"
            reason     = hard_excl_map[title]
            confidence = None
        elif title not in ai_filtered_titles:
            decision   = "AI_EXCLUDED"
            reason     = "AI 필터링 제외"
            confidence = ai_conf_map.get(title)
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
            "reason"    : reason,
            "confidence": confidence,
        })

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
                "excl_stats" : dict(excl_stats),
                "logs"       : logs,
            }, f, ensure_ascii=False, indent=2)
        print(f"  필터링 로그 저장: {log_path} (하드제외 {len(hard_excluded)}건 / 발송 {len(final_sent)}건)")
        if excl_stats:
            top3 = excl_stats.most_common(3)
            top3_str = " | ".join([f"{k}:{v}건" for k, v in top3])
            print(f"  제외 사유 Top3: {top3_str}")
    except Exception as e:
        print(f"  로그 저장 실패: {e}")

def send_email_error(error_msg: str, trace: str):
    """런타임 오류 발생 시 담당자에게 오류 내용 메일 발송"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    now_str = now.strftime("%Y년 %m월 %d일 %H:%M")
    receiver = NO_RESULT_RECEIVER if NO_RESULT_RECEIVER else EMAIL_SENDER

    html_body = f"""<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Apple SD Gothic Neo','Malgun Gothic',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f1f5f9;">
<tr><td align="center" style="padding:16px;">
<table width="640" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">
  <tr>
    <td style="background:#7f1d1d;padding:20px 26px;">
      <p style="margin:0 0 4px 0;font-size:18px;font-weight:bold;color:#ffffff;">🚨 eBiz본부 리스크 탐지봇 — 런타임 오류</p>
      <p style="margin:0;font-size:12px;color:#fca5a5;">{now_str} 기준 (KST)</p>
    </td>
  </tr>
  <tr>
    <td style="padding:20px 26px;">
      <p style="margin:0 0 8px 0;font-size:13px;font-weight:700;color:#1e293b;">오류 내용</p>
      <p style="margin:0 0 16px 0;font-size:13px;color:#dc2626;background:#fef2f2;padding:10px 14px;border-left:4px solid #dc2626;word-break:break-all;">{_esc(str(error_msg))}</p>
      <p style="margin:0 0 8px 0;font-size:13px;font-weight:700;color:#1e293b;">스택 트레이스</p>
      <pre style="margin:0;font-size:11px;color:#475569;background:#f8fafc;padding:12px 14px;overflow-x:auto;white-space:pre-wrap;word-break:break-all;border:1px solid #e2e8f0;">{_esc(trace[-2000:])}</pre>
    </td>
  </tr>
  <tr>
    <td style="padding:14px 26px;border-top:1px solid #e2e8f0;">
      <p style="margin:0;font-size:12px;color:#94a3b8;">GitHub Actions 워크플로우 로그에서 상세 내용을 확인하시기 바랍니다.<br>담당자: (정) 최진후 차장</p>
    </td>
  </tr>
</table>
</td></tr>
</table>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[리스크봇 오류] {now_str} 기준 — 런타임 오류 발생"
    msg["From"]    = f"🚨eBiz 리스크봇 <{EMAIL_SENDER}>"
    msg["To"]      = receiver
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.ehlo()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, [receiver], msg.as_string())
        print(f"  오류 메일 발송 완료 → {receiver}")
    except Exception as e:
        print(f"  오류 메일 발송 실패: {e}")

def send_email_no_result(subject: str, html_body: str):
    """결과 없을 때 특정인(NO_RESULT_RECEIVER)에게만 발송"""
    receiver = NO_RESULT_RECEIVER if NO_RESULT_RECEIVER else EMAIL_SENDER
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"eBiz 리스크봇 <{EMAIL_SENDER}>"
    msg["To"]      = receiver
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.ehlo()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, [receiver], msg.as_string())
        print(f"  결과없음 메일 발송 완료 → {receiver}")
    except smtplib.SMTPAuthenticationError as e:
        print(f"  결과없음 메일 인증 실패 (앱 비밀번호 확인 필요): {e}")
    except smtplib.SMTPException as e:
        print(f"  결과없음 메일 발송 실패 (SMTP): {e}")
    except Exception as e:
        print(f"  결과없음 메일 발송 실패: {e}")

def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"eBiz 리스크봇 <{EMAIL_SENDER}>"
    msg["To"]      = ", ".join(EMAIL_RECEIVERS)
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.ehlo()
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                server.sendmail(EMAIL_SENDER, EMAIL_RECEIVERS, msg.as_string())
            print("이메일 발송 완료")
            return
        except smtplib.SMTPAuthenticationError as e:
            print(f"이메일 인증 실패 (비밀번호/앱 비밀번호 확인 필요): {e}")
            raise
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
    now_kst         = datetime.now(timezone(timedelta(hours=9)))
    now_str_full    = now_kst.strftime("%m월 %d일 %H시 %M분")
    seen_urls       = load_seen_urls()
    seen_combos     = load_seen_combos()
    seen_context    = load_seen_context()
    sent_urls = set()
    new_combos_this_run = set()
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

    # 익스포저 데이터 선로드 — 해외 동적 키워드 생성 + AI 필터링에 공통 사용
    exposure_data = load_exposure_data()
    # 해외주식 동적 키워드 생성 — 익스포저 CSV 상위 30개 종목
    overseas_kws = get_overseas_keywords(exposure_data, top_n=30)
    all_keywords = KEYWORDS + overseas_kws
    if overseas_kws:
        print(f"  해외주식 동적 키워드 {len(overseas_kws)}개 추가: {overseas_kws[:6]}...")

    print(f"  키워드 {len(all_keywords)}개 병렬 크롤링 중...")
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(crawl_keyword, kw): kw for kw in all_keywords}
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
        subject = f"🚨 [리스크 탐지] {now_str_full} 기준 — 신규 뉴스 없음"
        send_email_no_result(subject, build_empty_html(now))
        save_seen_urls(set())
        return

    before_hard = len(raw_articles)
    hard_excluded_articles = []
    raw_articles_kept      = []
    for _a in raw_articles:
        _excl, _reason = is_hard_excluded(_a.get("title",""), _a.get("desc",""))
        if _excl:
            _a["_excl_reason"] = _reason
            hard_excluded_articles.append(_a)
        else:
            raw_articles_kept.append(_a)
    raw_articles = raw_articles_kept
    if before_hard != len(raw_articles):
        print(f"  하드 제외룰: {before_hard}건 → {len(raw_articles)}건 ({before_hard - len(raw_articles)}건 제거)")

    print(f"\nAI 필터링 중... (총 {len(raw_articles)}건)")
    filtered = ai_filter_and_grade(raw_articles, exposure_data=exposure_data)
    ai_filtered_articles = list(filtered)
    for _a in filtered:
        if find_exposure(_a.get("entity",""), exposure_data):
            _a["_has_exposure"] = True

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
    prev_title_norms = seen_context.get("title_norms", [])
    prev_desc_norms  = seen_context.get("desc_norms",  [])
    new_title_norms  = []
    new_desc_norms   = []

    for a in filtered:
        entity   = a.get("entity", "").strip()
        keyword  = a.get("keyword", "").strip()
        event_type = a.get("event_type", "").strip()
        combo    = (entity, event_type) if entity and event_type else \
                   (entity, keyword) if entity and keyword else None
        kw_only  = ("", keyword) if keyword else None
        t_norm   = _norm(a.get("title", ""))
        d_norm   = _norm(a.get("desc",  ""))
        matched  = False
        reason   = ""

        if combo and combo in seen_combos:
            if is_next_stage(a.get("title",""), a.get("desc","")):
                pass
            else:
                matched = True; reason = "동일 사건(entity+kw) 이미 발송"

        if not matched and not entity and kw_only and kw_only in seen_combos:
            matched = True; reason = "동일 키워드 이미 발송"

        if not matched and t_norm and not is_next_stage(a.get("title",""), a.get("desc","")):
            for prev_t in prev_title_norms:
                if _sim(t_norm, prev_t) >= TITLE_SIM_THRESHOLD - 0.02:
                    matched = True; reason = "이전 실행 발송 기사와 제목 유사"
                    break

        if not matched and d_norm and len(d_norm) > 20 and not is_next_stage(a.get("title",""), a.get("desc","")):
            for prev_d in prev_desc_norms:
                if _sim(d_norm, prev_d) >= DESC_SIM_THRESHOLD:
                    matched = True; reason = "이전 실행 발송 기사와 내용 유사"
                    break

        if matched:
            print(f"  [{a['grade']}] '{a['title'][:30]}' — {reason}, 스킵")
            continue

        # 동일 실행 내 동일 entity+event_type 이미 상위 등급 발송 시 하위 등급 차단
        # 예: 금양 상폐 긴급 이미 있으면 금양 상폐 주의·참고 차단
        GRADE_ORDER = {"긴급": 0, "주의": 1, "참고": 2}
        ev_key = (entity, event_type) if entity and event_type else None
        if ev_key:
            existing_grades = [GRADE_ORDER[x["grade"]] for x in filtered_final
                               if x.get("entity") == entity and x.get("event_type") == event_type]
            if existing_grades and GRADE_ORDER.get(a["grade"], 9) > min(existing_grades):
                print(f"  [{a['grade']}] '{a['title'][:30]}' — 동일 사건 상위등급 이미 발송, 스킵")
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
        subject = f"🚨 [리스크 탐지] {now_str_full} 기준 — 해당 뉴스 없음"
        send_email_no_result(subject, build_empty_html(now))
        save_seen_urls(set())
        return

    print("  본문 크롤링 중... (긴급·주의만)")
    def crawl_body(article):
        if article.get("grade") == "참고":
            article["body"] = ""
            return article
        body = fetch_article_body(article["url"])
        if body:
            article["body"] = body
            article["_body_failed"] = False
        else:
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

    print("  대응방안·고객안내 생성 중... (긴급만)")

    def generate_action_and_notice(article):
        if article.get("grade") != "긴급":
            return
        _body_failed = article.get("_body_failed", False)
        if _body_failed:
            print(f"  본문 크롤링 실패 — 제목·요약 기반으로 action 생성: {article.get('title','')[:30]}")
        body_text = article.get("body", "") or article.get("desc", "")
        entity    = article.get("entity", "")
        keyword   = article.get("keyword", "")
        exp_rows  = find_exposure(entity, exposure_data)
        # 해외주식 여부 — 익스포저 rows의 시장 컬럼 또는 keyword 패턴으로 판단
        is_overseas = any(r.get("시장","국내") == "해외" or r.get("종목유형","") in ("해외주식","해외대출") for r in exp_rows)
        def _fmt_exp(r):
            잔고 = float(str(r.get('잔고(억)', '0')).replace(',', ''))
            고객 = int(float(str(r.get('고객수', '0')).replace(',', '')))
            return f"{r.get('종목유형','')} {잔고:,.0f}억원/{고객:,}명"
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
                    "messages": [{"role": "user", "content": (
                        open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "action_prompt.txt"), encoding="utf-8").read()
                        .replace("__KW__", keyword)
                        .replace("__ENTITY__", entity)
                        .replace("__GRADE__", article.get("grade",""))
                        .replace("__TITLE__", article.get("title",""))
                        .replace("__BODY__", body_text[:400])
                        .replace("__EXP__", exp_str)
                        .replace("__EXP_LINE__", f"- eBiz 익스포저: {exp_str}" if exp_str else "")
                        .replace("__OVERSEAS__", "해외주식 (신용융자 불가, 담보대출만 가능)" if is_overseas else "국내주식")
                        .replace("__BODY_FAIL__", " (※ 본문 크롤링 실패 — 제목·요약 기반만 사용, 추측 금지)" if article.get("_body_failed") else "")
                    )}],
                },
                timeout=20,
            )
            if res.status_code == 429:
                print(f"  대응방안 Rate limit 429 — 스킵: {article.get('title','')[:20]}")
                return
            res.raise_for_status()
            payload = res.json()
            content = payload.get("content", [])
            raw = content[0].get("text", "").strip() if content else ""
            if not raw:
                return
            raw = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            if result.get("action"):
                action_text = result["action"]
                if _body_failed:
                    action_text += " *(본문 크롤링 실패, 제목 기반 생성)"
                article["action"] = action_text
            if result.get("customer_notice"):
                notice_text = result["customer_notice"]
                if _body_failed:
                    notice_text += "\n*(본문 크롤링 실패, 제목 기반 생성)"
                article["customer_notice"] = notice_text
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
    competitor_notices = load_competitor_notices()
    if competitor_notices:
        print(f"  경쟁사 신용·대출 특이사항 {len(competitor_notices)}건 발견")
    else:
        print("  경쟁사 신용·대출 특이사항 없음")
    if exposure_data:
        ref_date = next(iter(exposure_data.values()))[0].get("기준일", "")
        print(f"  익스포저 데이터 로드 완료 ({len(exposure_data)}건, 기준일: {ref_date})")
    else:
        ref_date = ""
        print("  익스포저 데이터 없음 — CSV 파일 미확인")

    subject = f"🚨 [리스크 탐지] {now_str_full} 기준"
    total_count = len(raw_articles) + len(hard_excluded_articles)

    urgent_cnt = len([a for a in filtered if a["grade"]=="긴급"])
    caution_cnt = len([a for a in filtered if a["grade"]=="주의"])
    ref_cnt = len([a for a in filtered if a["grade"]=="참고"])
    filtered_titles = f"[등급 분포] 긴급 {urgent_cnt}건 / 주의 {caution_cnt}건 / 참고 {ref_cnt}건\n\n" + "\n".join([f"- [{a['grade']}] {a['title']}" for a in filtered])
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
                "max_tokens": 80,
                "messages": [{"role": "user", "content": f"아래 오늘의 리스크 기사 목록을 보고, 오늘의 리스크 흐름을 30자 이내 한 문장으로만 작성하세요.\n문장 외 다른 내용 일절 금지. 예: '삼부토건 상폐 심의·홈플러스 회생 갈림길 동시 부각'\n\n{filtered_titles}"}],
            },
            timeout=15,
        )
        _sum_payload = sum_res.json()
        _sum_content = _sum_payload.get("content", [])
        _sum_text = next((b.get("text","") for b in _sum_content if b.get("type")=="text"), "")
        ai_summary = _sum_text.strip()
    except Exception:
        ai_summary = ""

    html = build_email_html(filtered, total_count=total_count, ai_summary=ai_summary, exposure_data=exposure_data, ref_date=ref_date, competitor_notices=competitor_notices, today_str=today_str)
    send_email(subject, html)

    for a in filtered:
        sent_urls.add(a.get("url", ""))
        entity     = a.get("entity", "").strip()
        keyword    = a.get("keyword", "").strip()
        event_type = a.get("event_type", "").strip()
        if event_type and entity:
            new_combos_this_run.add((entity, event_type))
        elif keyword and entity:
            new_combos_this_run.add((entity, keyword))
    save_seen_urls(sent_urls, new_combos_this_run,
                   title_norms=new_title_norms, desc_norms=new_desc_norms)
    save_filter_log(raw_articles, hard_excluded_articles,
                    ai_filtered_articles, filtered)

if __name__ == "__main__":
    import traceback as _tb
    try:
        main()
    except Exception as _e:
        _trace = _tb.format_exc()
        print(f"런타임 오류 발생:\n{_trace}")
        try:
            send_email_error(_e, _trace)
        except Exception as _me:
            print(f"오류 메일 발송 실패: {_me}")
        raise
