ENTITY_ALIAS_MAP = {
    # AI가 영문/약어로 추출하는 경우 → CSV 종목명 한글 표기 매핑 (검증된 매핑, 최우선 적용)
    "JTBC": "제이티비씨",
    "CJ": "씨제이",
    "LG": "엘지",
    "SK": "에스케이",
    "GS": "지에스",
    "DB": "디비",
    "KB": "케이비",
    "KT": "케이티",
    "HD": "에이치디",
    "HL": "에이치엘",
}

# 그룹 계열사 강제 매핑 — AI가 entity 하나만 추출해도 코드에서 계열사 익스포저 추가 조회
# key: AI가 추출하는 entity명 (별칭 포함), value: 함께 조회할 종목명 리스트
GROUP_ENTITIES_MAP = {
    # 중앙그룹
    "JTBC":      ["제이티비씨", "중앙일보", "콘텐트리중앙", "에스엘엘중앙", "중앙홀딩스", "메가박스중앙"],
    "제이티비씨": ["중앙일보", "콘텐트리중앙", "에스엘엘중앙", "중앙홀딩스", "메가박스중앙"],
    "중앙홀딩스": ["제이티비씨", "중앙일보", "콘텐트리중앙", "에스엘엘중앙", "메가박스중앙"],
    "콘텐트리중앙": ["제이티비씨", "중앙일보", "에스엘엘중앙", "중앙홀딩스"],
    "에스엘엘중앙": ["제이티비씨", "중앙일보", "콘텐트리중앙", "중앙홀딩스"],
    "중앙일보":   ["제이티비씨", "콘텐트리중앙", "에스엘엘중앙", "중앙홀딩스"],
}

# group_map.json은 import os/json 이후 로드 (아래 _load_group_map() 참조)

# 알파벳 → 한글 음역 (ENTITY_ALIAS_MAP에 없는 새 약어용 fallback)
ALPHA_TO_KOREAN = {
    'A':'에이','B':'비','C':'씨','D':'디','E':'이','F':'에프','G':'지',
    'H':'에이치','I':'아이','J':'제이','K':'케이','L':'엘','M':'엠',
    'N':'엔','O':'오','P':'피','Q':'큐','R':'알','S':'에스','T':'티',
    'U':'유','V':'브이','W':'더블유','X':'엑스','Y':'와이','Z':'제트'
}

def _alpha_to_korean(text: str) -> str:
    """영문 약어를 한글 음역으로 변환 (예: JTBC → 제이티비씨)
    영문이 아닌 문자(한글/숫자 등)는 그대로 유지
    """
    result = []
    for ch in text.upper():
        if ch in ALPHA_TO_KOREAN:
            result.append(ALPHA_TO_KOREAN[ch])
        else:
            result.append(ch)
    return "".join(result)

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
from html import escape as _html_escape


def _esc(v) -> str:
    """HTML 이스케이프 — None·숫자 등 비문자열 입력도 안전하게 처리.

    기존엔 html.escape를 그대로 사용해, AI 응답에서 url·entity 등이 null로
    오면 이메일 생성 전체가 AttributeError로 죽었다(2026-07-24 전수점검 발견).
    발송 직전 단계에서 죽으면 그 회차 알림이 통째로 유실되므로 방어한다."""
    if v is None:
        return ""
    if not isinstance(v, str):
        v = str(v)
    return _html_escape(v)
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
from email.utils import formataddr
from email.utils import formatdate, make_msgid
from email.header import Header

# group_map.json (DART 자동 매핑) 로드 — GROUP_ENTITIES_MAP에 병합
# group_mapper.py가 생성. 없어도 GROUP_ENTITIES_MAP fallback으로 동작
_GROUP_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "group_map.json")
if os.path.exists(_GROUP_MAP_FILE):
    try:
        with open(_GROUP_MAP_FILE, encoding="utf-8") as _f:
            _dart_map = json.load(_f)
        for _entity, _members in _dart_map.items():
            if _entity not in GROUP_ENTITIES_MAP:
                GROUP_ENTITIES_MAP[_entity] = _members
            else:
                _existing = set(GROUP_ENTITIES_MAP[_entity])
                GROUP_ENTITIES_MAP[_entity] = list(_existing | set(_members))
        print(f"  [group_map] DART 매핑 로드: {len(_dart_map)}개 종목")
    except Exception as _e:
        print(f"  [group_map] 로드 실패 (무시): {_e}")

# ─────────────────────────────────────────────
# 설정 — GitHub Secrets에서 자동으로 읽어옴
# ─────────────────────────────────────────────
EMAIL_SENDER      = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]
EMAIL_RECEIVERS   = [e.strip() for e in os.environ["EMAIL_RECEIVER"].split(",") if e.strip()]
NO_RESULT_RECEIVER = os.environ.get("NO_RESULT_RECEIVER", "").strip()  # 결과 없을 때 수신자
EMAIL_CC          = [e.strip() for e in os.environ.get("EMAIL_CC", "").split(",") if e.strip()]   # 참조
# 전체 발송 임계값 — 최종 기사 중 최고 리스크점수가 이 값 미만이면
# 전체 수신자 대신 보낸사람(NO_RESULT_RECEIVER/SENDER)에게만 발송.
# "실제 리스크 있는 메일만 전체 발송" 목적.
# 2026-07-25: 5.0 → 5.5 상향. 최근 9회 발송 실적 재현 결과 전체발송이
# 9/9(100%)로 과다했고, 임원 대상이므로 리스크가 더 높은 건만 보내도록 조정.
try:
    SELF_ONLY_MAX_SCORE = float(os.environ.get("SELF_ONLY_MAX_SCORE", "5.5"))
except ValueError:
    SELF_ONLY_MAX_SCORE = 5.5

# 고신뢰 주의(conf≥0.80) 우회 발송의 최소 익스포저 규모(억).
# 점수가 임계 미만이어도 AI 확신도가 높으면 전체 발송하는 우회 경로가 있는데,
# 기존엔 '익스포저 실재(>0)'만 봐서 2억짜리 종목도 전사 발송을 유발했음
# (7/25 21시 예선테크 주식 2억·여신 없음 실사례).
# 점수 5.5 이상은 이 값과 무관하게 전체 발송되므로, 소규모 종목의 진짜
# 리스크(예: 엔비티 담보비율 하회 5.5점·19억)는 계속 커버된다.
try:
    STRONG_CAUTION_MIN_EXPOSURE = float(os.environ.get("STRONG_CAUTION_MIN_EXPOSURE", "50"))
except ValueError:
    STRONG_CAUTION_MIN_EXPOSURE = 50.0

# 전체 발송 메일에 실을 '참고' 등급의 최소 익스포저 규모(억).
# 최근 9회 발송 실측: 오탐 20건 중 18건(90%)이 참고 등급이었고, 참고 자체의
# 오탐률은 78%(23건 중 18건). 오탐은 임원 신뢰도에 직결되므로 전체 발송 시
# 참고는 '당사 익스포저가 매우 큰 종목'으로 한정한다.
# 3,000억 기준 실측: 참고 정탐 3건(삼성전자·SK하이닉스·마이크론 급락) 전부
# 보존하면서 오탐 노출 0건. 본인 한정 발송에는 참고를 전부 유지해
# 담당자 모니터링에는 공백이 없다.
try:
    REF_FULLSEND_MIN_EXPOSURE = float(os.environ.get("REF_FULLSEND_MIN_EXPOSURE", "3000"))
except ValueError:
    REF_FULLSEND_MIN_EXPOSURE = 3000.0

# 시장급락 강제발송 기준 — 종목 수와 '위험고객 리스크잔고 규모'를 함께 본다.
# 기존엔 -3%↓ 종목 수(10개)만 봤는데, 위험고객 보유 종목이 305개나 되고
# 리스크잔고 중앙값이 0억이라(상위 5종목이 전체 785억의 54% 차지) 평범한
# 조정장에도 소액 종목 10개가 쉽게 넘어 뉴스 품질과 무관하게 매번 전사
# 발송됐음(7/27 14시 10종목·21시 14종목 연속 발동).
try:
    MARKET_CRASH_STOCK_THRESHOLD = int(os.environ.get("MARKET_CRASH_STOCK_THRESHOLD", "15"))
except ValueError:
    MARKET_CRASH_STOCK_THRESHOLD = 15
try:
    MARKET_CRASH_RBAL_THRESHOLD = float(os.environ.get("MARKET_CRASH_RBAL_THRESHOLD", "150"))
except ValueError:
    MARKET_CRASH_RBAL_THRESHOLD = 150.0
ANTHROPIC_KEY     = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_API_KEY    = os.environ.get("GOOGLE_API_KEY", "")       # Gemini 필터링용 (없으면 Claude fallback)
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

KEYWORDS = ["부실 리스크", "신용 리스크", "유동성 리스크", "디폴트 리스크", "기업회생", "상장폐지", "파산", "워크아웃", "부도", "거래정지", "반대매매 급증", "신용등급 강등", "PF 부실", "미매각", "발행어음", "감사의견", "관리종목", "횡령 배임", "한국투자증권오류", "한국투자증권 장애", "한국투자증권 접속불가", "한국투자증권 지연", "한국투자증권 미지급", "한국투자증권 이슈",
            # ── 신용공여 리스크 중간단계 (2026-07-28 추가) ──
            # 리스크 심사역 관점 점검에서 확인된 사각지대.
            # 실무 발생 순서: 주가하락 → 담보비율 하락 → [마진콜·담보부족] →
            # [추가담보 미납] → 반대매매 → 손실확정.
            # 기존엔 양 끝단(가격경보·반대매매)만 있고 중간이 통째로 비어 있어,
            # 이 표현만 쓰인 기사는 AI가 볼 기회조차 없었다.
            "마진콜", "담보부족", "추가담보", "담보비율 하회", "깡통계좌"]
MAX_NEWS_PER_KEYWORD = 300   # 네이버 API 페이지 제한 (100건×3페이지)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]
SEEN_FILE = "seen_news.json"
# dedup 보존기간 — 2026-07-24: 7일(168h) → 3일(72h)로 단축.
# 사유: 코오롱티슈진 임상실패 후 연속 하한가 국면에서 7/23 07시 1회 발송 뒤
# 동일 조합(entity_기타리스크)으로 7일간 전 후속기사가 차단돼, 정작 손실이
# 확대되는 시점에 봇이 침묵한 실사례. 3일로 줄여 재조명 주기를 짧게 한다.
SEEN_RETENTION_HOURS = 72
# 가격 연동 재발송 임계값 — 이미 발송한 사건이라도 당일 이 % 이상 추가
# 하락하면 dedup을 무시하고 재발송한다(연속 하한가 국면 침묵 방지).
PRICE_RESEND_THRESHOLD = -8.0
EXPOSURE_FILE = "exposure_data.csv"
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL",        "claude-sonnet-4-6")  # Gemini fallback·재검증용
CLAUDE_ACTION_MODEL = os.environ.get("CLAUDE_ACTION_MODEL", "claude-sonnet-4-6")  # action 생성 전용
# 전체 발송이 예상될 때 2차 본문검증에 쓰는 상위 모델.
# 임원 전사 발송은 오탐 비용이 가장 크므로 마지막 관문만 승급한다.
# ★단계를 늘리지 않고 '모델만 교체'하는 이유: 검증 단계를 추가하면 단계 간
#   순서·덮어쓰기 문제가 생긴다(2026-07-28 급락장 미발송·KB증권 등급 복원·
#   긴급 4건 초과 사고가 모두 그 유형이었다).
CLAUDE_VERIFY_HIGH_MODEL = os.environ.get("CLAUDE_VERIFY_HIGH_MODEL", "claude-opus-4-6")

# 실환경 테스트용 안전 스위치 — 전체 파이프라인은 그대로 돌리되 '발송만'
# 본인 한정으로 강제한다. 예비 발송범위 판정은 정상 수행되므로 2차 검증
# 모델 승급(Opus)·가격경보 캐싱·운영지표 기록까지 실제와 동일하게 검증된다.
# 기본값 꺼짐. 워크플로우에서 명시적으로 켤 때만 동작한다.
FORCE_SELF_ONLY = os.environ.get("FORCE_SELF_ONLY", "").strip() == "1"
# 실제로 이번 회차 2차 검증에 사용된 모델 — 메일 헤더 표기에 사용
_LAST_VERIFY_MODEL = CLAUDE_MODEL
# ── Gemini 모델 설정 ────────────────────────────────────────────────────
# ★2026-07-29 사고: gemini-2.5-flash로 승급했더니 fallback 100%.
#   원인은 해당 모델이 공지된 종료일(10/16)보다 일찍 내려간 것.
#   Google은 모델 은퇴 주기가 짧아, 단일 모델명을 하드코딩하면 조용히
#   전량 실패하고 유료 Claude가 1차 필터를 대신하게 된다(비용 급증).
# → 후보 목록을 두고 실패 시 다음 모델로 자동 전환한다.
#   GEMINI_MODEL을 지정하면 그 모델을 최우선으로 시도한다.
_GEMINI_CANDIDATES = [
    m.strip() for m in os.environ.get(
        "GEMINI_MODEL_CANDIDATES",
        "gemini-3.5-flash-lite,gemini-3.6-flash,gemini-flash-latest,gemini-2.5-flash-lite"
    ).split(",") if m.strip()
]
_env_model = os.environ.get("GEMINI_MODEL", "").strip()
if _env_model and _env_model in _GEMINI_CANDIDATES:
    _GEMINI_CANDIDATES.remove(_env_model)
if _env_model:
    _GEMINI_CANDIDATES.insert(0, _env_model)
GEMINI_MODEL = _GEMINI_CANDIDATES[0]

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
        fi = yf.Ticker(ticker).fast_info
        # fast_info는 yfinance 버전에 따라 dict 또는 object — 양쪽 호환
        def _fi_get(key_attr, key_dict):
            v = getattr(fi, key_attr, None)
            if v is None and isinstance(fi, dict):
                v = fi.get(key_dict)
            return v
        # 당일 등락률: (현재가 - 전일종가) / 전일종가 * 100
        prev = _fi_get("previous_close", "previousClose")
        curr = _fi_get("last_price", "lastPrice")
        if prev and curr and prev > 0:
            return round((curr - prev) / prev * 100, 2)
    except Exception:
        pass
    return None


def get_entity_price_drop(entity: str, exposure_data: dict) -> float | None:
    """dedup 우회 판정용 — 종목의 당일 등락률 조회 (국내·해외 겸용).

    build_price_alert_section()의 가격 조회는 이메일 렌더링 단계에서 일어나
    dedup 판정 시점에는 쓸 수 없으므로, 여기서 종목코드 기준으로 단건 조회한다.
    exposure_data에서 종목코드를 찾아 국내는 .KS 티커로 변환, 없으면 해외
    티커(get_price_change)로 폴백.

    반환: 등락률(예: -12.6) 또는 조회 실패 시 None
    """
    try:
        # find_exposure() 사용 — 직접 dict 조회는 법인명 표기차이·영문 별칭을
        # 못 잡아 종목코드를 찾지 못하고 조용히 None을 반환한다.
        rows = find_exposure(entity, exposure_data) or []
        # 국내 6자리 숫자 코드를 우선 선택. rows에는 채권(951F26 등 비숫자)·
        # 해외주식(NVDA 등)이 섞여 있어, 단순히 첫 행의 코드를 쓰면 주식 코드가
        # 있는데도 채권 코드를 집어 가격 조회에 실패한다.
        code = ""
        for r in rows:
            c = (r.get("종목코드") or "").strip()
            if c.isdigit() and len(c.zfill(6)) == 6:
                code = c
                break
        if code and code.isdigit():
            import yfinance as yf
            from datetime import datetime as _dt
            ticker = code.zfill(6) + ".KS"
            hist = yf.Ticker(ticker).history(period="5d", auto_adjust=False)
            if len(hist) < 2:
                return None
            kst = timezone(timedelta(hours=9))
            last_date = hist.index[-1]
            try:
                if last_date.tzinfo is not None:
                    last_kst = last_date.tz_convert("Asia/Seoul")
                else:
                    last_kst = last_date
                # 오늘 데이터가 아니면(휴장 등) 판정하지 않음
                if last_kst.strftime("%Y-%m-%d") != datetime.now(kst).strftime("%Y-%m-%d"):
                    return None
            except Exception:
                pass
            curr = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            if prev > 0:
                return round((curr - prev) / prev * 100, 2)
        # ── 해외 종목 폴백 ──
        # exposure_data의 해외주식·해외대출은 종목코드/종목명이 이미 티커
        # (TSLA, NVDA, QQQ 등)로 저장돼 있다. get_price_change()는 한글명을
        # 티커로 역변환하는 함수라 이 경우 매핑에 실패한다(실측: 해외 고유
        # 종목 2,224개 중 한글명 매핑 성공 25.4%, 미매핑 잔고 17.2조).
        # 따라서 rows에서 티커 형태 코드를 직접 찾아 조회하고, 그래도 없으면
        # entity 자체가 티커인지 확인한 뒤, 마지막으로 한글명 역변환을 시도한다.
        import re as _re

        def _is_ticker(s: str) -> bool:
            # 미국 상장 티커: 영문 대문자 1~5자.
            # 클래스 구분은 소스마다 표기가 다름 — CSV는 'BRK/B' 슬래시 형태,
            # yfinance는 'BRK-B' 하이픈 형태를 쓴다(_norm_ticker에서 변환).
            return bool(_re.fullmatch(r"[A-Z]{1,5}([./][A-Z])?", s or ""))

        def _norm_ticker(s: str) -> str:
            """CSV 표기(BRK/B)를 yfinance 표기(BRK-B)로 변환."""
            return (s or "").replace("/", "-").replace(".", "-")

        _ov_ticker = ""
        for r in rows:
            if r.get("종목유형") in ("해외주식", "해외대출"):
                for _cand in ((r.get("종목코드") or "").strip(),
                              (r.get("종목명") or "").strip()):
                    if _is_ticker(_cand):
                        _ov_ticker = _cand
                        break
            if _ov_ticker:
                break
        if not _ov_ticker and _is_ticker(entity.strip()):
            _ov_ticker = entity.strip()

        if _ov_ticker:
            import yfinance as yf
            hist = yf.Ticker(_norm_ticker(_ov_ticker)).history(period="5d", auto_adjust=False)
            if len(hist) >= 2:
                curr = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2])
                if prev > 0:
                    return round((curr - prev) / prev * 100, 2)
            return None

        # 한글명으로 저장된 해외 종목은 기존 역변환 경로 사용
        return get_price_change(entity)
    except Exception:
        return None


def _build_price_alert_section_uncached(exposure_data: dict, ref_date: str = '') -> str:
    """여신잔고 리스크 현황 섹션 HTML
    - 리스크종목 = Y + 종목유형 = 신용 행 추출 → 신용잔고 합산
    - yfinance 당일 등락률 조회 → -3% 이하 종목만 표시
    - 위험고객(리스크고객수 > 0) 컬럼 별도 표시
    - 탐지 종목 없으면 빈 문자열 반환
    - 모바일: 6컬럼 → font-size 10px + padding 축소로 대응

    ★결과 캐싱(2026-07-29): 이 함수는 한 회차에 3번 호출된다.
      ① 예비 발송범위 판정(2차 검증 모델 선택)
      ② 최종 발송범위 판정
      ③ 메일 HTML 생성
      매번 위험고객 보유 303종목의 시세를 조회하면 회차당 900회가 넘고
      실행시간이 약 3분 늘어난다(실측). 같은 회차 안에서는 시세가 바뀌어도
      판정이 흔들리면 안 되므로, 첫 호출 결과를 재사용한다.
      집계값(last_alerted_*)도 함께 보존해 판정 일관성을 보장한다.
    """
    try:
        import yfinance as yf
    except ImportError:
        _build_price_alert_section_uncached.last_alerted_count = 0
        _build_price_alert_section_uncached.last_alerted_rbal = 0
        return ''

    _build_price_alert_section_uncached.last_alerted_count = 0
    _build_price_alert_section_uncached.last_alerted_rbal = 0
    THRESHOLD = -3.0  # 2026-07-15: -5% → -3% 환원 (탐지 범위 확대)

    # 잔고 기준일 파싱
    bal_date = ref_date
    if not bal_date:
        for rows in exposure_data.values():
            if rows:
                bal_date = rows[0].get('기준일', '')
                break
    try:
        from datetime import datetime as _dt
        _d = _dt.strptime(bal_date, '%Y-%m-%d')
        bal_date_label = f'{_d.month:02d}월 {_d.day:02d}일'
    except Exception:
        bal_date_label = bal_date

    # 리스크종목 = Y + 종목유형 = 신용 행만 추출
    credit_map: dict = {}
    for rows in exposure_data.values():
        for r in rows:
            if r.get('종목유형', '') != '여신':
                continue
            if r.get('리스크종목', '').strip().upper() != 'Y':
                continue
            name = r.get('종목명', '').strip()
            if not name:
                continue
            def _pf(v):
                try:
                    return _num(v)
                except (ValueError, TypeError):
                    return 0.0
            bal, cust  = _pf(r.get('잔고(억)', 0)), int(_pf(r.get('고객수', 0)))
            rcust, rbal = int(_pf(r.get('리스크고객수', 0))), _pf(r.get('리스크잔고(억)', 0))
            if name not in credit_map:
                code = str(r.get('종목코드', '')).strip()
                credit_map[name] = {'bal': 0.0, 'cust': 0, 'rcust': 0, 'rbal': 0.0, 'code': code,
                                    'top_rbal': 0.0, 'top_cust': '', 'top_ratio': '',
                                    'ch': None}
            credit_map[name]['bal']   += bal
            credit_map[name]['cust']  += cust
            credit_map[name]['rcust'] += rcust
            credit_map[name]['rbal']  += rbal
            # 최고리스크 컬럼 (CSV에 없으면 빈값 유지)
            _top_rbal  = r.get('최고리스크잔고', '')
            _top_cust  = r.get('최고리스크고객', '')
            _top_ratio = r.get('유지담보비율', '')
            if _top_rbal:
                credit_map[name]['top_rbal']  = _top_rbal
                credit_map[name]['top_cust']  = _top_cust
                credit_map[name]['top_ratio'] = _top_ratio
            # 채널별 상세 (20컬럼 스키마일 때만 존재)
            if '뱅잔고' in r:
                ch = credit_map[name].setdefault('ch', None) or {
                    'b': {'bal': 0.0, 'cust': 0, 'rcust': 0, 'rbal': 0.0,
                          'top_rbal': '', 'top_cust': '', 'top_ratio': ''},
                    'y': {'bal': 0.0, 'cust': 0, 'rcust': 0, 'rbal': 0.0,
                          'top_rbal': '', 'top_cust': '', 'top_ratio': ''},
                }
                for key, pre in (('b', '뱅'), ('y', '영')):
                    ch[key]['bal']   += _pf(r.get(f'{pre}잔고'))
                    ch[key]['cust']  += int(_pf(r.get(f'{pre}고객수')))
                    ch[key]['rcust'] += int(_pf(r.get(f'{pre}리스크고객수')))
                    ch[key]['rbal']  += _pf(r.get(f'{pre}리스크잔고'))
                    if (r.get(f'{pre}최고리스크잔고') or '').strip():
                        ch[key]['top_rbal']  = r.get(f'{pre}최고리스크잔고', '')
                        ch[key]['top_cust']  = r.get(f'{pre}최고리스크고객', '')
                        ch[key]['top_ratio'] = r.get(f'{pre}유지담보비율', '')
                credit_map[name]['ch'] = ch

    if not credit_map:
        return ''

    total_count = len(credit_map)

    # ticker 매핑 — CSV 종목코드 우선, 없으면 TICKER_MAP_RUNTIME 역방향
    stock_list = []
    for name, info in sorted(credit_map.items(), key=lambda x: x[1]['bal'], reverse=True):
        # CSV row에서 종목코드 직접 추출
        raw_code = info.get('code', '')  # credit_map에 code 저장
        ticker = None
        if raw_code:
            if raw_code.isdigit():
                ticker = raw_code.zfill(6) + '.KS'  # 국내: 000660 → 000660.KS
            else:
                ticker = raw_code  # 해외: 이미 TSLA 등
        if not ticker:
            # fallback: TICKER_MAP_RUNTIME 역방향
            ticker = NAME_TO_TICKER.get(name)
            if not ticker:
                for t, n in TICKER_MAP_RUNTIME.items():
                    if n == name:
                        ticker = t
                        break
            # 해외 ticker .KS 미부착 확인
            if ticker and not ticker.endswith('.KS') and ticker.isdigit():
                ticker += '.KS'
        stock_list.append((name, info['bal'], info['cust'], info['rcust'], info['rbal'], ticker,
                           info.get('top_rbal',''), info.get('top_cust',''), info.get('top_ratio',''),
                           info.get('ch')))

    valid_tickers = [s[5] for s in stock_list if s[5]]
    if not valid_tickers:
        return ''

    # yfinance history — KST 기준 최근 거래일 데이터 사용
    # 날짜 일치 대신 '2거래일 이내' 조건 — 장중/장전/주말 모두 대응
    from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
    import pytz as _pytz
    _kst = _pytz.timezone('Asia/Seoul')
    _now_kst = _dt2.now(_kst)
    _today_str = _now_kst.strftime('%Y-%m-%d')

    def _fetch_price(item):
        """단일 종목 yfinance 조회 — (name, result_dict or None) 반환"""
        _n, _bal, _cu, _rc, _rb, _tk, _tr, _tc, _trat, _ch = item
        if not _tk:
            return _n, None
        try:
            hist = yf.Ticker(_tk).history(period='5d', interval='1d', auto_adjust=False)
            if hist is None or len(hist) < 2:
                return _n, None
            hist = hist.dropna(subset=['Close'])
            if len(hist) < 2:
                return _n, None
            # 마지막 거래일 KST 변환
            last_date = hist.index[-1]
            try:
                if last_date.tzinfo is None:
                    last_dt_kst = last_date.tz_localize('UTC').tz_convert('Asia/Seoul')
                else:
                    last_dt_kst = last_date.tz_convert('Asia/Seoul')
            except Exception:
                last_dt_kst = _now_kst  # 변환 실패 시 오늘로 가정
            # 오늘(KST) 데이터가 아니면 표시하지 않음 (주말·휴장일 등)
            if last_dt_kst.strftime('%Y-%m-%d') != _today_str:
                return _n, None
            curr = float(hist['Close'].iloc[-1])
            prev = float(hist['Close'].iloc[-2])
            if prev > 0:
                chg = round((curr - prev) / prev * 100, 2)
                return _n, {'chg': chg, 'curr': curr, 'ticker': _tk,
                            'top_rbal': _tr, 'top_cust': _tc, 'top_ratio': _trat}
        except Exception:
            pass
        return _n, None

    # ThreadPoolExecutor 병렬 조회 (순차 최대 90초 → 5~8초)
    price_map = {}
    _valid_items = [s for s in stock_list if s[5]]
    with ThreadPoolExecutor(max_workers=10) as _pex:
        _pfuts = {_pex.submit(_fetch_price, s): s for s in _valid_items}
        for _pf in as_completed(_pfuts):
            try:
                _pname, _pres = _pf.result()
                if _pres is not None:
                    price_map[_pname] = _pres
            except Exception:
                continue
    if not price_map:
        print(f'  [price_alert] yfinance 조회 실패 또는 오늘 데이터 없음')
        return ''

    # -3% 이하 필터
    alerted_raw = [
        (name, bal, cust, rcust, rbal,
         price_map[name]['chg'], price_map[name]['curr'], price_map[name]['ticker'],
         price_map[name].get('top_rbal',''), price_map[name].get('top_cust',''), price_map[name].get('top_ratio',''),
         ch)
        for name, bal, cust, rcust, rbal, ticker, top_rbal, top_cust, top_ratio, ch in stock_list
        if name in price_map and price_map[name]['chg'] <= THRESHOLD
    ]

    if not alerted_raw:
        return ''

    # 위험고객 있는 종목만 — 없으면 표시 불필요
    # 정렬: ① 리스크잔고 내림차순 ② 리스크고객수 내림차순
    alerted_sorted = sorted(
        [a for a in alerted_raw if a[3] > 0],
        key=lambda x: (-x[4], -x[3])
    )
    # 발송 게이트(main)에서 참조할 수 있도록 최종 종목 수를 함수 속성에 기록.
    # (-3% 초과 하락 + 위험고객 보유 종목이 다수여도 관련 뉴스가 하나도 안 잡히면
    # 메일 자체가 안 나가던 문제 방지용 — 시장 급락 시 뉴스 매칭 여부와 무관하게
    # 강제 전체발송 트리거로 사용)
    _build_price_alert_section_uncached.last_alerted_count = len(alerted_sorted)
    # 위험고객 리스크잔고 합계도 기록 — 종목 수가 적어도 잔고가 크면
    # 알릴 가치가 있으므로 발송 게이트에서 종목 수와 함께 판단한다.
    _build_price_alert_section_uncached.last_alerted_rbal = sum(
        (a[4] or 0) for a in alerted_sorted)
    if not alerted_sorted:
        return ''
    MAX_DISPLAY = 3
    display_alerted = alerted_sorted[:MAX_DISPLAY]
    extra_alerted   = alerted_sorted[MAX_DISPLAY:]

    def _fmt_price(curr, ticker):
        if not ticker.endswith('.KS'):
            return f'${curr:,.2f}'
        return f'{int(curr):,}원' if curr >= 1000 else f'{curr:.2f}원'

    # 채널 식별 컬러 — 라인 전체 착색 + ● 마커 (단어 반복 제거)
    _C_BANK   = '#2563eb'  # 뱅키스
    _C_BRANCH = '#8b5e3c'  # 영업점

    # 채널 라인 — 잔고·고객이 모두 0이면 '0억 (0명)' 대신 '-' (익스포저 카드와 동일 규칙)
    # 실사례(7/31 21시): KT&G·삼양식품 위험고객 컬럼에 영업점 '● 0억 (0명)'이 표시돼
    # 위험고객이 있는 것처럼 읽혔다. 정보가치 없는 0 표기를 제거한다.
    def _ch_line(color, bal, cust, first=True):
        mt = '' if first else 'margin-top:3px;'
        if cust <= 0 and bal < 0.05:
            return f'<div style="{mt}color:#cbd5e1;">-</div>'
        return f'<div style="{mt}color:{color};">● {bal:,.0f}억 ({cust:,}명)</div>'

    def _cust_bal_cell(cust, bal, ch):
        """전체 여신 칸 — 채널 모드: '● 잔고억 (고객수명)' 채널 컬러 2줄 (합산 없음)"""
        if ch:
            return (f'<td class="price-alert-td" style="padding:8px 3px;font-size:11px;font-weight:600;text-align:center;white-space:nowrap;">'
                    + _ch_line(_C_BANK,   ch["b"]["bal"],  ch["b"]["cust"],  True)
                    + _ch_line(_C_BRANCH, ch["y"]["bal"],  ch["y"]["cust"],  False) + '</td>')
        return (f'<td class="price-alert-td" style="padding:8px 3px;font-size:11px;font-weight:600;color:#1e293b;text-align:center;white-space:nowrap;">'
                f'{bal:,.0f}억 ({cust:,}명)</td>')

    def _risk_cell(rcust, rbal, ch):
        if rcust == 0:
            return '<td style="padding:8px 3px;font-size:11px;color:#cbd5e1;text-align:center;white-space:nowrap;">없음</td>'
        if ch:
            return (f'<td style="padding:8px 3px;font-size:11px;font-weight:600;text-align:center;white-space:nowrap;">'
                    + _ch_line(_C_BANK,   ch["b"]["rbal"], ch["b"]["rcust"], True)
                    + _ch_line(_C_BRANCH, ch["y"]["rbal"], ch["y"]["rcust"], False) + '</td>')
        return (f'<td style="padding:8px 3px;font-size:11px;font-weight:600;color:#92400e;text-align:center;white-space:nowrap;">'
                f'{rbal:.0f}억 ({rcust:,}명)</td>')

    def _top_line(dot_color, rbal, cust, ratio):
        """최고리스크 1줄 — 담보비율 선행(빨강 강조), 잔고·고객은 회색 보조
        담보비율은 정수 반올림 표시 — 소수점 2자리는 판단에 불필요하고,
        원본이 142.00처럼 소수부가 0이면 '142%'로 잘려 자릿수가 들쭉날쭉
        보이던 문제도 함께 해소(2026-07-24)."""
        if not (rbal or cust or ratio):
            return ''
        dot = f'<span style="color:{dot_color};">●</span> ' if dot_color else ''
        # 담보비율 이상치 방어 — 원본 엑셀에 0이 섞여 들어오는 사례 확인
        # (2026-07-24 업로드분 HD건설기계 여신 뱅유지담보비율=0.0).
        # 담보비율 0%는 실무상 성립하지 않는 값(계산 불가·데이터 누락 추정)이라
        # 그대로 표시하면 '가장 위험한 고객의 담보비율이 0%'로 오독된다.
        # 100 미만(비정상 대역)은 표시하지 않는다 — 정상 범위는 130~150대.
        _ratio_disp = ratio
        _ratio_bad = False
        if ratio not in (None, ''):
            try:
                _rv = float(ratio)
                if _rv < 100:
                    _ratio_bad = True
                else:
                    _ratio_disp = f'{round(_rv):,}'
            except (ValueError, TypeError):
                _ratio_disp = ratio
        if _ratio_bad:
            ratio = ''  # 비정상 값 → 담보비율 부분만 생략(잔고·고객은 유지)
        ratio_html = f'<span style="color:#dc2626;font-weight:700;">{_ratio_disp}%</span> ' if ratio else ''
        detail = "·".join(str(x) for x in (f'{rbal}억' if rbal else '', cust or '') if x)
        detail_html = f'<span style="color:#64748b;font-weight:400;">({detail})</span>' if detail else ''
        return f'{dot}{ratio_html}{detail_html}'.strip()

    def _top_risk_cell(top_rbal, top_cust, top_ratio, ch):
        if ch:
            # 빈 채널도 '-'로 자리를 유지한다 (2026-08-03).
            # 기존엔 값이 있는 줄만 렌더해, 위험고객 칸은 뱅/영 2줄인데 최고
            # 리스크 칸은 1줄이 되어 두 컬럼의 채널 행이 어긋났다.
            #   실사례(8/3 21시 미래에셋증권): 위험고객 뱅 '● 5억 (2명)' / 영 '-'
            #   인데 최고 리스크는 뱅 줄만 있어 영업점 행이 사라졌다.
            # 두 컬럼 모두 '뱅키스 위 / 영업점 아래' 2줄 구조를 항상 유지한다.
            _dash = '<span style="color:#cbd5e1;">-</span>'
            b_line = _top_line(_C_BANK,   ch['b']['top_rbal'], ch['b']['top_cust'], ch['b']['top_ratio']) or _dash
            y_line = _top_line(_C_BRANCH, ch['y']['top_rbal'], ch['y']['top_cust'], ch['y']['top_ratio']) or _dash
            return (f'<td style="padding:8px 3px;font-size:11px;font-weight:600;text-align:center;white-space:nowrap;">'
                    f'<div>{b_line}</div>'
                    f'<div style="margin-top:3px;">{y_line}</div></td>')
        if not top_rbal and not top_cust:
            return '<td style="padding:8px 3px;font-size:11px;color:#cbd5e1;text-align:center;white-space:nowrap;">-</td>'
        line = _top_line('', top_rbal, top_cust, top_ratio)
        return f'<td style="padding:8px 3px;font-size:11px;font-weight:600;text-align:center;white-space:nowrap;">{line}</td>'

    rows_html = ''
    for i, (name, bal, cust, rcust, rbal, chg, curr, ticker, top_rbal, top_cust, top_ratio, ch) in enumerate(display_alerted):
        bg = '#fafcff' if i % 2 == 0 else '#ffffff'
        rows_html += f'''
            <tr style="background:{bg};border-bottom:1px solid #f1f5f9;">
              <td class="price-alert-td" style="padding:8px 3px;text-align:center;white-space:nowrap;">
                <div style="font-size:13px;font-weight:600;color:#1e293b;">{name}</div>
                <div style="font-size:10px;font-weight:700;color:#2563eb;margin-top:2px;">▼{abs(chg):.1f}%</div>
              </td>
              {_cust_bal_cell(cust, bal, ch)}
              {_risk_cell(rcust, rbal, ch)}
              {_top_risk_cell(top_rbal, top_cust, top_ratio, ch)}
            </tr>'''

    _has_channel = any(a[11] for a in display_alerted)
    _legend = (f'<span style="color:{_C_BANK};">●</span><span style="color:#ffffff;font-weight:700;"> 뱅키스</span>'
               f' &nbsp;<span style="color:{_C_BRANCH};">●</span><span style="color:#ffffff;font-weight:700;"> 영업점</span>') if _has_channel else ''

    return f'''
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:14px;border:1px solid #e2e8f0;border-top:3px solid #475569;">
      <tr>
        <td bgcolor="#1e293b" style="padding:10px 14px;background:#1e293b;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="font-size:14px;font-weight:500;color:#f8fafc;white-space:nowrap;">📉 여신잔고 리스크 현황</td>
              <td align="right" style="font-size:11px;padding-left:10px;white-space:nowrap;">{_legend}</td>
            </tr>
            <tr>
              <td colspan="2" style="padding-top:3px;">
                <span style="font-size:12px;color:#fbbf24;">⚠ 위험고객: 단일종목 여신잔고 1억원이상 · 담보유지비율 140%~150%</span>
              </td>
            </tr>
            <tr>
              <td colspan="2" class="loan-hdr-right" style="font-size:11px;color:#94a3b8;padding-top:2px;white-space:nowrap;">단일종목 여신잔고 1억↑ 종목 {total_count}개 · {bal_date_label} 기준</td>
            </tr>
          </table>
        </td>
      </tr>
      <tr><td bgcolor="#ffffff" style="background:#ffffff;">
        <!--[if mso]><table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          <td width="16%"><![endif]-->
        <table cellpadding="0" cellspacing="0" border="0" class="price-alert-table" style="border-collapse:collapse;width:100%;">

          <thead>
            <tr bgcolor="#f8fafc" style="background:#f8fafc;border-bottom:1px solid #e2e8f0;">
              <th style="padding:7px 3px;font-size:11px;color:#64748b;font-weight:500;text-align:center;">종목명</th>
              <th style="padding:7px 3px;font-size:11px;color:#64748b;font-weight:500;text-align:center;">전체 여신</th>
              <th style="padding:7px 3px;font-size:11px;color:#d97706;font-weight:600;text-align:center;">⚠ 위험고객</th>
              <th style="padding:7px 3px;font-size:11px;color:#dc2626;font-weight:600;text-align:center;">최고 리스크</th>
            </tr>
          </thead>
          <tbody>{rows_html}
            {('<tr style="background:#fff3cd;"><td colspan="4" style="padding:8px 10px;font-size:11px;color:#92400e;font-weight:600;border-top:1px solid #fde68a;">&#9888; 외 ' + str(len(extra_alerted)) + '개 종목 추가 탐지 — 담당자 즉시 확인 <span style="font-weight:400;color:#b45309;font-size:10px;">(' + ", ".join([x[0] for x in sorted(extra_alerted, key=lambda x: x[4], reverse=True)[:5]]) + ("..." if len(extra_alerted) > 5 else "") + ')</span></td></tr>') if extra_alerted else ''}
            <tr bgcolor="#fafafa" style="background:#fafafa;">
              <td colspan="4" style="padding:7px 10px;font-size:12px;color:#94a3b8;border-top:1px solid #e2e8f0;">
                가격·등락률 출처: 야후파이낸스 (15분 지연) &nbsp;·&nbsp; 당일 -3% 초과 하락 + 위험고객 보유 종목만 표시
              </td>
            </tr>
          </tbody>
        </table>
      </td></tr>
    </table>'''



def build_price_alert_section(exposure_data: dict, ref_date: str = '') -> str:
    """여신잔고 리스크 현황 섹션 — 회차 내 결과 캐싱 래퍼.

    ★이 함수는 한 회차에 3번 호출된다(2026-07-29 실측):
      ① 예비 발송범위 판정(2차 검증 모델 선택)
      ② 최종 발송범위 판정
      ③ 메일 HTML 생성
    매번 위험고객 보유 303종목의 시세를 조회하면 회차당 900회를 넘고
    실행시간이 약 3분 늘어난다. 또한 호출 시점마다 시세가 달라지면
    '판정에 쓴 값'과 '메일에 표시된 값'이 어긋날 수 있다.
    → 같은 입력(익스포저·기준일)이면 첫 결과를 재사용해 성능과 일관성을
      동시에 확보한다. 집계값(last_alerted_*)도 함께 복원한다.
    """
    _ck = (id(exposure_data), ref_date)
    _c = getattr(build_price_alert_section, "_cache", None)
    if _c and _c.get("key") == _ck:
        build_price_alert_section.last_alerted_count = _c["count"]
        build_price_alert_section.last_alerted_rbal = _c["rbal"]
        return _c["html"]

    html = _build_price_alert_section_uncached(exposure_data, ref_date)
    cnt = getattr(_build_price_alert_section_uncached, "last_alerted_count", 0)
    rbal = getattr(_build_price_alert_section_uncached, "last_alerted_rbal", 0)
    build_price_alert_section.last_alerted_count = cnt
    build_price_alert_section.last_alerted_rbal = rbal
    build_price_alert_section._cache = {"key": _ck, "html": html,
                                        "count": cnt, "rbal": rbal}
    return html


def clear_price_alert_cache():
    """가격 경보 캐시 무효화.

    운영에서는 회차마다 프로세스가 새로 뜨므로 불필요하지만, 테스트가
    시세 조건(급락/평상)을 바꿔가며 검증할 때는 명시적으로 비워야 한다.
    """
    build_price_alert_section._cache = None
    for _f in (build_price_alert_section, _build_price_alert_section_uncached):
        _f.last_alerted_count = 0
        _f.last_alerted_rbal = 0


def normalize_ticker(name: str) -> str:
    """종목명이 티커(영문 대문자)이면 한글명으로 변환, 아니면 그대로 반환
    ticker_map.json → TICKER_TO_NAME 순으로 조회
    예: 'NVDA' → '엔비디아', 'IONQ' → '아이온큐', '삼성전자' → '삼성전자'
    """
    stripped = name.strip()
    if re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', stripped):
        return TICKER_MAP_RUNTIME.get(stripped, stripped)
    return stripped

def _normalize_ratio_pct(v: str) -> str:
    """유지담보비율 단위 방어 로직 — 소수(1.41)를 퍼센트(141)로 보정하고 정수화.

    [보정] 7/20 exposure_data.csv 업로드 시 원본 엑셀 export 서식이 바뀌어
    '146.87'(퍼센트) 대신 '1.4687'(소수)로 들어온 실사례 발생 → 코드가
    CSV 값 뒤에 '%'만 그대로 붙이는 구조(_top_line())라 메일에 '1.41%'로
    잘못 표기될 뻔함. 유지담보비율은 성격상 항상 두 자릿수 이상(보통
    130~150대)이므로 10 미만이면 소수 표기로 판단해 ×100 한다.

    [정수화] (2026-08-10 강화) 소수부는 판단에 불필요하고, 표시 경로가
    _top_line() 하나뿐이라는 보장이 없어 정규화 단계에서 미리 정수로 만든다.
    표시부의 round()와 이중으로 걸려, 어느 경로로 새더라도 'xxx%' 형태만
    노출된다. 채널 대표값 선택은 float 비교라 1% 미만 정밀도 손실은 무해하다.
    """
    v = (v or "").strip()
    if not v:
        return v
    try:
        x = float(v)
    except (ValueError, TypeError):
        return v
    if 0 < x < 10:
        x *= 100
    return str(round(x))


def _synthesize_channel_totals(row: dict) -> dict:
    """20컬럼(뱅/영 채널 분리) 스키마 row에 레거시 합산 키를 합성해 반환.
    - 뱅잔고 키가 없으면(기존 12컬럼) 원본 그대로 반환 — 하위호환
    - 합성 키: 잔고(억)·고객수·리스크종목·리스크고객수·리스크잔고(억) = 채널 합산,
      최고리스크잔고·최고리스크고객·유지담보비율 = 담보비율 낮은(더 위험한) 채널 대표값
    - 원본 뱅*/영* 키는 그대로 보존 → 표시 함수가 채널 병기에 사용
    """
    # 스키마 무관 방어 로직 — 신/구 스키마 모두 유지담보비율 계열 키를 여기서 정규화
    for _k in ("유지담보비율", "뱅유지담보비율", "영유지담보비율"):
        if _k in row:
            row[_k] = _normalize_ratio_pct(row[_k])

    if "뱅잔고" not in row:
        return row

    def _f(v):
        try:
            return _num(v)
        except (ValueError, TypeError):
            return 0.0

    def _i(v):
        return int(_f(v))

    b_bal, y_bal   = _f(row.get("뱅잔고")), _f(row.get("영잔고"))
    b_cus, y_cus   = _i(row.get("뱅고객수")), _i(row.get("영고객수"))
    b_rc,  y_rc    = _i(row.get("뱅리스크고객수")), _i(row.get("영리스크고객수"))
    b_rb,  y_rb    = _f(row.get("뱅리스크잔고")), _f(row.get("영리스크잔고"))
    b_y = (row.get("뱅리스크종목") or "").strip().upper() == "Y"
    y_y = (row.get("영리스크종목") or "").strip().upper() == "Y"

    row["잔고(억)"]     = f"{b_bal + y_bal:g}"
    row["고객수"]       = str(b_cus + y_cus)
    row["리스크종목"]   = "Y" if (b_y or y_y) else ""
    row["리스크고객수"] = str(b_rc + y_rc)
    row["리스크잔고(억)"] = f"{b_rb + y_rb:g}"

    # 대표 최고리스크 = 담보비율이 낮은(마진콜에 가까운) 채널
    b_ratio = _f(row.get("뱅유지담보비율")) if (row.get("뱅유지담보비율") or "").strip() else None
    y_ratio = _f(row.get("영유지담보비율")) if (row.get("영유지담보비율") or "").strip() else None
    if b_ratio is not None and (y_ratio is None or b_ratio <= y_ratio):
        row["최고리스크잔고"] = row.get("뱅최고리스크잔고", "")
        row["최고리스크고객"] = row.get("뱅최고리스크고객", "")
        row["유지담보비율"]   = row.get("뱅유지담보비율", "")
    elif y_ratio is not None:
        row["최고리스크잔고"] = row.get("영최고리스크잔고", "")
        row["최고리스크고객"] = row.get("영최고리스크고객", "")
        row["유지담보비율"]   = row.get("영유지담보비율", "")
    else:
        row.setdefault("최고리스크잔고", "")
        row.setdefault("최고리스크고객", "")
        row.setdefault("유지담보비율", "")
    return row

def load_exposure_data() -> dict:
    """CSV에서 익스포저 데이터 로드 — {종목명: [row, ...]} 리스트 딕셔너리 반환
    지원 스키마:
      (신) 20컬럼: 기준일,종목명,종목코드,종목유형 + 뱅/영 채널별 8항목 — 합산키 자동 합성
      (구) 12컬럼: 기준일,종목명,종목코드,종목유형,잔고(억),고객수,리스크종목,... — 그대로 사용
    헤더 깨진 경우 positional 파싱으로 자동 fallback"""
    if not os.path.exists(EXPOSURE_FILE):
        print(f"  [경고] {EXPOSURE_FILE} 파일 없음 — 익스포저 매칭 비활성화 (리스크 점수 보정 불가)")
        return {}
    try:
        with open(EXPOSURE_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows_all = list(reader)
            if rows_all and "종목명" in (rows_all[0] or {}):
                result = {}
                for row in rows_all:
                    name = normalize_ticker((row.get("종목명") or "").strip())
                    row["종목명"] = name  # 티커→한글명 정규화
                    if not name:
                        continue
                    row = _synthesize_channel_totals(row)
                    # 시장 컬럼 없으면 국내 기본값
                    if "시장" not in row:
                        row["시장"] = "국내"
                    result.setdefault(name, []).append(row)
                return result
    except Exception as e:
        print(f"  [경고] {EXPOSURE_FILE} DictReader 파싱 실패: {e} — positional fallback 시도")
    # fallback: positional 파싱
    try:
        result = {}
        with open(EXPOSURE_FILE, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            for row in reader:
                if len(row) < 5:
                    continue
                d = {
                    "기준일":      row[0].strip(),
                    "종목명":      row[1].strip(),
                    "종목코드":    str(row[2]).strip().zfill(6) if len(row) > 2 and row[2] else "",
                    "종목유형":    row[3].strip() if len(row) > 3 else "",
                    "잔고(억)":    row[4].strip() if len(row) > 4 else "0",
                    "고객수":      row[5].strip() if len(row) > 5 else "0",
                    "리스크종목":  row[6].strip() if len(row) > 6 else "",
                    "리스크고객수": row[7].strip() if len(row) > 7 else "0",
                    "리스크잔고(억)": row[8].strip() if len(row) > 8 else "0",
                    "최고리스크잔고": row[9].strip() if len(row) > 9 else "",
                    "최고리스크고객": row[10].strip() if len(row) > 10 else "",
                    "유지담보비율":  _normalize_ratio_pct(row[11].strip() if len(row) > 11 else ""),
                }
                name = normalize_ticker(d["종목명"])
                d["종목명"] = name
                if name:
                    result.setdefault(name, []).append(d)
        print(f"  [경고] {EXPOSURE_FILE} positional fallback 파싱 완료 ({len(result)}종목) — 헤더 확인 필요")
        return result
    except Exception as e:
        print(f"  [오류] {EXPOSURE_FILE} 파싱 완전 실패: {e} — 익스포저 매칭 비활성화")
        return {}

def get_overseas_keywords(exposure_data: dict = None, top_n: int = 30) -> list:
    """해외주식 포괄 리스크 키워드 반환
    종목명 기반 동적 키워드 대신 포괄 키워드 사용
    → ticker_mapper 선행 불필요, AI가 기사에서 entity 직접 추출
    """
    return list(OVERSEAS_KEYWORDS)

def sanitize_action_numbers(action: str, exp_rows: list, src_text: str = "") -> tuple:
    """
    대응방안(action) 텍스트에서 익스포저 관련 수치(N명·N억원)를 추출해,
    실제 제공된 익스포저(exp_rows)의 값 또는 그 부분집합 합산으로 설명되지 않는
    '창작된 수치'가 있으면 True(오염)로 판정한다. 할루시네이션 원천 차단용 검증 계층.

    src_text: 기사 제목+본문. 여기에 실재하는 수치는 '기사 인용'이므로 창작이 아니다.
      (2026-07-31 추가) 기존엔 익스포저 값이 아니면 무조건 창작으로 봐서, 기사의
      '사건 규모' 수치까지 오염 처리됐다. 실사례(7/30 14시 HUG): 기사 원문의
      "3,820억 사업장"이 오염 판정 → 문장이 "규모 부실 사업장"으로 파손됐다.

    반환: (오염여부: bool, 오염수치목록: list)
    - exp_rows가 비어 있으면(익스포저 없음) 검증 스킵 → (False, [])
    - action에 '억원'/'명' 수치가 아예 없으면 → (False, [])
    """
    if not exp_rows:
        return (False, [])

    # 기사 원문에 실재하는 수치 수집 — 창작 판정에서 면제
    src_bal, src_cust = set(), set()
    if src_text:
        for m in re.finditer(r'([\d,]+)\s*억', src_text):
            try:
                src_bal.add(int(m.group(1).replace(',', '')))
            except ValueError:
                pass
        for m in re.finditer(r'([\d,]+)\s*(?:명|가구|세대)', src_text):
            try:
                src_cust.add(int(m.group(1).replace(',', '')))
            except ValueError:
                pass

    # 실제 익스포저에서 정당한 잔고·고객수 값 수집
    # 20컬럼 스키마면 채널별(뱅/영) 값도 허용 — AI가 채널 수치를 인용해도 오염 아님
    bal_vals, cust_vals = [], []
    for r in exp_rows:
        for _bk in ('잔고(억)', '뱅잔고', '영잔고', '리스크잔고(억)', '뱅리스크잔고', '영리스크잔고'):
            try:
                b = int(round(_num(r.get(_bk))))
                if b > 0:
                    bal_vals.append(b)
            except (ValueError, TypeError):
                pass
        for _ck in ('고객수', '뱅고객수', '영고객수', '리스크고객수', '뱅리스크고객수', '영리스크고객수'):
            try:
                c = int(_num(r.get(_ck)))
                if c > 0:
                    cust_vals.append(c)
            except (ValueError, TypeError):
                pass

    def _allowed_sums(vals, cap=12):
        """개별 값 + 부분집합 합산(최대 cap개 종목까지)을 허용 집합으로."""
        allowed = set(vals)
        # 종목 수가 많지 않을 때만 부분집합 합산 계산 (조합 폭발 방지)
        import itertools as _it
        vv = vals[:cap]
        for k in range(2, len(vv) + 1):
            for combo in _it.combinations(vv, k):
                allowed.add(sum(combo))
        # 전체 합산도 허용
        if vals:
            allowed.add(sum(vals))
        return allowed

    allowed_bal = _allowed_sums(bal_vals) | src_bal
    allowed_cust = _allowed_sums(cust_vals) | src_cust

    tainted = []

    # 검증 제외: "N억원 이상/이하/초과/미만" 같은 임계 기준 표현은 익스포저 수치가
    # 아니라 정책 기준이므로 통과. (예: "여신 1억원 이상 고객")
    _THRESHOLD_RE = r'\d[\d,]*\s*억원?\s*(?:이상|이하|초과|미만)'
    _action_for_check = re.sub(_THRESHOLD_RE, ' ', action)

    # 금액: "106억원", "1,062 억" 등 → '억' 앞 숫자
    for m in re.finditer(r'([\d,]+)\s*억', _action_for_check):
        try:
            v = int(m.group(1).replace(',', ''))
        except ValueError:
            continue
        if v == 0:
            continue
        # ±1 오차 허용(반올림 표기차)
        if not any(abs(v - a) <= 1 for a in allowed_bal):
            tainted.append(f"{v}억")

    # 고객수: "1,062명", "499 명" 등 → '명' 앞 숫자
    for m in re.finditer(r'([\d,]+)\s*명', _action_for_check):
        try:
            v = int(m.group(1).replace(',', ''))
        except ValueError:
            continue
        if v == 0:
            continue
        if not any(abs(v - a) <= 1 for a in allowed_cust):
            tainted.append(f"{v}명")

    return (len(tainted) > 0, tainted)

# ── 오염 수치 '타깃' 제거 (2026-07-31 신설) ──
# 기존 구현은 오염이 1건이라도 잡히면 문장 내 모든 '억/명' 수치를 일괄 삭제해,
# 익스포저와 무관한 '사건 규모' 수치까지 소실시켰다.
#   실사례(7/30 14시 HUG): "3,820억 규모 부실 사업장" → "규모 부실 사업장"
# → sanitize_action_numbers()가 오염으로 지목한 값만 제거하고 나머지는 보존한다.
_THRESHOLD_RE_STR = r'\d[\d,]*\s*억원?\s*(?:이상|이하|초과|미만)'

def _strip_tainted_numbers(text: str, bad: list) -> str:
    """bad(예: ["1062명", "106억"])에 해당하는 수치만 제거. 정상 수치는 유지."""
    if not text or not bad:
        return text
    bad_bal  = {int(b[:-1]) for b in bad if b.endswith('억')}
    bad_cust = {int(b[:-1]) for b in bad if b.endswith('명')}

    # 임계 기준 표현("여신 1억원 이상")은 정책 문구이므로 보호 후 복원
    _protected = []
    def _prot(m):
        _protected.append(m.group(0))
        return f"\x00{len(_protected)-1}\x00"
    text = re.sub(_THRESHOLD_RE_STR, _prot, text)

    def _repl(m, pool):
        try:
            v = int(m.group(1).replace(',', ''))
        except ValueError:
            return m.group(0)
        return '' if v in pool else m.group(0)

    # 괄호 수치구는 내부 수치가 '전부' 오염일 때만 괄호째 제거
    def _paren(m):
        inner = m.group(0)
        vals = []
        for x in re.findall(r'([\d,]+)\s*(?:억|명)', inner):
            try:
                vals.append(int(x.replace(',', '')))
            except ValueError:
                pass
        if vals and all((v in bad_bal or v in bad_cust) for v in vals):
            return ''
        return inner
    text = re.sub(r'[\(（][^()（）]*(?:억|명)[^()（）]*[\)）]', _paren, text)

    # 수치를 지울 때 앞뒤 수식어까지 함께 지운다 (2026-08-13).
    # 기존엔 숫자만 제거해 "약 242억 규모" → "약 규모"처럼 문장이 깨졌다
    # (8/13 14시 IS동서: "총 채권 익스포저 약 규모, 35명 포함"). 괄호 안에
    # 정상 수치가 섞여 있으면 괄호째 제거도 안 되므로 여기서 처리해야 한다.
    # 수식어는 non-capturing이라 정상 수치일 때는 원본이 그대로 보존된다.
    _MOD = r'(?:약|총|최대|최소|무려|각각|각)?\s*'
    _SUF = r'(?:\s*(?:규모|상당|가량|수준))?'
    text = re.sub(_MOD + r'([\d,]+)\s*억원?' + _SUF, lambda m: _repl(m, bad_bal), text)
    text = re.sub(_MOD + r'([\d,]+)\s*명' + _SUF,    lambda m: _repl(m, bad_cust), text)
    # 수치가 빠지며 홀로 남은 조사·기호 정리
    text = re.sub(r'\(\s*[,·]\s*', '(', text)
    text = re.sub(r'\s*[,·]\s*\)', ')', text)

    for _i, _p in enumerate(_protected):
        text = text.replace(f"\x00{_i}\x00", _p)
    return re.sub(r'\s{2,}', ' ', text).replace(' ,', ',').replace(' .', '.').strip()

# ── 고객케어 안내 문구 검증 계층 (2026-07-31 신설) ──
# 지금까지 할루시네이션 방어는 action(대응방안)에만 걸려 있었고 customer_notice는
# 무검증으로 발송됐다. 고객 안내 문구는 그대로 고객에게 전달될 수 있어 action보다
# 위험도가 높다. 실사례(7/30 21시 '더 테크놀로지' 긴급메일):
#   - "2025년 8월 10일 상장 폐지" → 실제 2026년, 연도 환각
#   - "더 테크놀로지(종목코드 확인 요망)" → AI 플레이스홀더가 그대로 발송
_NOTICE_PLACEHOLDER_RE = re.compile(
    r'[\(（][^()（）]{0,40}?'
    r'(?:확인\s*요망|확인\s*바람|확인\s*필요|기입|삽입|입력\s*요망|추후\s*확인|미확인|TBD)'
    r'[^()（）]{0,40}?[\)）]'
)
_NOTICE_BARE_PLACEHOLDER = ("종목코드 확인 요망", "종목코드 확인요망",
                            "○○○", "○○", "XXX", "OOO", "[내용 확인]", "[확인]")

def sanitize_customer_notice(notice: str, exp_rows: list, src_text: str = "") -> tuple:
    """고객 안내 문구에서 플레이스홀더·연도 환각·창작 수치를 제거.

    반환: (정제된 문구, 수정내역 목록)
    """
    if not notice:
        return (notice, [])
    fixed, out = [], notice

    # 1) 플레이스홀더 제거
    _new = _NOTICE_PLACEHOLDER_RE.sub('', out)
    if _new != out:
        fixed.append("플레이스홀더(괄호구)")
        out = _new
    for _ph in _NOTICE_BARE_PLACEHOLDER:
        if _ph in out:
            out = out.replace(_ph, '')
            fixed.append(f"플레이스홀더({_ph})")

    # 2) 연도 환각 제거 — 과거 연도 또는 내년 초과 연도는 삭제("2025년 8월 10일"→"8월 10일")
    _cur_year = datetime.now(timezone(timedelta(hours=9))).year
    def _fix_year(m):
        try:
            y = int(m.group(1))
        except ValueError:
            return m.group(0)
        if y < _cur_year or y > _cur_year + 1:
            fixed.append(f"연도({y}년)")
            return ''
        return m.group(0)
    out = re.sub(r'(\d{4})\s*년\s*', _fix_year, out)

    # 2-b) 월 환각 제거 (2026-08-14 신설)
    # 실사례(8/14 21시 'TIME 미국배당다우존스액티브' 긴급메일):
    #   기사는 "19일 상장폐지 / 18일 거래정지"인데 고객문구는 "5월 19일 폐지,
    #   마지막 거래일 5월 18일" — AI가 없는 달을 창작. 연도 표기가 없어
    #   2)의 연도 가드를 우회했다. 고객에게 그대로 나가면 매도 시한 오인 유발.
    # 판정: 기사 원문에 그 달이 등장하지 않고, 현재월 ±1 범위도 아니면 삭제
    #       ("5월 19일" → "19일"). 일자는 기사 근거가 있으므로 보존한다.
    _cur_month = datetime.now(timezone(timedelta(hours=9))).month
    _ok_months = {(_cur_month - 2) % 12 + 1, _cur_month, _cur_month % 12 + 1}
    def _fix_month(m):
        try:
            mm = int(m.group(1))
        except ValueError:
            return m.group(0)
        if not (1 <= mm <= 12):
            return m.group(0)
        if f"{mm}월" in src_text or mm in _ok_months:
            return m.group(0)
        fixed.append(f"월({mm}월)")
        return m.group(2)          # "5월 19일" → "19일"
    out = re.sub(r'(\d{1,2})\s*월\s*(\d{1,2}\s*일)', _fix_month, out)

    # 3) 창작 수치 제거 — action과 동일 기준, 오염 값만 타깃 제거
    _t, _bad = sanitize_action_numbers(out, exp_rows, src_text)
    if _t:
        out = _strip_tainted_numbers(out, _bad)
        fixed.append(f"창작수치{_bad}")

    out = re.sub(r'[ \t]{2,}', ' ', out)
    out = re.sub(r'\(\s*\)|（\s*）', '', out)
    return (out.strip(), fixed)

# 우선주 접미 — 본주와 별개 종목이므로 익스포저 병합에서 제외한다.
# 국내 표기 관례: 우 / 우B / 1우 / 2우B / 3우B / 우선주
_PREF_STOCK_SUFFIX_RE = re.compile(r'\d*우[A-Z]?|우선주')

# ── 익스포저 미보유 유형의 조치 문구 제거 (2026-08-01 신설) ──
# action_prompt.txt는 이미 "[OB 인계 제외 조건] 여신 익스포저 없음 또는 총규모
# 10억 미만 → OB 문구 생략"을 명시하고 있으나, AI가 이를 위반한 사례가 발생했다.
# 실사례(7/31 21시 롯데카드): 익스포저가 채권 1,502억뿐이고 여신은 0인데
# "여신 보유잔고 3억원 이상 고객 즉시 인계, OB 최우선 진행"이 붙어, 담당자가
# 실행할 수 없는 조치가 임원 메일에 나갔다.
# 프롬프트 준수에만 의존하지 않고 결정론적 코드 게이트로 강제한다.
_OB_CLAUSE_RE = re.compile(
    r'(?:\s*(?:→|,|·)\s*)?여신\s*보유\s*잔고\s*[\d,]+\s*(?:억원|억|천만원)\s*이상\s*'
    r'고객\s*(?:즉시\s*)?인계\s*[,、]?\s*OB\s*(?:최우선\s*)?진행'
)
# 여신 미보유 시 제거할 '그 외' 조치 표현 (2026-08-02 확장)
# 기존엔 OB 인계 문구 하나만 잡았는데, 8/2 14시 다원시스 건에서 다른 표현으로
# 같은 문제가 재발했다. 익스포저가 주식뿐(여신 '잔고 없음')인데 대응방안에
#   "신용융자·미수 보유 고객 우선 추출하여 담보비율 긴급 점검,
#    반대매매 연쇄 가능성 사전 차단"
# 이 붙어, 여신이 없으니 신용융자도 없는 종목에 실행 불가 조치가 나갔다.
#
# [삭제 단위] 화살표(→)로 구분되는 '절' 단위로만 지운다. 정규식으로 구절을
# 잘라내면 유효 조치까지 훼손된다 — 실측에서 "평가손 산출", "손실 금액
# 재산출"이 함께 사라졌다.
# [삭제 조건] 절이 여신 전제 조치인지 보수적으로 판정한다.
#   ① 절이 여신 키워드로 시작하거나  ② 여신 키워드가 2개 이상
# 조건 미달이면 유지 — 곁가지로 한 번 언급된 정도는 문장을 살리는 쪽이 낫다.
_YEOSIN_DEP_KW = ("신용융자", "미수금", "미수", "담보비율", "담보 비율",
                  "담보부족", "담보 부족", "반대매매", "반대 매매",
                  "강제매도", "강제 매도", "강제청산", "강제 청산", "마진콜",
                  # (2026-08-03 보강) 8/3 14시 JR리츠 건에서 여신 잔고가 없는데
                  # "담보대출 보유 고객 담보비율 긴급 점검"이 통과했다. 담보대출·
                  # 신용거래·주식담보는 모두 여신 잔고를 전제한 표현이다.
                  "담보대출", "담보 대출", "주식담보", "주식 담보",
                  "신용거래", "신용 거래", "융자잔고", "융자 잔고", "대출잔고",
                  # (2026-08-04 보강) 8/4 14시 본느 건 — 여신 0인데
                  # "담보계좌·신용계좌 전수 점검"이 통과했다.
                  "담보계좌", "담보 계좌", "신용계좌", "신용 계좌",
                  "담보부족계좌", "위탁증거금", "증거금")

# 조치 서술어 — 하위절 삭제 후 문장이 깨지지 않았는지 확인용
_ACTION_VERB_RE = re.compile(
    r'(?:산출|점검|보고|공유|확인|추적|준비|진행|착수|파악|정비|수립|요청|'
    r'인계|차단|대비|검토|조회|추출|통보|안내|모니터링|실시|이행)\s*$'
)

def _is_yeosin_dependent_clause(clause: str) -> bool:
    """절이 여신(신용거래) 잔고를 전제로 한 조치인지."""
    c = _NS_RE.sub("", clause)
    kws = [k for k in _YEOSIN_DEP_KW if _NS_RE.sub("", k) in c]
    if not kws:
        return False
    if len(set(_NS_RE.sub("", k) for k in kws)) >= 2:
        return True
    # '절 시작'은 앞 6자로 한정 — 넓게 잡으면 곁가지로 한 번 언급된 절까지
    # 걸린다(실측: "상폐 확정 시 강제 매도 수량 재산출 후 보고"가 제거됨).
    head = c[:6]
    return any(_NS_RE.sub("", k) in head for k in kws)

_NOTICE_CLAUSE_RE = re.compile(r'(?:\s*(?:→|,|·)\s*)?[^→,·]{0,20}?고객\s*안내\s*준비[^→,·]{0,30}')
_YEOSIN_TYPES_ACT = ("여신", "해외대출")
_STOCK_TYPES_ACT  = ("주식", "해외주식")
_OB_MIN_YEOSIN    = 10.0   # 억. action_prompt.txt의 OB 제외 기준과 동일

# 대응방안 중복 문구 정리 (2026-08-02 신설)
# 8/2 14시 다원시스 건: "… → 고객 안내 준비, 소비자보호부 고객 안내 준비 요청"
# 처럼 같은 조치가 한 문장 안에 두 번 나왔다. AI가 절을 이어 붙이며 생긴
# 중복이라 프롬프트로는 완전히 막기 어렵다.
# ※ '재산출'·'즉시 산출'·'점검' 같은 일반 동사는 대상에서 제외한다. 실측에서
#    "손실 금액 재산출"과 "담보부족 계좌 재산출"이 중복으로 잡혀 서로 다른
#    조치가 삭제됐다. 대상은 그 자체로 완결된 '조치명'으로 한정한다.
_DUP_PHRASES = ("고객 안내 준비", "컴플라이언스부 보고", "컴플라이언스부 공유",
                "소비자보호부 통보")

def dedup_action_phrases(action: str) -> tuple:
    """같은 조치 표현이 2회 이상 나오면 뒤엣것을 담은 하위절을 제거."""
    if not action:
        return (action, [])
    removed = []
    clauses = action.split("→")
    for ph in _DUP_PHRASES:
        ph_ns = _NS_RE.sub("", ph)
        seen = False
        for i, c in enumerate(clauses):
            subs = c.split(",")
            keep = []
            for sub in subs:
                if ph_ns and ph_ns in _NS_RE.sub("", sub):
                    if seen:
                        removed.append(ph)
                        continue     # 중복 — 이 하위절 통째로 제거
                    seen = True
                keep.append(sub)
            clauses[i] = ",".join(keep)
    if not removed:
        return (action, [])
    out = " → ".join(c.strip().strip(",").strip() for c in clauses if c.strip().strip(","))
    out = re.sub(r'[ \t]{2,}', ' ', out).strip()
    return (out, sorted(set(removed)))

# ── 대응방안 내 익스포저 수치 제거 (2026-08-14) ──
# action_prompt는 익스포저 수치를 금지하나 AI가 반복해서 위반한다. 카드 하단에
# 채널별로 정확히 표시되는 값이라 중복이고, 문장만 길어져 정작 '무엇을 할
# 것인가'가 묻힌다. 실사례(8/14 14시·21시):
#   "보유 고객(뱅키스 12억원/1,671명·영업점 9억원/409명) 평가손 즉시 산출"
#   "보유 고객(뱅키스·영업점) 평가손 즉시 산출"   ← 부분 적용해 정보 0
# → 프롬프트에 의존하지 않고 코드에서 결정론적으로 제거한다.
# ※ 정책 임계 기준("여신 보유잔고 3억원 이상 고객")은 조치 대상을 가르는 기준선
#   이라 반드시 살려야 한다. 아래 패턴은 'N억원/N명' 슬래시쌍과 채널 라벨만
#   노리므로 '이상/미만' 형태의 임계 표현에는 걸리지 않는다.
_EXP_PAREN_RE = re.compile(r'\s*[\(（][^()（）]*(?:뱅키스|영업점)[^()（）]*[\)）]')
_EXP_PAIR_RE = re.compile(
    r'\s*(?:뱅키스|영업점|주식|여신|채권|해외주식|해외대출)?\s*'
    r'[\d,]+\s*억원?\s*/\s*[\d,]+\s*명'
)
_EXP_LONE_PAREN_RE = re.compile(r'\s*[\(（][\s·,]*[\)）]')

def prepend_entity_to_action(action: str, entity: str) -> tuple:
    """대응방안 첫머리에 대상 종목명이 없으면 붙인다. (정제문, 보강여부)

    (2026-08-19) action_prompt에 '첫 조치는 종목명으로 시작' 규칙을 넣었으나,
    실측(8/19 14시·21시)에서 모나미·한빛소프트는 종목명이 있고 듀오백·JTBC·
    한국토지신탁은 없어 회차 안에서도 표기가 갈렸다. 그룹사 기사나 다종목
    회차에서 "보유 고객 평가손 즉시 산출"만 있으면 어느 종목을 조치하라는
    것인지 문장만으로는 알 수 없다. 프롬프트 준수에 맡기지 않고 보강한다.
    """
    if not action or not entity:
        return (action or "", False)
    _head = action[:40]
    if entity in _head:
        return (action, False)
    # 약칭↔정식명(예: 한빛 ↔ 한빛소프트)도 이미 언급된 것으로 본다.
    # 6자 이상 공통 접두어 매칭은 엔티티 매칭 규칙과 같은 기준을 쓴다.
    for _tok in re.findall(r'[가-힣A-Za-z0-9]{2,}', _head):
        if len(_tok) >= 2 and (entity.startswith(_tok) or _tok.startswith(entity)):
            return (action, False)
    return (f"{entity} {action}", True)


def strip_exposure_figures(action: str) -> tuple:
    """대응방안에서 익스포저 수치 표기를 제거. (정제문, 제거여부)"""
    if not action:
        return (action, False)
    out = _EXP_PAREN_RE.sub('', action)      # 채널 라벨이 든 괄호구 통째
    out = _EXP_PAIR_RE.sub('', out)          # 괄호 밖 'N억원/N명' 쌍
    out = _EXP_LONE_PAREN_RE.sub('', out)    # 수치가 빠져 홀로 남은 빈 괄호
    if out == action:
        return (action, False)
    out = re.sub(r'\s{2,}', ' ', out)
    out = out.replace(' ,', ',').replace(' .', '.').replace(' →', ' →')
    return (out.strip(), True)

# ── 고객문구 문장 경계 절단 (2026-08-16 신설) ──
# 기존 렌더는 notice[:200] + "..." 로 하드컷해 문장 한가운데서 끊겼다.
#   실사례(8/15 07시 엑시큐어하이트론·엔지켐생명과학 2건 모두):
#   "…공시 내용은 KIND(kin..." — 담당자가 고객에게 복사해 쓰는 문구인데
#   미완결이라 그대로는 사용할 수 없다.
# → 상한 이내의 '마지막 문장 끝'에서 자른다. 잘려도 항상 완결된 문장이 남는다.
_SENT_END_RE = re.compile(r'(?<=[.!?])\s|(?<=다\.)\s*|(?<=요\.)\s*')

def truncate_at_sentence(text: str, limit: int = 200) -> str:
    """limit 이내에서 마지막 문장 경계로 절단. 경계가 없으면 어절 경계로 폴백.

    (2026-08-16) 절단 발생 시 관측 로그를 남긴다. 프롬프트 190자 상한을 이번에
    처음 넣은 터라 준수율이 미측정이다. 상한이 지켜지면 절단은 거의 일어나지
    않으므로, 빈도를 재보고 추가 대응(행동유도 우선 보존 등) 필요 여부를
    데이터로 판단하기 위한 계측이다. 함수 속성에 누적해 회차 요약에서 읽는다.
    """
    truncate_at_sentence.total = getattr(truncate_at_sentence, "total", 0) + 1
    if not text or len(text) <= limit:
        return text or ""
    truncate_at_sentence.truncated = getattr(truncate_at_sentence, "truncated", 0) + 1
    head = text[:limit]
    # 1순위: 문장 종결 위치.
    # 주의(2026-08-16 수정): 단순히 [.!?]를 찾으면 "kind.krx.co.kr" 같은 도메인의
    # 점을 문장 끝으로 오인해 "…KIND(kind.krx.co." 로 잘린다 — 고치려던 증상이
    # 그대로 재현됐다. 고객문구에는 KIND·DART 주소가 거의 항상 들어가므로
    # 한국어 공문체 종결어미('…습니다.', '…바랍니다.')를 우선 기준으로 삼고,
    # 그다음 '문장부호 + 공백/문자열끝'만 인정한다. 도메인의 점은 뒤에 곧바로
    # 문자가 붙으므로 두 기준 모두에서 걸러진다.
    _ends = [m.end() for m in re.finditer(r'(?:다|요|음|함)[.!?]["\')\]]?(?=\s|$)', head)]
    if not _ends:
        _ends = [m.end() for m in re.finditer(r'[.!?]["\')\]]?(?=\s|$)', head)]
    if _ends and _ends[-1] >= limit * 0.5:
        out = head[:_ends[-1]].strip()
        # 행동 유도가 절단으로 사라졌는지 — 잘라낸 뒤쪽에만 있으면 소실이다
        _tail = text[len(out):]
        if re.search(r'확인하시|점검하시|문의|바랍니다', _tail) and not re.search(
                r'확인하시|점검하시|바랍니다', out):
            truncate_at_sentence.action_lost = getattr(
                truncate_at_sentence, "action_lost", 0) + 1
            print(f"  [고객문구 절단·행동유도 소실] {len(text)}자 → {len(out)}자")
        else:
            print(f"  [고객문구 절단] {len(text)}자 → {len(out)}자")
        return out
    # 2순위: 어절 경계 — 문장부호가 없거나 너무 앞이면 단어 중간 절단만 피한다
    _sp = head.rfind(' ')
    if _sp >= limit * 0.5:
        print(f"  [고객문구 절단·어절경계] {len(text)}자 → {_sp}자")
        return head[:_sp].rstrip() + "…"
    print(f"  [고객문구 절단·강제] {len(text)}자 → {limit}자")
    return head.rstrip() + "…"

def strip_unsupported_action_clauses(action: str, exp_rows: list) -> tuple:
    """익스포저에 존재하지 않는 유형의 조치 문구를 제거. (정제문, 제거내역)"""
    if not action:
        return (action, [])
    def _sum(types):
        t = 0.0
        for r in exp_rows or []:
            if r.get("종목유형", "") in types:
                try:
                    t += _num(r.get("잔고(억)"))
                except (ValueError, TypeError):
                    pass
        return t
    removed, out = [], action
    _yeosin = _sum(_YEOSIN_TYPES_ACT)
    if _yeosin < _OB_MIN_YEOSIN:
        _new = _OB_CLAUSE_RE.sub('', out)
        if _new != out:
            removed.append("OB인계(여신 익스포저 없음/10억 미만)")
            out = _new
    # 신용융자·담보비율·반대매매 등은 여신 잔고가 '아예 없을' 때만 제거한다.
    # 10억 미만이어도 잔고가 있으면 담보비율 점검 자체는 유효한 조치다.
    if _yeosin <= 0:
        # 절(→) 안에서 '및'·','로 한 단계 더 쪼개 하위절 단위로 판정한다.
        # 절 전체를 지우면 같은 절에 붙어 있는 주식 관련 유효 조치까지 사라진다.
        #   실사례(8/3 JR리츠): "보유 주식 고객 평가손 산출 및 담보대출 보유 고객
        #   담보비율 긴급 점검" — 앞부분은 주식 138억에 대한 유효 조치다.
        _clauses, _changed = [], False
        for _c in out.split("→"):
            _subs = re.split(r'(?<=[^\s])\s*(?:및|,)\s*', _c)
            _keep = [x for x in _subs if not _is_yeosin_dependent_clause(x)]
            if len(_keep) != len(_subs):
                # 하위절을 지운 뒤 남은 꼬리가 서술어 없이 명사로 끝나면 문장이
                # 깨진다. "A 보유 고객 평가손 및 담보계좌 점검"에서 뒤를 지우면
                # "A 보유 고객 평가손"만 남아 무엇을 하라는 건지 사라진다.
                # 이런 경우엔 제거를 포기하고 원문을 유지한다 — 실행 불가 조치가
                # 남는 것보다 문장이 깨지는 쪽이 더 나쁘다.
                _tail = (_keep[-1].strip() if _keep else "")
                if _keep and not _ACTION_VERB_RE.search(_tail):
                    _clauses.append(_c)
                    continue
                _changed = True
            if _keep:
                _clauses.append(", ".join(x.strip() for x in _keep if x.strip()))
        if _changed:
            removed.append("신용거래 조치(여신 잔고 없음)")
            out = " → ".join(c.strip() for c in _clauses if c.strip())
    if _sum(_STOCK_TYPES_ACT) <= 0:
        _new = _NOTICE_CLAUSE_RE.sub('', out)
        if _new != out:
            removed.append("고객안내(주식 익스포저 없음)")
            out = _new
    if removed:
        out = re.sub(r'\s*(?:→|,|·)\s*$', '', out.strip())   # 끝에 남은 연결기호 정리
        out = re.sub(r'^\s*(?:→|,|·)\s*', '', out)           # 문두에 남은 연결기호 정리
        out = re.sub(r'\s*→\s*(?=→)', '', out)               # 연속 화살표 축약
        out = re.sub(r'[ \t]{2,}', ' ', out).strip()
    return (out, removed)

def find_exposure(entity: str, exposure_data: dict) -> list:
    """entity와 종목명 딕셔너리 매칭 — O(1) 정확 매칭 우선, fallback prefix 6자
    성능 최적화: 정확 매칭(O1) → 부분포함 문자열(O(n)) → prefix 6자(O(n))
    2만행에서도 정확 매칭은 즉시, 미등록 종목도 빠르게 반환
    정확 매칭이 있어도, entity를 포함하는 다른 종목명(법인명 표기차이: "중앙일보"↔"중앙일보(주)",
    "에스엘엘중앙"↔"에스엘엘중앙 주식회사")이 있으면 함께 병합 — 누락 방지
    """
    if not entity or not exposure_data:
        return []

    results = []
    seen_names = set()

    # 1) 정확 매칭 — O(1). 있어도 즉시 반환하지 않고 2)에서 추가 매칭 계속 탐색
    if entity in exposure_data:
        results.extend(exposure_data[entity])
        seen_names.add(entity)
    else:
        # 1.5) 영문/약어 별칭 변환 후 재시도 (예: JTBC → 제이티비씨) — 검증된 사전, 최우선
        for alias, kor in ENTITY_ALIAS_MAP.items():
            if entity.upper().startswith(alias):
                converted = kor + entity[len(alias):]
                if converted in exposure_data:
                    results.extend(exposure_data[converted])
                    seen_names.add(converted)
                    break
                results_alias = find_exposure(converted, exposure_data)
                if results_alias:
                    return results_alias

        # 1.6) 사전에 없는 영문 약어 — 알파벳 음역 fallback (예: NEW123 → 엔이더블유...)
        if not results and re.match(r'^[A-Za-z]', entity):
            translit = _alpha_to_korean(entity)
            if translit != entity.upper():
                if translit in exposure_data:
                    results.extend(exposure_data[translit])
                    seen_names.add(translit)
                else:
                    results_translit = find_exposure(translit, exposure_data)
                    if results_translit:
                        return results_translit

    # 2) 부분포함 + prefix 6자 — entity가 name에 포함되거나 그 반대
    #    정규식 컴파일 1회만 수행 (루프 밖)
    clean_e = re.sub(r'[(주)㈜\s]', '', entity)
    ce_len = len(clean_e)

    for name, rows in exposure_data.items():
        if name in seen_names:
            continue

        # 접두(prefix) 매칭 — 법인명 표기차이(중앙일보↔중앙일보(주))만 허용.
        # 접미 부분일치(마이크론⊂하나마이크론) + 짧은 무관명 흡수(에스엘⊂에스엘엘중앙,
        # 제이티⊂제이티비씨) 오매칭 차단 → 접두 관계라도 짧은 쪽이 5자 미만이면 정확일치만 허용
        _clean_n = re.sub(r'[(주)㈜\s]', '', name)
        if _clean_n:
            if _clean_n == clean_e:
                results.extend(rows); seen_names.add(name); continue
            _short = min(len(_clean_n), ce_len)
            if _short >= 5 and (_clean_n.startswith(clean_e) or clean_e.startswith(_clean_n)):
                results.extend(rows); seen_names.add(name); continue

        # prefix 6자 매칭 — 법인명 축약 대응 (제이알글로벌리츠 ↔ 제이알글로벌위탁관리...)
        if ce_len >= 4:
            clean_n = re.sub(r'[(주)㈜\s]', '', name)
            if len(clean_n) >= 4:
                # 공통 접두 길이 계산
                plen = 0
                for a, b in zip(clean_e, clean_n):
                    if a == b:
                        plen += 1
                    else:
                        break
                if plen >= 6:
                    # 오매칭 가드: 공통 접두 6자 이상이어도, 짧은 쪽 이름 전체가
                    # 공통 접두에 거의 포함될 때만 축약 관계로 인정한다.
                    # (제이알글로벌리츠[7자]는 접두 6자가 이름의 86% → 축약 인정,
                    #  제이알글로벌인베스트먼트[12자]는 접두 6자가 50% → 다른 회사)
                    _short_len = min(ce_len, len(clean_n))
                    if _short_len > 0 and (plen / _short_len) >= 0.75:
                        results.extend(rows)
                        seen_names.add(name)

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
                    if _kw_hit(title, CREDIT_KEYWORDS):
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
              <td align="right"><span style="font-size:12px;color:#94a3b8;">{today_str} 당일 기준</span></td>
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
    """최근 3일 키(YYYY-MM-DD HH) 기준 seen URL 로드 — 오래된 키 자동 제거
    (2026-07 패치: 24시간→7일로 확대. load_seen_stages()와 보존기간을 맞춰,
    known_cases 진행 중 사건의 파생기사가 3일 이상 지나 URL이 바뀌면 dedup을
    빠져나가 참고 등급으로 매일 재노출되던 문제 해결. 저장(save_seen_urls)은
    이미 7일 보존이라 조회만 좁았던 불일치를 바로잡음.)"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(SEEN_RETENTION_HOURS)
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
    """최근 3일 내 발송된 (entity, keyword) 조합 로드 (2026-07-24: 7일→3일 단축)"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(SEEN_RETENTION_HOURS)
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

def load_seen_stages() -> set:
    """최근 3일 내 발송된 (entity, stage_keyword) 조합 로드 — stage 기반 dedup용"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(SEEN_RETENTION_HOURS)  # 3일 — 파산선고 등 단계 보도는 수일간 지속
    }
    stages = set()
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return stages
        if not isinstance(data, dict):
            return stages
        for k in valid_keys:
            entry = data.get(k, {})
            if isinstance(entry, dict):
                for st in entry.get("stages", []):  # 구버전 슬롯엔 없음 → 빈 처리 (하위호환)
                    if isinstance(st, (list, tuple)) and len(st) == 2:
                        stages.add((st[0], st[1]))
    return stages

def load_seen_context() -> dict:
    """최근 3일 내 발송된 기사의 title_norms·desc_norms 로드 — 맥락 기반
    중복 감지 (2026-07-24: 7일→3일 단축)"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(SEEN_RETENTION_HOURS)
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


def load_seen_entities_today() -> set:
    """당일(KST 날짜 기준) 발송된 entity 목록 로드 — 동일 entity 1일 1건 제한용"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    today = now.strftime("%Y-%m-%d")
    today_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(24)
        if (now - timedelta(hours=i)).strftime("%Y-%m-%d") == today
    }
    entities = set()
    if not os.path.exists(SEEN_FILE):
        return entities
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return entities
        for k in today_keys:
            entry = data.get(k, {})
            for combo in entry.get("combos", []):
                if isinstance(combo, (list, tuple)) and len(combo) == 2:
                    if combo[0] != "ek":
                        entities.add(combo[0])
    except Exception:
        pass
    return entities


def load_known_entities() -> dict:
    """최근 3일 seen_news에서 entity별 최초 발송일(days_ago) 계산
    반환: {entity: days_ago}  — days_ago=0: 오늘, 1: 어제, ...
    강등 기준: days_ago >= 3 → 등급 1단계 강등
    차단 기준: days_ago >= 7 → 완전 차단 (NEXT_STAGE 예외 유지)
    """
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    # 최대 3일 = 72시간치 슬롯 로드
    entity_first: dict = {}  # {entity: 최초 발송 days_ago}
    if not os.path.exists(SEEN_FILE):
        return entity_first
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return entity_first
        for i in range(SEEN_RETENTION_HOURS):
            slot_dt = now - timedelta(hours=i)
            slot_key = slot_dt.strftime("%Y-%m-%d %H")
            days_ago = i // 24
            entry = data.get(slot_key, {})
            for combo in entry.get("combos", []):
                if isinstance(combo, (list, tuple)) and len(combo) == 2:
                    if combo[0] != "ek":
                        ent = combo[0]
                        # 가장 오래된 발송일 추적 (최솟값 = 최초)
                        if ent not in entity_first or days_ago > entity_first[ent]:
                            entity_first[ent] = days_ago
    except Exception:
        pass
    return entity_first

def save_seen_urls(seen: set, combos: set = None, title_norms: list = None, desc_norms: list = None,
                   stages: set = None):
    """현재 시각 키(YYYY-MM-DD HH)로 seen URL + 발송 조합 저장 — 최근 3일 키만 보존"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    current_key = now.strftime("%Y-%m-%d %H")
    valid_keys = {
        (now - timedelta(hours=i)).strftime("%Y-%m-%d %H")
        for i in range(SEEN_RETENTION_HOURS)  # 3일 = 72시간 보존 (강등·차단 이력 유지)
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
    existing_stages = [tuple(x) for x in cur.get("stages", [])]
    if stages:
        for st in stages:
            if tuple(st) not in existing_stages:
                existing_stages.append(tuple(st))
    existing_stages = existing_stages[-100:]
    existing[current_key] = {
        "urls":        merged_urls,
        "combos":      existing_combos,
        "title_norms": existing_titles,
        "desc_norms":  existing_descs,
        "stages":      [list(x) for x in existing_stages],
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
    """네이버 검색 API로 뉴스 수집 — 최근 14시간 기사만.
    사유: 실행 간 공백 구간의 기사가 어느 실행에도 안 걸려 영구 누락되는
    사각지대를 없애기 위해, 최대 실행 간격보다 넉넉한 창을 쓴다.
    스케줄 이력:
      - 기존 07/12/17시: 최대 간격 14시간(17시→익일 07시) → 6시간 창일 때
        17시~익일01시(8시간) 누락 확인(7/23 실측) → 14시간으로 확대
      - 현행 07/14/21시(2026-07-24 변경): 최대 간격 10시간(21시→익일 07시)
        이므로 14시간 창이면 4시간 여유를 두고 전 구간 커버
    창이 넓어 재수집이 발생하지만, seen_urls 기반 URL 중복제외가 크롤링
    직후(AI 필터 이전)에 걸려 이미 처리된 기사는 비용 없이 스킵되므로
    실질 비용 증가는 미미함(Gemini/Claude 2차 비용은 신규 URL만 발생)."""
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    cutoff_kst = now_kst - timedelta(hours=14)
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

        for item in items:
            pub_date_str = item.get("pubDate", "")
            try:
                pub_dt = _pdt(pub_date_str).astimezone(kst)
                pub_date = pub_dt.date()
            except Exception:
                pub_dt = now_kst
                pub_date = today_kst

            if pub_dt < cutoff_kst:
                # stop 플래그 제거 — API가 날짜 비순서로 올 수 있으므로 skip만
                continue

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
        if len(items) < 100 or start >= 301:
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
    "시황", "장마감", "마감 시황", "마감 종합", "마켓 마감", "마켓 시황",
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
    # 실적 축약 표기 — "순익 1700억"처럼 '순이익'이 아닌 축약형으로 쓰면
    # 기존 패턴을 빠져나갔음 (7/25 "하나증권, 전부문 성장에 2분기 순익
    # 1700억…ROE 개선" 참고 3.8 오탐 실사례)
    # ※ '순익' 단독은 등재 금지 — "순익 급감에 신용등급 강등"처럼 진짜
    #   리스크 기사까지 차단하는 미탐이 발생함(검증에서 확인). 반드시
    #   긍정 방향이 확정된 표현만 등재한다.
    "ROE 개선", "ROE 상승", "전부문 성장", "전 부문 성장",
    "이익 증가", "이익 급증", "최대 실적", "최고 실적", "역대급 실적",
    # 인가·승인·선정 등 사업 확장 호재 (7/25 "영광의 IMA…한국금융지주"
    # 참고 4.0 오탐 — IMA(종합투자계좌) 인가는 신규 사업 획득으로 호재)
    "인가 획득", "인가 취득", "본인가", "예비인가", "라이선스 획득",
    "사업자 선정", "우선협상대상자", "수상", "대상 수상",
    # ── 조달 흥행·성과 호재 (7/26 14시 참고 5건 전수 오탐 실사례) ──
    # 회사채 증액은 수요예측 흥행(=조달 성공)이라 호재. 미매각·미달과 반대.
    "증액 확정", "증액 발행", "수요예측 흥행", "완판", "전량 매각",
    "어닝서프라이즈", "어닝 서프라이즈", "실적 서프라이즈",
    "수익률 상위", "수익률 1위", "수익률 톱", "상위권 싹쓸이", "수익률 선두",
    # "깜짝 실적"은 _POSITIVE_PATS에 등재해 부정어 동반 시 면제되게 한다
    # ("인텔 깜짝 실적에도 -7.89% 급락" 미탐 방지 — 검증에서 확인)
    "깜짝 실적",
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
    "위기 탈출", "위기탈출", "거래정지 해제", "거래재개", "거래 재개", "매매거래 정지 해제",
    "액면병합", "주권 변경상장",
    "사상 최고가", "역대 최고가", "신 최고가",                       # 호재 최고가 기사
    "상장폐지 예정", "존속기한 만료", "만기 상장폐지", "만기 해지",   # ETF 정상 만기 해지
    "상장폐지 예고", "상장폐지 결정 ETF", "ETF 상장폐지",           # ETF 정상 만기
    "재개 후 ", "재개 뒤 ", "거래 재개 후", "거래재개 후",           # 거래재개 이후 주가 기사
    "전망", "소식",
    "(完)", "(완)", "①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
    "현직이 푸는", "전문가가 보는", "기자가 간다",
    # 개인 칼럼·투자조언 코너 — 실측: '"...비중 축소 권한다"[경제적본능]'
    "[경제적본능]", "[투자노트]", "[마켓인사이트]", "[머니무브]",
    "[재테크]", "[증시전망]", "권한다\"",
    # 회고·역사 정리성 기사 — 실측: "파산 문턱서 나스닥까지…되짚은 SK하이닉스 25년"
    "되짚은", "되짚어", "돌아본 ", "돌아보는", "지나온 길", "그간의 여정",
    "후보", "공약", "선거", "시의원", "구의원", "도의원", "국회의원", "시장 출마", "당선",
    "복합문화", "재개발", "부지 활용", "도시재생", "리모델링",
]

TEXT_PATTERNS = [
    "분석", "리포트", "보고서", "추천",
    "인터뷰", "기획", "특집", "르포", "칼럼", "오피니언", "사설", "논설",
]

TITLE_ONLY_PATTERNS += [
    "기자수첩", "기자의 눈", "기자노트", "취재후기", "현장에서", "데스크에서",
    "잡아낸", "변호사", "법률가", "판사", "부장검사", "사례로 보는", "이야기",
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

    # ── 법조계·칼럼·책임 논쟁 기사 ──
    "법조계", "책임져라", "법적 책임", "배상 책임", "법원 판결",

    # ── 지역 문화·협회·단체·인물 선출 기사 — 금융 리스크 무관 ──
    "협회장", "협회 회장", "이사장 선출", "이사장 취임", "회장 체제",
    "영화인협회", "문화예술", "문화재단", "예술인",
    "지역 영화", "지역 문화", "지역 경제",

    # ── 리스크 해소·정상화 표현 — 현재 리스크 없음 ──
    "딛고 정상화", "위기 극복", "정상화 추진", "정상 궤도",
    "혼란 딛고", "수습 완료", "안정화 완료",

    # ── 개인·전문가 정책의견 기사 — 금융 리스크 직접 발생 없음 ──
    "공익 위해", "제도 개선 촉구", "입법 필요", "법안 발의",

    # ── 개인 투자 손실 스토리·칼럼 기사 — 직접 리스크 아님 ──
    "투자의 귀재", "감 잃어버린", "폭락에 우는", "개미들의 눈물",
    "주식 망했다", "투자 실패", "개인 투자자 손실",
    # ── 재계·그룹 비교 칼럼 브래킷 — 특정 기업 직접 리스크 아님 ──
    "[금...", "[금융...", "[금주",

    # ── 방송·연예 콘텐츠 — 시청률·드라마·예능 성과 기사 ──
    "시청률", "드라마 흥행", "예능 흥행", "반전 활약", "제작발표회",
    "OST 공개", "시사회", "출연 확정", "촬영 시작",

    "레거시 미디어", "종언", "미디어 시대",
    "언론사 위기", "신문사 위기", "방송사 부진",

    # ── 유통·소비재 시황 분석 — 당사 직접 익스포저 없음 ──
    "대형마트 우는", "대형마트 부진", "대형마트 위기",
    "쿠팡만 웃는", "영업규제", "의무휴업",

    # ── 증시·시장 구조 분석 시리즈 기사 — 개별 종목 리스크 아님 ──
    "[코스닥 모멘텀]", "[증시 모멘텀]", "[마켓 모멘텀]",
    "2부 리그", "꼬리표는 어디서",
    "생존 실험", "코스닥의 꿈", "30살 코스닥", "코스닥 30주년",
    "[증권 NOW]", "[증권가 NOW]",

    # ── M&A·사업 확장·계열사 편입 — 직접 손실 리스크 없음 ──
    "품고 키운다", "판 키운다", "유통·제조 한지붕",
    "완전자회사화 본격", "자회사로 품",

    # ── 감성·낚시성 주가하락 제목 — 거래정지·상폐 확정 사건 아님 (07/03 오탐) ──
    "바닥이 어디냐", "어디까지 빠지나", "눈물의", "피눈물", "곡소리",
    "패닉", "공포에 질린", "던졌다", "물렸다", "물린 개미",
    "5개월만에", "반토막", "반 토막", "토막 났다",
    # ── 재무제표 해설·분석 기사 — 확정 손실 사건 아님 (07/03 CMG제약 오탐) ──
    "적신호", "빨간불 켜진", "현금흐름", "수익성 둔화", "외형 성장 이면",
    "재무 위험 신호", "재무 건전성 점검", "실적 뜯어보니",
    # ── 상장폐지 저지 응원매수·테마주 강세 — "상장폐지" 등 CRITICAL_KW 동반해도 호재
    #    (7/19 "상장폐지 위기 기업 '돈쭐내서 살리자'…맛살·볼펜 구매" 오탐 실사례.
    #    CRITICAL_EXEMPT만으로는 AI 판단으로 넘어갈 뿐 자동 제외되지 않아 별도 등재) ──
    "돈쭐", "응원매수", "저지 응원", "막자 매수", "애국테마", "애국 테마",

    # ── 해외법인 M&A 완료·사명변경(리브랜딩) — 상장폐지 아닌 재편, 익스포저
    #    확정 전까지 호재 취급 (7/19 "골드그룹·GRC 합병 완료…'GORO'로 재탄생" 오탐 실사례) ──
    "합병 완료", "로 재탄생", "새 이름으로",
    # ── 완전자회사 편입·주식교환에 의한 자진 상장폐지 — 손실 리스크 아님
    #    (7/23 "우리금융 동양생명 편입 본궤도"가 주의로, "이마트 신세계푸드
    #    완전자회사 편입 완료"가 참고로 발송된 오탐 실사례. 주식교환으로
    #    모회사 주식을 받는 구조라 강제청산·손실과 무관) ──
    "완전자회사", "자회사 편입", "편입 완료", "편입 본궤도", "주식교환일",

    # ── 기지사건 파급·업계 영향 해설 — 확정 종목 파생 (홈플러스·중앙그룹) ──
    "떠는", "술렁이는", "긴장하는", "운명의 날", "갈림길",
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
    # 칼럼·오피니언·시각 브래킷 — 산업 트렌드 분석 기사
    r"\[현장의\s*시각\]",
    r"\[시각\]",
    r"\[오피니언\]",
    r"\[기고\]",
    r"\[데스크\s*시각\]",
    r"\[현장\s*르포\]",
    # 증시·시장 구조 분석 시리즈 — 개별 종목 리스크 아님
    # 단, 뒤에 상장폐지·파산·부도·거래정지 등 리스크 키워드가 있으면 통과
    r"\[코스닥\s*(모멘텀|분석|전망|체크|진단)\].{0,20}(?<!상장폐지)(?<!파산)(?<!부도)(?<!거래정지)$",
    r"\[코스피\s*(모멘텀|분석|전망|체크|진단)\]",
    r"\[증시\s*(모멘텀|분석|전망|체크|진단)\]",
    r"\[마켓\s*(모멘텀|분석|전망|체크|진단)\]",
    r"\[N2\s*증시\s*풍향계\]",   # [N2 증시 풍향계]만 차단, [N2 증시]는 제외
    # 재계·그룹 비교 칼럼 브래킷 — 특정 기업 직접 리스크 아님
    r"\[금\.\.\.\]",             # [금...] 시리즈 칼럼
    r"\[금\s*[가-힣]{1,6}\]",   # [금융] [금주] 등 브래킷
    # 코너·데스크·시각성 브래킷 — 개별 기명 코너 칼럼 (07/03 [이런국장] 오탐)
    # 상장폐지·파산·부도·거래정지 리스크 키워드가 없을 때만 차단
    r"\[[가-힣]{2,10}(국장|부장|칼럼|노트|시선|픽|레터|톡)\](?!.*(상장폐지|파산|부도|거래정지|회생|반대매매))",
]

# ── 공백 무시 매칭용 사전계산 ──────────────────────────────────────────
# is_hard_excluded()가 호출될 때마다 426개 패턴을 정규화하면 비용이 크므로
# 모듈 로드 시 1회만 (원본, 공백제거본) 쌍으로 만들어 둔다.
_NS_RE = re.compile(r"\s+")
_TITLE_PATTERNS_NS = [(p, _NS_RE.sub("", p)) for p in TITLE_ONLY_PATTERNS]
_TEXT_PATTERNS_NS  = [(p, _NS_RE.sub("", p)) for p in TEXT_PATTERNS]


# 공백 무시 매칭에서 제외할 짧은 패턴 —
# 2~3글자 패턴은 공백을 제거하면 인접 단어와 붙어 우연히 매칭된다.
# 실측: "OO사 자금난" → "OO사자금난"에 '사자'(개미·사자 매매 패턴)가 걸려
# 진짜 리스크 기사가 차단되는 미탐 발생(2026-07-28).
# 이런 패턴은 원문 그대로만 비교한다.
_NS_EXEMPT = frozenset({
    "사자", "팔자", "개미", "외인", "시총", "번 ", "만든 ", "모은 ", "불린 ",
    "인사", "부고", "승진", "선임", "취임", "퇴임", "수주", "협약", "수상",
})


def _kw_hit(text: str, keywords) -> bool:
    """키워드 포함 여부 — 공백 무시 비교.

    한국어 기사 제목은 같은 표현도 띄어쓰기가 제각각이라("전산 장애" vs
    "전산장애", "완전 자회사" vs "완전자회사") 원문 비교만으로는 같은 사건이
    다르게 판정된다. 실측으로 다음 문제가 확인돼 전 매칭 지점을 이 헬퍼로
    통일한다:
      · 하드제외 패턴 426개 중 72개가 공백 변형에 취약
      · calc_risk_score의 kw_weight가 갈려 같은 사건이 5.0점 vs 8.2점
    """
    if not text:
        return False
    t_ns = _NS_RE.sub("", text)
    for k in keywords:
        if k in text:
            return True
        if k not in _NS_EXEMPT and _NS_RE.sub("", k) in t_ns:
            return True
    return False


def _num(v, default: float = 0.0) -> float:
    """'1,234'·''·None·'-' 등을 안전하게 float으로. 실패 시 default.

    exposure_data는 대직자가 엑셀에서 변환해 올리는 외부 데이터라 빈 값·
    하이픈·결측이 섞일 수 있다. 기존엔 float(str(v).replace(",","")) 를
    그대로 써서 빈 문자열 하나만 들어와도 점수 계산과 이메일 생성이
    ValueError로 죽었다(실측 확인).
    """
    if v is None:
        return default
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s and s not in ("-", "—", "N/A", "nan") else default
    except (TypeError, ValueError):
        return default


def _kw_hits(text: str, keywords) -> list:
    """_kw_hit의 목록 반환판 — 매칭된 키워드를 전부 돌려준다."""
    if not text:
        return []
    t_ns = _NS_RE.sub("", text)
    return [k for k in keywords
            if k in text or (k not in _NS_EXEMPT and _NS_RE.sub("", k) in t_ns)]


# ── 기지 사건 동적 주입 — known_cases.json → 프롬프트 __KNOWN_CASES__ 치환 ──
_KNOWN_CASES_FALLBACK = (
    "  · JTBC·중앙그룹(중앙홀딩스·콘텐트리중앙·메가박스중앙·에스엘엘중앙·중앙일보) 기업회생 신청 (2026-06 확정)\n"
    "  · 홈플러스 기업회생 → 회생절차 폐지 (2025 회생, 2026-07 폐지 결정)\n"
    "  · 금양 상장폐지 결정 (2026-06 확정)"
)

def render_known_cases() -> str:
    """known_cases.json → 프롬프트 삽입용 목록 텍스트. 실패 시 폴백 상수."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_cases.json")
    try:
        with open(path, encoding="utf-8") as f:
            cases = json.load(f)
        if not isinstance(cases, list) or not cases:
            return _KNOWN_CASES_FALLBACK
        lines = []
        for c in cases:
            if not isinstance(c, dict) or not c.get("entity"):
                continue
            line = f"  · {c['entity']} — {c.get('event','')} ({c.get('date','')}"
            if c.get("stage"):
                line += f", 현재: {c['stage']}"
            line += ")"
            lines.append(line)
        return "\n".join(lines) if lines else _KNOWN_CASES_FALLBACK
    except Exception:
        return _KNOWN_CASES_FALLBACK


def load_known_case_entities() -> set:
    """known_cases.json → 강등 대상 개별 종목명 집합.
    entity 필드가 'JTBC·중앙그룹(콘텐트리중앙·메가박스중앙·...)' 형태로
    복수 종목을 묶어 표기하므로, 구분자(·, (), 공백)로 분해해 개별 종목명을 추출.
    이 집합의 종목이 등장하는 기사는 신규 법적단계(NEXT_STAGE)가 아닌 한
    기지 사건 파생으로 간주해 처음부터 강등한다.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_cases.json")
    ents: set = set()
    try:
        with open(path, encoding="utf-8") as f:
            cases = json.load(f)
        if not isinstance(cases, list):
            return ents
        for c in cases:
            if not isinstance(c, dict):
                continue
            raw = c.get("entity", "") or ""
            # 괄호 안팎 모두 분해: 'JTBC·중앙그룹(중앙홀딩스·콘텐트리중앙)' → 토큰들
            raw = raw.replace("(", "·").replace(")", "·").replace(",", "·")
            for tok in raw.split("·"):
                tok = tok.strip()
                # '중앙그룹'처럼 그룹 총칭은 개별 매칭 오탐 소지 → 2자 이상 실종목만
                if len(tok) >= 2 and tok not in ("그룹", "계열사", "중앙그룹"):
                    ents.add(tok)
    except Exception:
        pass
    return ents


def load_entity_canonical_map() -> dict:
    """known_cases.json → {별칭: 대표명} 매핑.

    같은 사건인데 AI가 실행마다 다른 별칭(JTBC/제이티비씨/중앙일보)을 entity로
    뽑으면, combo dedup의 키 (entity, event_type)가 매번 달라져 같은 사건이
    중복 발송된다(2026-07-10 12시 실측: 07시엔 '중앙일보', 12시엔 'JTBC'로
    잡혀 동일 워크아웃 결정 기사가 중복 발송됨).

    이 함수는 known_cases.json의 'A·B·C(D·E)' 별칭 그룹에서 첫 토큰을
    대표명으로 삼아, 그룹 내 모든 별칭을 대표명으로 되돌리는 맵을 만든다.
    dedup 시 entity를 이 맵으로 정규화한 뒤 combo를 생성하면, 별칭이
    달라도 같은 사건으로 인식된다.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_cases.json")
    canon: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            cases = json.load(f)
        if not isinstance(cases, list):
            return canon
        for c in cases:
            if not isinstance(c, dict):
                continue
            raw = c.get("entity", "") or ""
            raw = raw.replace("(", "·").replace(")", "·").replace(",", "·")
            toks = [t.strip() for t in raw.split("·") if t.strip()]
            toks = [t for t in toks if t not in ("그룹", "계열사", "중앙그룹") and len(t) >= 2]
            if not toks:
                continue
            representative = toks[0]  # 그룹의 첫 토큰을 대표명으로
            for t in toks:
                canon[t] = representative
    except Exception:
        pass
    return canon


def canonicalize_entity(entity: str, canon_map: dict, exposure_data: dict = None) -> str:
    """dedup combo 키 생성 전 entity를 대표명으로 정규화.

    [1] known_cases.json 별칭 그룹 (JTBC/중앙일보 등)
    [2] (2026-08-13 추가) 익스포저 종목명 — known_cases에 없는 일반 종목의
        표기 흔들림을 잡는다. 실사례(8/13 14시): 같은 IS동서 거래정지 사건이
        entity 'IS동서'와 '아이에스동서'로 갈려 dedup을 통과, 주의 2건으로
        중복 발송됐다(익스포저 카드는 둘 다 '아이에스동서'로 동일 표시).
        find_exposure()가 두 표기를 모두 같은 종목행으로 해소하므로, 그
        종목명을 대표명으로 삼으면 별칭을 따로 등재하지 않아도 정규화된다.
        여러 종목이 걸리면 모호하므로 단일 종목으로 해소될 때만 적용한다.
    """
    if not entity:
        return entity
    if entity in canon_map:
        return canon_map[entity]
    if exposure_data:
        try:
            rows = find_exposure(entity, exposure_data)
        except Exception:
            rows = []
        names = {(r.get("종목명") or "").strip() for r in rows}
        names.discard("")
        if len(names) == 1:
            return names.pop()
    return entity


# ── 리스크 '해소' 국면 판정 (2026-07-31 신설) ──
# 기존 해소 가드(_RESOLVE_KW)는 dedup 경로(is_next_stage)에만 있어, 등급 판정에는
# 적용되지 않았다. 실사례(7/30 21시): "[속보] 대진첨단소재, 상장폐지 절차 일시
# 보류…법원 가처분 결정까지"가 주의 7.3점으로 발송됐다 — 상폐가 '중단'된,
# 리스크 완화 방향 기사다.
#
# ※ 하드 제외가 아니라 '참고 강등'으로 처리한다. 상폐 절차가 보류됐어도 해당
#   종목은 여전히 상폐 위기 국면이고 당사 익스포저도 있어, 완전 배제하면 미탐이
#   된다. 긴급·주의 발송만 막고 노출은 유지하는 것이 맞다.
#   (경쟁사 리스크 처리와 동일 원칙 — 익스포저 있으면 참고로 남긴다)
_RISK_NOUN_RE = r'(?:상장\s*폐지|상폐|거래\s*정지|매매거래\s*정지|회생\s*절차|파산\s*절차|청산\s*절차|워크아웃)'
_RESOLVE_VERB_RE = r'(?:보류|중단|유예|연기|철회|취소|해제|면제|모면|종결|졸업|백지화|무산)'
# "유예기간 종료"처럼 해소어 뒤에 종료·만료가 붙으면 오히려 악화 — 제외
_RESOLVED_RE = re.compile(
    _RISK_NOUN_RE + r'[^,\.·…]{0,12}?' + _RESOLVE_VERB_RE +
    r'(?!\s*(?:기간)?\s*(?:종료|만료|끝|해제되|이후))'
)
# 해소 기사처럼 보여도 아래가 함께 있으면 실질 리스크 진행 중 — 강등 면제
_STILL_ADVERSE_KW = ("정리매매", "상폐 확정", "상장폐지 확정", "파산 선고", "회생 개시",
                     "회생절차 개시", "부도", "디폴트", "채무불이행", "감사의견 거절",
                     "상장폐지 결정", "퇴출 확정")

def is_risk_resolved(title: str) -> bool:
    """제목이 '리스크 절차의 중단·철회·해제' 국면인지. 참고 강등 판정용."""
    if not title:
        return False
    if _kw_hit(title, _STILL_ADVERSE_KW):
        return False
    return bool(_RESOLVED_RE.search(title.replace(" ", " ")))

def is_hard_excluded(title: str, desc: str = "", url: str = "") -> tuple:
    """하드 제외 패턴 매칭 — (excluded: bool, reason: str) 반환

    ┌─ 판정 순서 (이 순서가 곧 우선순위이며, 바꾸면 규칙이 무력화된다) ─┐
    │ [1단] 발행 형식 게이트 — '기사 형식' 자체로 리스크 보도가 아닌 것 │
    │       연예매체 도메인 / 이중 브래킷 연재물 / 대조 비교 해설       │
    │ [2단] 사건 성격 게이트 — 키워드는 리스크지만 실제로는 아닌 것     │
    │       호재성 상폐 / 기술적 거래정지 / 거래정지 해제 / 해외파산 등  │
    │ [3단] CRITICAL_KW bypass — 위를 통과했고 치명 키워드가 있으면      │
    │       AI 판단으로 넘김                                            │
    │ [4단] 일반 패턴 매칭 — TITLE_ONLY / TEXT / 정규식 / 브래킷 추정    │
    └───────────────────────────────────────────────────────────────────┘

    ★ 반복 사고 원인 (5회 발생): 1·2단에 있어야 할 판정을 4단에 두면
      3단 bypass가 먼저 return해 규칙 자체에 도달하지 못한다.
      실제로 호재성 상폐·기술적 거래정지·거래정지 해제·대조 비교·
      이중 브래킷 연재물이 모두 같은 이유로 뚫렸다.
      → 새 게이트를 추가할 때는 반드시 '3단보다 앞'인지 확인할 것.
      → 4단(일반 패턴)에 넣어도 되는 것은 CRITICAL_KW와 무관한 순수
        노이즈(마케팅·인사·부고 등)뿐이다.
    """

    # 연예 전문매체 도메인 차단 — 금융 리스크 기사 비중 사실상 0, 오탐 다발원
    # (한민용=topstarnews, 샘킴=osen 오탐 이력 기반. 2026.07)
    # ★도메인은 '호스트 전체'가 아니라 '핵심 도메인'으로 등재할 것.
    #   tenasia.hankyung.com만 넣어뒀더니 같은 매체의 tenasia.co.kr이
    #   그대로 통과했다(7/28 '박은영 JTBC 재정난 유튜브 중단' 참고 5.5 오탐).
    _ENT_DOMAINS = ("osen.co.kr", "tenasia.", "topstarnews.net",
                    "newsen.com", "tvreport.co.kr", "mydaily.co.kr",
                    "xportsnews.com", "stardailynews.co.kr", "starnewskorea.com",
                    "joynews24.com", "sportsw.kr", "enews24.tving.com",
                    "bntnews.co.kr", "sportstoday.co.kr", "isplus.com",
                    "spotvnews.co.kr", "wikitree.co.kr", "insight.co.kr",
                    "dispatch.co.kr", "sportskhan.news", "sportsseoul.com")
    if url:
        _u = url.lower()
        for _dom in _ENT_DOMAINS:
            if _dom in _u:
                return True, f"연예매체 도메인: {_dom}"

    # ═══ [2단] 사건 성격 게이트 — CRITICAL_KW bypass보다 반드시 앞 ═══
    # ── 호재성 상장폐지 원천 차단 (AND 게이트) ──
    # 배경: M&A·공개매수 자진상폐는 filter_prompt(75행)·gemini(109행)·
    # 2차검증 프롬프트에 모두 규칙이 있는데도 AI가 반복적으로 놓쳐 왔음
    # (7/19 골드그룹 참고, 7/23 동양생명 주의, 7/24 SK시그넷 긴급 6.5).
    # "상장폐지" 단어가 CRITICAL_KW라 AI 우회 경로를 타는 게 근인.
    # → 프롬프트에 의존하지 않고 코드에서 결정론적으로 차단한다.
    #
    # 단순 "공개매수" 단독 차단은 불가(적대적 M&A·경영권 분쟁 등 진짜
    # 리스크 기사에도 등장). 따라서 ①상폐/매각 맥락 + ②주주 보상수단이
    # 동시에 있을 때만 차단하는 AND 조건으로 오차단을 막는다.
    _DELIST_CTX_KW = ("상장폐지", "상폐", "자진상장폐지", "자진 상장폐지",
                      "코넥스", "매각", "인수")
    _SHAREHOLDER_COMPENSATION_KW = (
        "공개매수", "프리미엄", "잔여 지분", "잔여지분", "현금 교부금",
        "교부금", "주식교환", "완전자회사", "지분 매입", "현금 매입",
    )
    # 단, 적대적 M&A·경영권 분쟁·실패 국면은 주주 보상이 확정되지 않아
    # 실질 리스크가 있으므로 게이트에서 제외한다.
    _HOSTILE_KW = ("적대적", "경영권 분쟁", "경영권분쟁", "방어", "무산",
                   "불발", "실패", "철회", "반발", "소송", "가처분", "저지")
    if (_kw_hit(title, _DELIST_CTX_KW)
            and _kw_hit(title, _SHAREHOLDER_COMPENSATION_KW)
            and not _kw_hit(title, _HOSTILE_KW)):
        return True, "호재성 상장폐지(공개매수·프리미엄 보상)"

    # ── 규제 완화·허용 = 대상 기관에 호재 (2026-08-02 신설) ──
    # 8/2 21시 실사례: "「23조 부실 털자」…지역농협 NPL펀드 셀프투자 허용"이
    # 주의 5.5점으로 발송됐다. 제목의 '23조 부실'이 리스크로 읽혔으나, 실제
    # 내용은 부실채권 처리 수단을 넓혀주는 규제 완화라 방향이 정반대다.
    # 판단축: 당국 조치의 방향이 '풀어줌'인가 '조임'인가.
    # 부실 규모(N조 부실·NPL·연체율) 병기는 조치의 배경 설명이지 신규 손실이 아니다.
    _EASE_RE = re.compile(
        r'(?:규제|요건|기준|한도|제한)[^,·]{0,6}?(?:완화|개선|해제|폐지)'
        r'|셀프투자\s*허용'
        r'|(?:완화|허용)\s*(?:인가|결정|방침)'
    )
    # 조임 방향·부담 전가가 함께 있으면 실질 리스크 — 게이트 면제
    _TIGHTEN_KW = ("강화", "제한", "중단", "취소", "인가 취소", "자본확충",
                   "엄격", "부담 전가", "손실보전", "인수 의무")
    if _EASE_RE.search(title) and not _kw_hit(title, _TIGHTEN_KW):
        return True, "규제 완화·허용(대상 기관 호재)"

    # 적대적 M&A·경영권 분쟁은 실질 리스크 — 기존 '매수'(응원매수용) 등
    # 일반 패턴에 걸려 조기 차단되지 않도록 AI 판단으로 우선 통과시킨다.
    if _kw_hit(title, ("적대적", "경영권 분쟁", "경영권분쟁")):
        return False, None

    # ── 기술적 거래정지 원천 차단 (AND 게이트) ──
    # 주식분할·병합·액면변경 등에 수반되는 거래정지는 신주권 상장 전까지의
    # 절차상 일시 정지로, 손실·부실과 무관한 정상 이벤트.
    # 실사례(7/24 21시): "거래소 '한울앤제주, 29일부터 주권매매거래정지'"가
    # 긴급 6.8로 발송 — 본문 사유는 "주식분할에 따른 전자등록 변경·말소"였고
    # 여신잔고도 없었으나, 대응방안·고객안내 문구까지 생성됨.
    # 기존 프롬프트에 "기술적조치(액면병합 등)" 제외 규칙이 있었지만 주식분할이
    # 목록에 없어 통과 → 코드에서 결정론적으로 차단한다.
    # desc(본문 요약)까지 함께 검사: 사유는 제목이 아닌 본문에만 있는 경우가 많음.
    _halt_text = f"{title} {desc or ''}"
    _HALT_CTX_KW = ("거래정지", "거래 정지", "매매거래정지", "매매거래 정지",
                    "주권매매거래정지", "주권매매거래 정지")
    _TECHNICAL_REASON_KW = (
        "주식분할", "주식 분할", "액면분할", "액면 분할",
        "주식병합", "주식 병합", "액면병합", "액면 병합",
        "전자등록 변경", "전자등록변경", "말소",
        "변경상장", "변경 상장", "신주권", "주권제출", "주권 제출",
        "액면가 변경", "무상증자", "기업분할", "인적분할", "물적분할",
    )
    # 실질 리스크 사유 — 하나라도 있으면 위 기술적 게이트를 적용하지 않는다.
    _SUBSTANTIVE_HALT_REASON_KW = (
        "부도", "파산", "회생", "워크아웃", "채무불이행", "디폴트",
        "횡령", "배임", "분식", "사기", "수사", "기소", "구속",
        "감사의견", "의견거절", "의견 거절", "한정의견",
        "상장폐지", "상폐", "폐지 사유", "관리종목", "불성실공시",
        "자본잠식", "영업정지", "제재", "과징금", "실질심사",
        "미상환", "연체", "채권단", "법정관리",
    )
    if (_kw_hit(_halt_text, _HALT_CTX_KW)
            and _kw_hit(_halt_text, _TECHNICAL_REASON_KW)
            # ★ 안전장치: 실질 리스크 사유가 함께 있으면 기술적 절차가 언급돼도
            #   차단하지 않는다. 예) "부도로 거래정지…채권 전자등록 변경 절차",
            #   "횡령 발생으로 매매거래정지"(본문에 주식분할이 무관하게 언급).
            #   미탐(진짜 리스크 차단)이 오탐보다 훨씬 치명적이므로 보수적으로 통과.
            and not _kw_hit(_halt_text, _SUBSTANTIVE_HALT_REASON_KW)):
        return True, "기술적 거래정지(주식분할·병합·전자등록 변경 등)"

    # ── 해외 비상장·해외 산업 파산 기사 차단 ───────────────────────────
    # 해외 비상장 기업의 파산·회생은 국내 상장 익스포저와 무관하다.
    # 프롬프트에 규칙이 있으나 '파산'(RISK_PRIORITY 1.5)이 CRITICAL_KW라
    # bypass를 타 반복 통과했음(레드랍스터·LIV골프에 이어 7/27 21시
    # "스토리지랩스 미국 파산법 11장", "노스볼트 이어 바르타 파산 가능성").
    # 해외 법제 표지(파산법 11장·챕터11 등) 또는 해외 지역 표지 + 산업 전망이
    # 있으면 차단하되, 국내 파급이 명시되면(국내 증시·상장사·수출) 면제.
    _FOREIGN_BANKRUPTCY_KW = ("파산법 11장", "챕터11", "챕터 11", "챕터 일레븐",
                              "연방파산법", "미국 파산법", "일본 민사재생법")
    _FOREIGN_REGION_KW = ("유럽", "미국", "일본", "중국", "독일", "프랑스",
                          "영국", "스웨덴", "북미", "해외")
    _INDUSTRY_OUTLOOK_KW = ("산업 붕괴", "붕괴 조짐", "업계 위기", "산업 위기",
                            "줄도산", "연쇄 파산", "생태계 붕괴")
    if (_kw_hit(title, _FOREIGN_BANKRUPTCY_KW)
            or (_kw_hit(title, _FOREIGN_REGION_KW)
                and _kw_hit(title, _INDUSTRY_OUTLOOK_KW))):
        if not _kw_hit(title, ("국내", "코스피", "코스닥", "상장사", "수출",
                                "국내 증시", "한국")):
            return True, "해외 비상장·해외 산업 파산"

    # ── 시장 인프라 기관 자체 이슈 차단 ────────────────────────────────
    # 한국거래소·예탁결제원·금융투자협회 등은 당사도 경쟁사도 아닌 시장 인프라
    # 기관이다. 이들의 경영평가·인사·조직개편은 당사 손실과 무관한데,
    # '전산장애'(RISK_PRIORITY 1.8) 같은 키워드를 물면 점수가 크게 오른다.
    # 실사례(7/27 21시): "한국거래소, 지난해 경영평가 B등급…전산장애 영향에
    # 두 단계 하락" 참고 5.4 — 거래소 경영평가는 당사 리스크가 아니다.
    # 단, 거래소 시스템 장애로 '매매가 실제 중단'된 경우는 당사 고객에게
    # 영향이 있으므로 면제한다.
    _INFRA_ORG_KW = ("한국거래소", "거래소", "예탁결제원", "금융투자협회",
                     "코스콤", "KRX", "증권금융")
    _INFRA_ADMIN_KW = ("경영평가", "등급", "인사", "조직개편", "이사장", "사장 선임",
                       "임원 인사", "채용", "사회공헌", "표창", "포상", "국정감사")
    if (_kw_hit(title, _INFRA_ORG_KW) and _kw_hit(title, _INFRA_ADMIN_KW)
            and not _kw_hit(title, ("매매 중단", "거래 중단", "매매중단",
                                     "전산 마비", "시스템 마비", "체결 지연"))):
        return True, "시장 인프라 기관 행정 이슈"

    # ── 업계 서비스·경쟁 트렌드 기사 차단 ──────────────────────────────
    # "증권업계, MTS 경쟁 확전" 같은 서비스 경쟁·신기능 소개 기사는 리스크가
    # 아닌데, 'MTS'가 RISK_PRIORITY 가중치 1.8이라 점수가 5.4까지 치솟았음
    # (7/26 14시 실사례 — 임계 5.5 바로 아래였다).
    # 업계 동향 표지 + 경쟁·서비스 어휘 조합이면 차단하되, 장애·사고·제재 등
    # 실질 리스크 어휘가 함께 있으면 면제한다.
    _INDUSTRY_CTX_KW = ("증권업계", "업계", "은행권", "카드업계", "보험업계", "금융권")
    _SERVICE_TREND_KW = ("경쟁", "확전", "각축", "맞불", "출시", "오픈", "개편",
                         "리뉴얼", "서비스 강화", "고도화", "탑재", "도입",
                         "선보여", "선봬", "새단장", "새 단장")
    if (_kw_hit(title, _INDUSTRY_CTX_KW) and _kw_hit(title, _SERVICE_TREND_KW)
            and not _kw_hit(title, _SUBSTANTIVE_HALT_REASON_KW)
            and not _kw_hit(title, ("장애", "사고", "먹통", "오류", "중단",
                                     "손실", "민원", "불완전판매", "제재",
                                     "과징금", "징계", "검사", "조사", "적발",
                                     "위반", "논란", "피해"))):
        return True, "업계 서비스·경쟁 트렌드"

    # ── 시장경보(투자경고·투자주의·투자위험) 지정 차단 ──────────────────
    # 거래소의 투자경고/투자주의/투자위험종목 지정은 단기 급등에 대한 과열
    # 경계 조치로, 부실이 아니다. filter_prompt에 "테마주 급등 경계 조치는
    # false" 규칙이 있으나 AI가 놓치면 그대로 통과했음
    # (7/26 07시 "[공시] 엔젠바이오·휴림에이텍 '투자경고'…" 참고 3.8 실사례).
    # 브래킷 [공시]는 보도성 화이트리스트라 코너물 판별도 비껴갔다.
    # 단, 하락·부실 사유가 함께 있으면(관리종목·감사의견거절 등) 실질
    # 리스크이므로 면제한다 — 프롬프트의 "하락사유 거래정지는 탐지"와 일치.
    # K-OTC(금융투자협회 장외시장) 관련 지정은 유가·코스닥 상장폐지와 별개.
    # 당사 위탁매매 익스포저가 사실상 없어 손실과 무관하다.
    # (7/27 "일정실업, K-OTC 상장폐지지정기업부 신규 지정" 참고 4.2 오탐 —
    #  익스포저 0억 확인)
    if _kw_hit(title, ("K-OTC", "K OTC", "케이오티씨", "장외시장 지정")):
        return True, "K-OTC 장외시장(당사 익스포저 무관)"

    _MARKET_ALERT_KW = ("투자경고", "투자주의", "투자위험", "시장경보",
                        "투자경고종목", "투자위험종목", "단기과열")
    if _kw_hit(title, _MARKET_ALERT_KW) and not _kw_hit(
            title, _SUBSTANTIVE_HALT_REASON_KW):
        return True, "시장경보 지정(급등 과열 경계)"

    # ── 연예·인물 논란 파생기사 차단 (AND 게이트) ──
    # 기업의 회생·파산 등 실제 사건에서 파생된 연예인·인물 가십 기사는
    # 금융 리스크가 아님. 실사례(7/24 21시): "이나연, 회생 신청한 JTBC
    # '출근 브이로그' 뭇매"가 점수 6.5(긴급 기사 6.8과 동급)로 산정됨.
    # 리스크 키워드(회생)가 제목에 있어 CRITICAL_KW bypass를 타는 게 근인.
    #
    # ※ 회귀세트 대조 결과 동일 유형 과거 오탐이 다수 확인돼 일반화:
    #   - "샘킴, …정호영 배신하고 에스파 춤췄다..카리나 깜짝"(연예가십+키워드오염)
    #   - "김미경, 회사 부도 위기·빚 수십억"(비상장 개인사업자)
    #   초기 구현은 '브이로그/뭇매' 등 특정 어휘에만 반응해 위 2건을 놓쳤음.
    # 인물 발언·심경 표현 — 직함 없이 이름만 나오는 연예 기사 대응
    # (7/28 "박은영, 'JTBC 재정난'에 유튜브도 중단…\"제작비의 어려움, 슬퍼\"")
    # 기업 리스크가 배경일 뿐 내용은 개인의 활동 중단·심경이다.
    if _kw_hit(title, ("슬퍼", "속상", "눈물", "심경", "고백", "토로", "울먹",
                       "안타깝", "먹먹", "착잡")) and _kw_hit(
                title, ("유튜브", "채널", "방송", "출연", "활동", "인스타", "SNS")):
        return True, "인물 심경·활동 기사"

    _GOSSIP_KW = ("브이로그", "유튜브", "인스타", "SNS", "뭇매", "구설",
                  "해명", "사과문", "논란 확산", "갑론을박", "누리꾼",
                  "네티즌", "악플", "댓글 반응", "팬들", "방송 출연",
                  "예능", "화보", "인터뷰 논란")
    _PERSON_CTX_KW = ("아나운서", "앵커", "배우", "가수", "아이돌", "연예인",
                      "출연자", "MC", "개그맨", "모델", "인플루언서",
                      "셰프", "요리사", "방송인", "유튜버", "코미디언",
                      "강사", "작가", "감독", "프로듀서")
    # 연예·방송 고유 어휘 — 하나만 있어도 금융 기사가 아닐 가능성이 매우 높음
    _SHOWBIZ_KW = ("에스파", "카리나", "아이유", "블랙핑크", "BTS", "방탄소년단",
                   "드라마", "예능 프로", "출연료", "소속사", "데뷔", "컴백",
                   "무대", "팬미팅", "콘서트", "앨범", "뮤직비디오",
                   "열애", "결혼설", "이혼", "폭로", "사생활")
    _gossip_hit = _kw_hit(title, _GOSSIP_KW)
    _person_hit = _kw_hit(_halt_text, _PERSON_CTX_KW)
    _showbiz_hits = [s for s in _SHOWBIZ_KW if s in title]
    _showbiz_hit = bool(_showbiz_hits)
    if (_gossip_hit and (_person_hit or _kw_hit(title, ("브이로그", "뭇매", "누리꾼", "악플")))) \
            or (_showbiz_hit and _person_hit) \
            or (_showbiz_hit and _gossip_hit):
        return True, "연예·인물 논란 파생기사"
    # 연예 고유어휘가 2개 이상 동시 등장하면 연예매체가 아닌 일반 매체
    # 게재분이라도 연예 기사로 판단(도메인 차단 사각지대 보완).
    # 이 조건만 넓게 걸리므로, 금융 리스크 어휘가 함께 있으면 면제한다.
    if len(_showbiz_hits) >= 2 and not _kw_hit(title, (
            "주가", "급락", "반대매매", "상장", "공시", "유상증자",
            "회생", "파산", "부도", "감사의견", "횡령", "배임", "제재")):
        return True, "연예·인물 논란 파생기사"

    # 치명적 키워드 bypass — AI 판단으로 넘김
    CRITICAL_KW = ["상장폐지", "파산", "부도", "횡령", "배임", "거래정지",
                   "기업회생", "회생절차", "회생계획", "회생신청", "회생 신청", "회생 절차",
                   "법정관리", "워크아웃", "자본잠식", "감사의견", "상장적격성", "실질심사",
                   "채무불이행", "디폴트", "감사의견 거절", "감사의견거절",
                   "차환 실패", "차환실패", "미상환", "연체", "반대매매",
                   "불성실공시", "관리종목", "영업정지",
                   "MTS 장애", "MTS 접속 장애"]
    # 스팩·정상상폐·호재성·칼럼 기사는 CRITICAL_KW bypass 면제 → 하드제외 적용
    CRITICAL_EXEMPT = ["스팩", "SPAC", "기업인수목적", "알짜", "체질 변신", "체질 개선",
                       "방카", "인수 효과", "밸류업", "주식병합",
                       # 상장폐지 저지 응원매수·테마주 강세 — "상장폐지"(CRITICAL_KW) 있어도
                       # 호재성 확정(7/19 "돈쭐내서 살리자" 오탐 실사례 — AI가 프롬프트 규칙을
                       # 놓쳐 참고로 발송됨. 결정론적 하드제외로 재발 방지)
                       "돈쭐", "응원매수", "저지 응원", "막자 매수", "애국테마", "애국 테마",
                       # 완전자회사 편입·주식교환 자진상폐 — "상장폐지" 있어도 손실 무관
                       # (7/23 동양생명·신세계푸드 오탐 실사례)
                       "완전자회사", "자회사 편입", "편입 완료", "편입 본궤도", "주식교환일",
                       # 칼럼·오피니언 형식 — CRITICAL_KW 있어도 AI 우회 차단
                       "[현장의 시각]", "[현장의시각]", "[시각]", "[오피니언]",
                       "[기고]", "[데스크 시각]", "[현장 르포]", "[기자수첩]",
                       "현장의 시각", "현장에서 보는",
                       # 방송·연예 콘텐츠 — 시청률·드라마·예능 기사
                       "시청률", "드라마", "예능", "반전 활약", "연기", "출연",
                       "OST", "제작발표회", "시사회", "배우", "촬영", "방영",
                       # 가상자산 — 코인·토큰 중심 기사는 거래정지·상폐 키워드 있어도
                       # 뱅키스 대상 아님. (예: '리퍼블릭, 거래정지…이더리움 1570개 보유')
                       "이더리움", "비트코인", "가상자산", "암호화폐", "알트코인",
                       "솔라나", "리플", "도지코인", "스테이블코인", "코인 보유",
                       "ETH", "BTC", "디파이", "스테이킹",
                       # 회고·역사 정리성 기사 — CRITICAL_KW(파산 등)가 위기 극복 서사의
                       # 일부로 언급되는 경우. 실측: "파산 문턱서 나스닥까지…되짚은 SK하이닉스 25년"
                       "되짚은", "되짚어", "돌아본 ", "돌아보는", "지나온 길", "그간의 여정",]
    # ── 거래정지 해제·재개 = 리스크 해소 ────────────────────────────────
    # '거래정지'는 CRITICAL_KW(가중치 1.5)라 bypass를 타는데, '풀리고·해제·
    # 재개' 같은 해소 맥락에서도 그대로 통과했음.
    # 실사례(7/27 14시): "거래정지 풀리고 70% 뛴 효성화학…2분기 반전 쓸까"
    # 가 참고 6.0점으로 발송 — 거래재개 + 70% 급등은 완전한 호재다.
    # 기존 '거래재개' 패턴은 있었으나 '거래정지 풀리고' 형태를 못 잡았다.
    # ★단, 해제 후 재차 악화된 경우(재정지·재차·다시)나 실질 리스크 사유가
    #   함께 있으면 면제 — "거래 재개 직후 재차 거래정지…감사의견 거절"
    #   같은 기사를 차단하면 미탐이 된다(검증에서 확인).
    if (_kw_hit(title, ("정지 풀리", "정지 해제", "거래 재개", "거래재개",
                        "재개 첫날", "정지 해소", "매매 재개", "매매재개",
                        "상폐 면했", "상장유지 결정", "실질심사 통과"))
            and not _kw_hit(title, ("재차", "재개 직후", "다시 정지", "재정지",
                                    "또 정지", "무산", "취소", "불발"))
            # _SUBSTANTIVE 전체를 면제어로 쓰면 '상폐 면했다'·'실질심사 통과'
            # 같은 해소 표현까지 걸려 게이트가 무력화된다(검증에서 확인).
            # 해소 기사에 나올 수 없는 '확정 악화' 사유만 좁게 본다.
            and not _kw_hit(title, ("감사의견 거절", "의견거절", "자본잠식",
                                    "부도", "파산", "회생절차", "횡령", "배임",
                                    "채무불이행", "디폴트"))):
        return True, "거래정지 해제(리스크 해소)"

    # ═══ [1단] 발행 형식 게이트 ═══
    # ── 이중 브래킷 연재 시리즈 차단 ────────────────────────────────────
    # "[더벨][상장폐지 카운트다운] …" 처럼 [매체][코너] 형태로 시작하는 기사는
    # 명백한 연재 기획물이다. EXCLUDE_TITLE_RE_PATTERNS에 규칙이 있었으나
    # 제목에 CRITICAL_KW('상장폐지')가 있으면 bypass가 먼저 return해
    # 규칙 자체에 도달하지 못했다(7/28 14시 참고 5.8 오탐 실사례).
    # → bypass보다 앞에서 판정한다. 두 번째 브래킷이 보도성(속보·공시 등)이면
    #   면제해 "[단독][속보] …" 같은 형태는 통과시킨다.
    _dbl = re.match(r'^\[([^\]]{1,12})\]\s*\[([^\]]{1,20})\]', title)
    if _dbl:
        _b1, _b2 = _dbl.group(1).strip(), _dbl.group(2).strip()
        _NEWSY = {"단독", "속보", "공시", "특징주", "긴급속보", "공식"}
        if _b1 not in _NEWSY and _b2 not in _NEWSY:
            return True, f"이중 브래킷 연재물: [{_b1}][{_b2}]"

    # A vs B 대조 비교 기사 — 사실 보도가 아닌 해설·기획물.
    # (7/26 "파산 문턱 홈플러스 vs 흑자 부활 남양유업…사모펀드가 가른 극과 극")
    # ★CRITICAL_KW(파산 등) bypass보다 먼저 판정해야 한다. 뒤에 두면 '파산'
    #   때문에 AI 판단으로 넘어가 그대로 통과한다(검증에서 확인).
    if _kw_hit(title, (" vs ", " VS ", "vs.", "극과 극", "명암 갈린",
                       "엇갈린 운명", "희비 갈린", "희비교차")):
        return True, "대조 비교 해설기사"

    # ═══ [3단] CRITICAL_KW bypass — 여기 도달 = 1·2단을 모두 통과 ═══
    # ★이 아래에 추가하는 규칙은 치명 키워드가 있는 기사에는 적용되지 않는다.
    # CRITICAL_KW / CRITICAL_EXEMPT 판정도 공백 무시로 비교한다.
    # 면제어가 공백 변형("응원 매수")으로 쓰이면 면제가 적용되지 않아
    # CRITICAL_KW bypass를 타고 그대로 통과하던 문제(7/25 실측).
    if _kw_hit(title, CRITICAL_KW):
        if not _kw_hit(title, CRITICAL_EXEMPT):
            return False, None  # 치명적 키워드 → AI 판단으로 넘김
    # 대형 익스포저 섹터 + 리스크 표현 조합 → 밸류에이션 패턴 있어도 통과
    SECTOR_KW  = ["반도체", "AI", "엔비디아", "테슬라", "배터리", "전기차",
                  "바이오", "금융주", "은행주", "삼성전자", "하이닉스"]
    RISK_EXPR  = ["급락", "쇼크", "위기", "리스크", "균열", "붕괴", "흔들", "패닉"]
    if _kw_hit(title, SECTOR_KW) and _kw_hit(title, RISK_EXPR):
        return False, None  # 섹터 리스크 기사 → AI 판단

    # 이벤트·할인 등 마케팅 키워드가 있어도 소비자 불만/지연 신호가 함께 있으면
    # 하드제외 면제 — 7/22 "한투증권 이벤트 보상 하세월…참여자 불만 잇따라" 오탐(누락)
    # 실사례. 순수 이벤트 공지("~이벤트 진행")는 계속 차단, 이벤트發 소비자 불만
    # (지급 지연·미지급 등 당사 평판/운영 리스크)만 AI 판단으로 넘긴다.
    _EVENT_MARKETING_KW = ("할인", "이벤트", "프로모션")
    _EVENT_COMPLAINT_KW = ("불만", "지연", "미지급", "하세월", "늑장", "누락",
                           "먹튀", "기만", "논란")
    if (_kw_hit(title, _EVENT_MARKETING_KW)
            and _kw_hit(title, _EVENT_COMPLAINT_KW)):
        return False, None


    # ═══ [4단] 일반 패턴 매칭 — CRITICAL_KW 없는 기사만 도달 ═══
    # ── 공백 무시 매칭 ──────────────────────────────────────────────
    # 반복 오탐의 구조적 원인: 패턴은 등재돼 있는데 기사가 띄어쓰기를 다르게
    # 쓰면 빠져나갔음. 예) 등재 '응원매수' ↔ 기사 '응원 매수',
    # '완전자회사' ↔ '완전 자회사', '목표주가' ↔ '목표 주가'.
    # 전수 조사 결과 공백 변형에 취약한 한글 패턴이 72개(전체 426개 중)였고,
    # 오탐을 발견할 때마다 변형을 하나씩 추가하는 두더지잡기가 반복됐다.
    # → 제목·본문과 패턴 양쪽의 공백을 제거해 비교한다.
    # 안전성 실측: 정탐 이력 23건에 대해 새로 차단되는 건 0건(오차단 없음),
    # 오탐 이력에서는 추가 차단이 발생함을 확인 후 적용.
    _title_ns = _NS_RE.sub("", title)

    # ── 호재 패턴 + 부정어 조합 면제 ────────────────────────────────────
    # '수주·계약체결·MOU·신고가' 등은 호재라서 하드제외 대상이지만, 그 호재가
    # 무산·취소·파기된 기사는 오히려 실질 리스크다. 기존엔 호재 패턴만 보고
    # 차단해 "계약체결 무산에 자금난", "수주 취소로 유동성 위기", "MOU 파기"
    # 같은 진짜 리스크를 놓치고 있었음(2026-07 전수검수 실측 — 공백 무시화
    # 이전부터 존재하던 미탐).
    # → 부정어가 함께 있으면 이 패턴들은 차단하지 않고 AI 판단으로 넘긴다.
    # ── 역접 논평·칼럼 구조 판별 ────────────────────────────────────────
    # "영광의 IMA, 하지만 상당한 왕관의 무게…주주 챙기기 쟁점"
    # "화려한 STO 인가, 그러나 남은 과제…주주가치 쟁점"
    # 처럼 [긍정 수식] + [역접 접속사] + [부담·과제 어휘] 구조는 사실 보도가
    # 아니라 논평·칼럼이다. 개별 표현을 패턴화하면 변형에 계속 뚫리므로
    # 구조(역접 + 논평어휘 동시 등장)로 판별한다.
    # CRITICAL_KW(부도·회생·상장폐지 등) bypass가 이 지점보다 먼저 동작하므로
    # 실제 리스크 사건 기사는 여기까지 오지 않는다.
    # 거시 환경 전망 칼럼 — 특정 종목을 지목하지 않는 매크로 논평
    # (7/27 "다시 직면한 '고유가-고금리' 위험" 참고 2.7 오탐)
    # 거시 지표 어휘 + 전망·재부각 표현 조합이며, 개별 기업명이 없을 때만 차단.
    _MACRO_KW = ("고유가", "저유가", "고금리", "저금리", "환율", "물가", "인플레",
                 "경기침체", "스태그플레이션", "긴축", "금리인상", "금리인하",
                 "유가", "국제유가", "달러 강세", "원화 약세")
    _OUTLOOK_KW = ("직면", "재부각", "다시", "우려", "전망", "위험", "그림자",
                   "복병", "변수", "먹구름", "경고등", "빨간불")
    if (_kw_hit(title, _MACRO_KW) and _kw_hit(title, _OUTLOOK_KW)
            and not _kw_hit(title, _SUBSTANTIVE_HALT_REASON_KW)
            and not _kw_hit(title, ("반대매매", "강제청산", "마진콜", "담보비율"))):
        return True, "거시 환경 전망 칼럼"

    _CONTRAST_KW = ("하지만", "그러나", "그럼에도", "그런데도", "이면에", "반면")
    _COMMENTARY_KW = ("쟁점", "과제", "무게", "명암", "딜레마", "역설",
                      "그늘", "이면", "함정", "숙제", "고민", "물음표")
    if (_kw_hit(title, _CONTRAST_KW)
            and _kw_hit(title, _COMMENTARY_KW)):
        return True, "역접 논평·칼럼 구조"

    _POSITIVE_PATS = {
        "수주", "계약체결", "MOU", "협약", "신고가", "급등", "상한가",
        "흑자전환", "실적개선", "호실적", "목표달성", "목표주가",
        "최대 실적", "최고 실적", "역대급 실적", "ROE 개선", "ROE 상승",
        "전부문 성장", "전 부문 성장", "이익 증가", "이익 급증",
        "인가 획득", "인가 취득", "본인가", "예비인가", "라이선스 획득",
        "사업자 선정", "우선협상대상자", "수상", "대상 수상",
        "깜짝 실적", "어닝서프라이즈", "어닝 서프라이즈", "실적 서프라이즈",
        "증액 확정", "증액 발행", "수요예측 흥행", "완판",
        "수익률 상위", "수익률 1위", "상위권 싹쓸이",
    }
    _NEGATION_KW = ("무산", "취소", "철회", "불발", "실패", "해지", "파기",
                    "결렬", "반려", "거부", "좌초", "백지화", "취하", "중단",
                    "급감", "축소", "미달", "하회", "하향", "반토막", "적자",
                    "위기", "부진", "차질",
                    # 주가 하락 표기 — "깜짝 실적에도 7.89%↓"처럼 호재 어휘와
                    # 함께 쓰이면 실제로는 급락 기사다(검증에서 미탐 확인)
                    "↓", "급락", "폭락", "하락", "약세", "곤두박질", "미끄")
    _has_negation = _kw_hit(title, _NEGATION_KW)

    # 섹터 일반명사(은행권·보험사 등)는 업계 동향 기사 배제용인데, 제재·조사·
    # 장애 같은 실질 리스크 어휘와 함께 쓰이면 실제 사건 보도다(검증에서
    # "은행권 불완전판매 무더기 제재…과징금 부과" 미탐 확인).
    _SECTOR_PATS = {"보험사", "은행권", "저축은행", "캐피탈", "증권업계", "카드업계"}
    _has_incident = _kw_hit(title, ("제재", "과징금", "징계", "검사", "조사",
                                    "적발", "위반", "장애", "사고", "먹통",
                                    "불완전판매", "횡령", "배임", "피해"))

    for pat, pat_ns in _TITLE_PATTERNS_NS:
        if pat in title or (pat not in _NS_EXEMPT and pat_ns in _title_ns):
            if _has_negation and pat in _POSITIVE_PATS:
                continue  # 호재가 무산된 기사 → 실질 리스크이므로 AI 판단으로
            if _has_incident and pat in _SECTOR_PATS:
                continue  # 섹터명 + 실제 사건 → 업계 동향이 아님
            return True, pat
    text = title + " " + (desc or "")
    _text_ns = _NS_RE.sub("", text)
    for pat, pat_ns in _TEXT_PATTERNS_NS:
        if pat in text or (pat not in _NS_EXEMPT and pat_ns in _text_ns):
            return True, pat
    for pat in EXCLUDE_TITLE_RE_PATTERNS:
        if re.search(pat, title):
            return True, pat

    # ── 브래킷 코너물 구조적 판별 (화이트리스트 한계 극복) ──────────────
    # 제목이 "[코너명]"으로 시작하거나 끝나면, 그 코너명이 보도성(단독·속보 등)이
    # 아닌 한 필자 개인 코너·연재물로 추정 → CRITICAL_KW 없으면 차단
    # 신규 코너명(예: [기자의 창], [줌인], [애널리스트 노트])도 자동 차단됨
    #
    # 말미 브래킷 대응(2026-07-24): 기존엔 선두(^)만 검사해 동일 코너명이라도
    # "[기자의 창] A사 실적 부진"은 차단되고 "A사 실적 부진 [기자의 창]"은
    # 통과하는 사각지대가 있었음. 실제로 [이런국장]은 말미 형태라 별도
    # 화이트리스트 정규식으로만 잡히고 있었고, 등재되지 않은 신규 코너명은
    # 말미에 오면 그대로 통과했음.
    # ★잘린 제목 대응(2026-07-27): 네이버 뉴스 API가 제목을 자르면
    #   "…[Z-스코어 기업가치 ..." 처럼 닫는 대괄호가 사라져 기존 정규식이
    #   전부 매칭 실패했음. 실사례 2건이 코너물인데 그대로 통과.
    #   여는 대괄호로 시작해 말줄임(…/...)으로 끝나면 잘린 코너명으로 본다.
    _bracket_m = (re.match(r'^\[([^\]]{1,20})\]', title)
                  or re.search(r'\[([^\]]{1,20})\]\s*$', title)
                  or re.search(r'\[([^\]]{1,20}?)\s*(?:\.{2,}|…)\s*$', title))
    if _bracket_m:
        _bracket_content = _bracket_m.group(1)
        # 보도성 브래킷(허용) — 최소화된 화이트리스트
        # 보도성 브래킷(허용) — 부분일치가 아닌 '완전일치'로 판정한다.
        # 부분일치였을 때 "[제약공시 책갈피]"가 '공시'에 걸려 보도성으로 오인돼
        # 코너물이 그대로 통과했음(7/25 14시 주의 4.2 오탐 실사례).
        _NEWS_BRACKET_KW = {"단독", "속보", "공시", "특징주", "긴급속보", "공식",
                            "공시 종합", "오늘의 공시"}
        _bc = _bracket_content.strip()
        if _bc not in _NEWS_BRACKET_KW:
            _CRITICAL_KW_LOCAL = ["상장폐지", "파산", "부도", "횡령", "배임",
                                  "거래정지", "기업회생", "회생절차", "회생계획", "회생신청",
                                  "MTS 장애", "MTS 접속 장애"]
            if not _kw_hit(title, _CRITICAL_KW_LOCAL):
                return True, f"브래킷 코너 추정: [{_bracket_content}]"

    return False, None


def ai_filter_batch_gemini(batch: list, offset: int = 0) -> list:
    """Gemini Flash 1차 필터링 — response_schema 강제로 JSON 파싱 오류 원천 차단
    반환: list(성공) | None(실패 → Claude fallback 트리거)
    인터페이스: ai_filter_batch와 완전 동일
    """
    global GEMINI_MODEL   # 모델 은퇴 시 후보로 전환하기 위해
    if not batch or not GOOGLE_API_KEY:
        return None

    try:
        from google import genai as _genai
        from google.genai import types as _gtypes
    except ImportError:
        print("  [Gemini] google-genai 미설치 — Claude fallback")
        return None

    numbered = "\n".join([
        f"{i+offset+1}. {a['title']}\n   요약: {a.get('desc','')}"
        for i, a in enumerate(batch)
    ])
    _fp = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
               "filter_prompt_gemini.txt"), encoding="utf-8").read()
    prompt = _fp.replace("{numbered}", numbered).replace("__KNOWN_CASES__", render_known_cases())

    # response_schema — 모든 필드 타입 명시, entities는 ARRAY(STRING)
    _item_schema = _gtypes.Schema(
        type=_gtypes.Type.OBJECT,
        properties={
            "id":             _gtypes.Schema(type=_gtypes.Type.INTEGER),
            "relevant":       _gtypes.Schema(type=_gtypes.Type.BOOLEAN),
            "grade":          _gtypes.Schema(type=_gtypes.Type.STRING,  nullable=True),
            "reason":         _gtypes.Schema(type=_gtypes.Type.STRING,  nullable=True),
            "confidence":     _gtypes.Schema(type=_gtypes.Type.NUMBER),
            "action":         _gtypes.Schema(type=_gtypes.Type.STRING,  nullable=True),
            "entity":         _gtypes.Schema(type=_gtypes.Type.STRING,  nullable=True),
            "entities":       _gtypes.Schema(
                                  type=_gtypes.Type.ARRAY,
                                  items=_gtypes.Schema(type=_gtypes.Type.STRING),
                                  nullable=True,
                              ),
            "event_type":     _gtypes.Schema(type=_gtypes.Type.STRING,  nullable=True),
            "related_stocks": _gtypes.Schema(
                                  type=_gtypes.Type.ARRAY,
                                  items=_gtypes.Schema(type=_gtypes.Type.STRING),
                                  nullable=True,
                              ),
        },
        required=["id", "relevant", "confidence"],
    )
    _schema = _gtypes.Schema(type=_gtypes.Type.ARRAY, items=_item_schema)

    # 15 RPM 기준 — 첫 배치 제외, 이후 배치는 5초 간격
    if offset > 0:
        time.sleep(5)

    for attempt in range(3):
        try:
            _t0 = time.time()
            _client = _genai.Client(api_key=GOOGLE_API_KEY)
            _resp = _client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=_gtypes.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema=_schema,
                ),
            )
            print(f"  [Gemini] 배치 {offset//50+1} 응답 {time.time()-_t0:.1f}초")

            grades = json.loads(_resp.text)
            if not isinstance(grades, list):
                raise ValueError(f"Gemini 응답 list 아님: {type(grades)}")

            # 파싱 후 article 필드 세팅 — ai_filter_batch와 동일 로직
            grade_map = {}
            for g in grades:
                _gid = g.get("id", g.get("news_id"))
                if _gid is not None:
                    grade_map[_gid] = g
            result = []
            for i, article in enumerate(batch):
                info = grade_map.get(i + offset + 1, {})
                article["_ai_confidence"] = info.get("confidence", None)
                if info.get("relevant") and info.get("grade"):
                    _ent = (info.get("entity") or "").strip()
                    if not _ent:
                        print(f"  [entity 빈값] relevant 무효화: {article.get('title','')[:30]}")
                        continue
                    article["grade"]      = info["grade"]
                    article["reason"]     = info.get("reason") or ""
                    article["action"]     = info.get("action") or ""
                    article["entity"]     = _ent
                    _ents_raw = info.get("entities") or []
                    _ents_clean = [e.strip() for e in _ents_raw if e and e.strip()] or [_ent]
                    if _ent not in _ents_clean:
                        _ents_clean = [_ent] + _ents_clean
                    article["entities"]      = _ents_clean
                    article["event_type"]    = info.get("event_type") or ""
                    # related_stocks: AI 추출 관련 상장주 — exposure_data 매칭 시 관련주 섹션 표시
                    _rs_raw = info.get("related_stocks") or []
                    article["related_stocks"] = [s.strip() for s in _rs_raw if s and s.strip()]
                    _evt = article["event_type"]
                    article["event_key"]  = f"{_ent}_{_evt}" if _ent and _evt else ""
                    # ※ 과거 _gemini_filtered 플래그로 재검증을 트리거했으나,
                    #    현재 재검증은 Gemini 사용 시 전건 대상이라 불필요.
                    #    설정만 하고 읽지 않는 죽은 플래그였으므로 제거(2026-07-29).
                    result.append(article)
            return result

        except Exception as e:
            _es = str(e)
            # 모델명 오류(404/NOT_FOUND)는 재시도해도 소용없다 → 즉시 fallback
            if any(x in _es for x in ["404", "NOT_FOUND", "no longer available",
                                       "is not found", "not supported"]):
                # ★모델이 은퇴했을 수 있다. 다음 후보로 전환해 재시도한다.
                #   (2026-07-29: gemini-2.5-flash가 공지일보다 일찍 내려가
                #    fallback 100% 발생 — 단일 모델 하드코딩의 위험)
                _cur = GEMINI_MODEL
                _rest = [m for m in _GEMINI_CANDIDATES if m != _cur]
                if _rest and not _RUN_STATS.get("gemini_model_switched"):
                    GEMINI_MODEL = _rest[0]
                    _RUN_STATS["gemini_model_switched"] = True
                    if not _RUN_STATS.get("gemini_err"):
                        _RUN_STATS["gemini_err"] = f"MODEL_SWITCH:{_cur}→{GEMINI_MODEL}"
                    print(f"  [Gemini] 모델 '{_cur}' 사용 불가 → '{GEMINI_MODEL}'로 전환 후 재시도")
                    continue
                print(f"  [Gemini] 모델 오류 → Claude fallback: {_es[:60]}")
                if not _RUN_STATS.get("gemini_err"):
                    _RUN_STATS["gemini_err"] = f"MODEL:{_es[:100]}"
                return None
            # 429·503·quota는 '일시적 제한'이므로 백오프 후 재시도한다.
            # 기존엔 즉시 Claude fallback으로 빠져 1차 필터가 유료 Claude로
            # 대체되고 있었음(2026-07-29 실측: 8배치 중 6배치). 유료 전환 후에도
            # 순간 버스트로 걸릴 수 있어 재시도를 둔다.
            if any(x in _es for x in ["429", "503", "quota", "RESOURCE_EXHAUSTED",
                                       "UNAVAILABLE"]):
                if attempt < 2:
                    _wait = (2 ** attempt) * 8 + random.uniform(0, 4)   # 8~12s, 16~20s
                    print(f"  [Gemini] 일시적 제한({_es[:40]}) — {_wait:.0f}초 후 재시도 "
                          f"({attempt+2}/3)")
                    time.sleep(_wait)
                    continue
                print(f"  [Gemini] 재시도 3회 소진 → Claude fallback: {_es[:50]}")
                if not _RUN_STATS.get("gemini_err"):
                    _RUN_STATS["gemini_err"] = f"QUOTA:{_es[:100]}"
                return None
            print(f"  [Gemini] 오류 시도 {attempt+1}/3: {_es[:80]}")
            if attempt < 2:
                time.sleep(random.uniform(5, 15))
                continue
            return None
    return None

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
    _fp_tpl    = _fp_tpl.replace("__KNOWN_CASES__", render_known_cases())
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
                    "max_tokens": 8000,
                    "temperature": 0.0,
                    "system": "당신은 JSON API입니다. 설명·요약·표·마크다운 없이 JSON 배열만 출력하세요. 출력은 반드시 [ 로 시작하고 ] 로 끝나야 합니다. 코드블록(```)도 사용하지 마세요. 각 객체의 식별자 키는 반드시 \"id\"여야 하며 \"news_id\" 등 다른 이름을 사용하지 마세요. 필드명은 정확히 id, relevant, grade, reason, confidence, action, entity, entities, event_type 만 사용하세요.",
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
            if grades and not all(isinstance(g, dict) for g in grades):
                raise ValueError(f"grades 요소가 dict가 아님 (markdown 응답 가능성): {type(grades[0])}")
            grade_map = {}
            for g in grades:
                _gid = g.get("id", g.get("news_id"))
                if _gid is not None:
                    grade_map[_gid] = g
            result = []
            for i, article in enumerate(batch):
                info = grade_map.get(i + offset + 1, {})
                article["_ai_confidence"] = info.get("confidence", None)
                if info.get("relevant") and info.get("grade"):
                    if not (info.get("entity") or "").strip():
                        print(f"  [entity 빈값] relevant 무효화: {article.get('title','')[:30]}")
                        continue
                    article["grade"]      = info["grade"]
                    article["reason"]     = info.get("reason", "")
                    article["action"]     = info.get("action", "")
                    article["entity"]     = (info.get("entity") or "").strip()
                    _ent2 = (info.get("entity") or "").strip()
                    _ents_clean2 = [e.strip() for e in (info.get("entities") or []) if e and e.strip()] or [_ent2]
                    if _ent2 not in _ents_clean2:
                        _ents_clean2 = [_ent2] + _ents_clean2
                    article["entities"]   = _ents_clean2
                    article["event_type"] = info.get("event_type", "")
                    # event_key: "entity_eventtype" 형태로 생성 — 사건 단위 dedup 기준
                    _ent = (info.get("entity") or "").strip()
                    _evt = (info.get("event_type") or "").strip()
                    article["event_key"]  = f"{_ent}_{_evt}" if _ent and _evt else ""
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
            return None  # 실패 (빈 결과 []와 구분)
    return None  # 3회 실패

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
        "가처분","효력정지","집행정지","이의신청","항고","판결","인용",
        "파산선고","청산","폐업","법정관리","회생인가","회생계획",
        "변제","채무조정","추가제재","과징금","검찰고발","수사착수",
    }
    _RESOLVE_KW_DET = ("기각","취하","철회","각하","거래재개","거래 재개",
                       "상장유지","재상장","정상화","해제","졸업")
    def _is_next_stage_det(title: str) -> bool:
        if _kw_hit(title, _RESOLVE_KW_DET):
            return False  # 해소 국면은 새 단계 아님
        return _kw_hit(title, _NEXT_STAGE)

    seen_urls_local = set()

    for a in articles:
        title_norm = normalize(a.get("title", ""))
        desc_norm  = normalize(a.get("desc", ""))
        entity     = (a.get("entity") or "").strip()
        keyword    = (a.get("keyword") or "").strip()
        combo      = (entity, keyword) if entity else None

        url = (a.get("url") or "").strip()
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
    "부도": 1.5, "거래정지": 1.5, "매매거래 정지": 1.5, "매매거래정지": 1.5, "거래 정지": 1.5,
    "상장 폐지": 1.5, "회생 신청": 1.3, "회생신청": 1.3, "채무불이행": 1.4, "디폴트": 1.4,
    "반대매매": 1.4, "강제청산": 1.4,
    "기업회생": 1.3, "워크아웃": 1.2,

    # ── 2026-08-01 확장 ──
    # 정탐 이력 35건 실측에서 제목 키워드 미매칭이 21건(60%)이었다. 미매칭이면
    # kw_weight가 기본값 1.0으로 눌려 원점수가 4.5에 고정되고, 이 값은 발송
    # 게이트(SELF_ONLY_MAX_SCORE=5.5)·등급 내 정렬·dedup 대표기사 선정에 모두
    # 쓰인다. 특히 '주의만 있는 회차'는 임계 미달로 자체 발송에 그친다.
    # (긴급은 _force_full로 우회되므로 영향 없음)
    #
    # 아래 표현은 오탐 이력 68건 전수 대조에서 매칭 0건을 확인하고 등재했다.
    # 신규 표현은 최고 대역(1.8 전산장애 / 2.0 당사)에 넣지 않는다 — 사전을
    # 넓히면 오탐 기사도 발송 게이트를 통과할 수 있어 1.2~1.5로만 배치한다.
    "주가조작": 1.5, "시세조종": 1.5, "미상환": 1.5,
    "차환 실패": 1.4, "차환실패": 1.4,
    "감사의견 거절": 1.4, "감사의견거절": 1.4,
    "상장적격성 실질심사": 1.4, "실질심사": 1.4,
    # 자본잠식 (2026-08-14 등재) — 상장폐지 사유가 확정된 사실인데 가중치 표에
    # 없어 점수에 반영되지 않았다. 실사례(8/14 14시): 광명전기·디에이테크놀로지가
    # 같은 '반기말 자본전액잠식·실질심사' 사건인데 9.1/6.4로 갈렸다.
    # ※ 등급은 올리지 않는다 — 등급 상향은 전사 발송 폭증을 부르므로, 점수(같은
    #   등급 내 정렬·발송 게이트)에만 반영한다. '자본잠식 해소·탈출' 호재 기사는
    #   기존 해소 강등 규칙이 별도로 처리하므로 단독형은 최저 대역에 둔다.
    "완전자본잠식": 1.4, "완전 자본잠식": 1.4,
    "자본전액잠식": 1.4, "자본 전액잠식": 1.4,
    "자본잠식": 1.2,
    "신용등급 하향": 1.3, "신용등급 강등": 1.3, "등급 하향": 1.3,
    "불성실공시": 1.3, "투자유의종목": 1.3,
    "회생절차 개시": 1.3, "회생절차 신청": 1.3,
    # '신용등급'·'발행어음' 단독은 상향·중립 기사도 걸릴 수 있어 최저 대역.
    # ('신용등급 BBB→BB 하향'처럼 등급 표기가 사이에 끼어 연속 매칭이 안 되는
    #  제목을 잡기 위한 보완 — 구체 표현이 함께 걸리면 max로 1.3이 적용된다)
    "신용등급": 1.2, "발행어음": 1.2,
    "PF 부실": 1.2, "PF부실": 1.2, "브릿지론": 1.2,
    "유동성 위기": 1.2, "유동성위기": 1.2, "실적 쇼크": 1.2,
}

# 당사 직접 이슈 키워드 — 익스포저 페널티 면제 + 긴급 강제 지정
DIRECT_INCIDENT_KW = {
    "MTS", "HTS",
    "전산장애", "전산사고", "접속장애", "접속불가",
}

# 당사 직접 언급 + 부정적 이슈 복합 조건 — 제목에 둘 다 있을 때만 force_urgent
DIRECT_COMPANY_KW = "한국투자증권"
DIRECT_NEGATIVE_KW = {
    "장애", "오류", "사고", "중단", "차단", "먹통",
    "제재", "과태료", "과징금", "고발", "수사", "검사",
    "해킹", "유출", "보안사고",
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
    # conf는 AI 응답(confidence)을 그대로 받으므로 이상값 방어가 필요하다.
    # 음수·1 초과·문자열이 들어오면 점수가 범위를 벗어난다(실측: conf=-0.5 →
    # -2.8점). 0.0~1.0으로 클램프하고, 변환 불가 시 보수적 기본값 0.3 사용.
    try:
        conf = float(article.get("_ai_confidence") if article.get("_ai_confidence") is not None else 0.3)
    except (TypeError, ValueError):
        conf = 0.3
    conf = min(max(conf, 0.0), 1.0)
    _title_only = article.get("title", "")
    title = _title_only + article.get("reason", "")
    # kw_weight는 '제목'에 실제 리스크 키워드가 있을 때만 가중.
    # reason·event_type은 AI 생성물이라 제목에 없는 사건(거래정지·상폐 등)을
    # 붙이는 오분류가 잦음 → 제목 무근거 격상 방지 위해 제목 기준으로 산정.
    # 키워드 가중치도 공백 무시로 매칭한다.
    # 기존엔 원문 그대로 비교해 '전산장애'는 잡히고 '전산 장애'는 못 잡아
    # 같은 사건이 5.0점 vs 8.2점으로 갈렸다(실측). RISK_PRIORITY에
    # '매매거래 정지'/'매매거래정지'처럼 변형을 수동 등재해 둔 것도 있으나
    # 누락된 게 많아, is_hard_excluded와 동일하게 공백 무시로 통일한다.
    _kw_title_ns = _NS_RE.sub("", _title_only)
    kw_weight = max(
        [v for k, v in RISK_PRIORITY.items()
         if k in _title_only or _NS_RE.sub("", k) in _kw_title_ns],
        default=1.0
    )
    is_direct_incident = _kw_hit(_title_only, DIRECT_INCIDENT_KW)

    # 익스포저 잔고 합산 → 구간별 boost
    exp_boost = 0.0
    if exposure_data is not None:
        entity = (article.get("entity") or "").strip()
        rows = find_exposure(entity, exposure_data) if entity else []
        if rows:
            article["_has_exposure"] = True
            total_bal = sum(
                _num(r.get("잔고(억)"))
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
    # 최종 점수도 0~10으로 클램프 — exp_boost 음수(-0.05)와 낮은 conf가
    # 겹치면 음수가 나올 수 있다.
    return round(min(max(raw * 5, 0.0), 10.0), 1)

# ── 표시용 리스크 점수 (2026-08-01 신설) ──
# 내부 원점수(_risk_score)는 '제목 키워드 가중치 × AI 확신도'라서 등급과 산정
# 축이 다르다. 그 결과 같은 메일에 5.7점 긴급과 5.2점 주의가 나란히 표시돼
# 임원이 푸터 기준표를 신뢰할 수 없었다.
# 실측(정탐 이력 35건): 제목 키워드 미매칭이 21건(60%)이라 원점수가 기본값에
# 눌리고, 긴급 4.5~9.0 / 주의 4.5~6.8로 두 등급 구간이 거의 완전히 겹친다.
# → 역전은 예외가 아니라 상시 발생이므로 키워드 사전 보강만으로는 해소 불가.
#
# [설계] 등급을 1차 신호로 삼고, 표시 점수는 등급 대역 안에 배치한다.
#   긴급 7.0~10.0 / 주의 5.0~7.0 / 참고 0~5.0
#   대역 내 위치는 원점수를 0~10 절대 스케일로 매핑 — 회차마다 동반 기사
#   구성이 달라져도 같은 기사는 항상 같은 점수가 나온다(상대 순위 방식의
#   회차 간 불일치 회피).
#
# ※ 내부 로직(_verify_high_risk_by_claude의 5.0 게이트, dedup 대표기사 선정,
#   참고 정렬)은 원점수를 그대로 쓴다. 표시값만 분리해 부작용을 차단한다.
_GRADE_SCORE_BAND = {"긴급": (7.0, 10.0), "주의": (5.0, 7.0), "참고": (0.0, 5.0)}

def display_risk_score(article: dict):
    """카드에 표시할 점수. 원점수가 없으면 '' 반환(점수 블록 생략)."""
    raw = article.get("_risk_score")
    if not raw:
        return ""
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return ""
    lo, hi = _GRADE_SCORE_BAND.get(article.get("grade", "참고"), (0.0, 5.0))
    return round(lo + (hi - lo) * min(max(raw, 0.0), 10.0) / 10.0, 1)

def regrade_by_score(articles: list, exposure_data: dict = None) -> list:
    """등급별 상한 초과 시 리스크 점수 기반으로 하위 등급 강등"""
    # ── 타 증권사 주체 기사의 당사 오추출 방어 ──
    # AI가 entity를 "한국투자증권"으로 뽑았으나 제목에 당사가 없고 타 증권사(키움·
    # 미래에셋 등)가 제목에 있으면, 경쟁사 이슈를 당사 이슈로 오인한 것. entity를
    # 비우고 참고로 강등해 당사 익스포저 매칭·긴급 발송을 차단한다.
    # 유지보수 주의: 이 증권사 목록은 filter_prompt.txt, filter_prompt_gemini.txt,
    # _verify_high_risk_by_claude() 재검증 프롬프트까지 4곳에 흩어져 있다.
    # 증권사를 추가/제외할 때는 4곳 모두 동일하게 수정할 것.
    _OTHER_BROKERS = ("키움", "미래에셋", "삼성증권", "NH투자", "신한투자", "KB증권",
                      "하나증권", "대신증권", "메리츠증권", "토스증권", "카카오페이증권",
                      "유안타", "교보증권", "현대차증권", "이베스트", "다올투자",
    # 금융지주·은행 — 증권 자회사를 둔 경쟁 그룹. 지주 차원의 실적·건전성
    # 기사도 당사 리스크가 아니다(7/27 하나금융 NPL·KB금융 대손비용 오탐).
    "하나금융", "KB금융", "신한지주", "우리금융", "농협금융", "BNK금융",
    "DGB금융", "JB금융", "카카오뱅크", "케이뱅크", "토스뱅크",
)
    for a in articles:
        _ent = (a.get("entity") or "")
        _title = a.get("title", "")
        if ("한국투자증권" in _ent or "한국금융지주" in _ent) and "한국투자증권" not in _title:
            if _kw_hit(_title, _OTHER_BROKERS):
                print(f"  [당사 오추출 방어] entity 무효화·참고강등: {_title[:40]}")
                a["entity"] = ""
                a["entities"] = []
                a["grade"] = "참고"
                a["_force_urgent"] = False
                a["customer_notice"] = None

    # ── 경쟁사 자체 리스크 참고 강등/배제 ──
    # 타 증권사가 리스크 주체(제목에 타 증권사 + 그 증권사가 entity)이고 당사가
    # 제목에 없으면, 경쟁사 자체 사건(전산장애·제재·손실)이다.
    # - 익스포저 있음: 고객이 해당 증권주를 보유해 참고용으로 표시 가치가 있으므로
    #   참고로 강등(전사 긴급/주의 발송만 차단, 노출은 유지)
    # - 익스포저 없음: 표시할 익스포저 자체가 없어 참고로 남겨둘 실익이 없고,
    #   경쟁사 내부 사정을 다루는 게 부적절함 — 완전 배제(_excluded=True)
    #   (2026-07-16: 신한투자증권 익스포저 0건 기사가 참고 등급으로 발송된 사례
    #   확인 후 추가 — filter_prompt.txt엔 이미 relevant:false 규칙이 있었으나
    #   AI가 놓친 경우를 코드 레벨에서 한 번 더 차단)
    def _is_same_group(_e: str, _t: str) -> bool:
        """entity가 제목 속 경쟁 증권사와 '같은 그룹'인지.

        '삼성증권 봐주기' 기사에서 entity가 삼성생명/삼성카드로 추출되는 등
        그룹 계열사로 흔들리는 경우를 흡수한다. 단순 접두 2글자 비교로,
        전혀 다른 종목(DI동일 등)에는 적용되지 않는다.
        """
        if len(_e) < 2:
            return False
        _pre = _e[:2]
        return any(_b.startswith(_pre) for _b in _kw_hits(_t, _BROKER_ENTITIES))

    _BROKER_ENTITIES = ("키움증권", "미래에셋증권", "삼성증권", "NH투자증권",
                        "신한투자증권", "KB증권", "하나증권", "대신증권", "메리츠증권",
                        "토스증권", "카카오페이증권", "유안타증권", "교보증권",
                        "현대차증권", "이베스트투자증권", "다올투자증권")
    for a in articles:
        # ★entity 표기 변형 방어(2026-07-29 실측):
        #   정확 일치만 보면 앞뒤 공백·빈값·그룹 계열사명에서 판정이 뚫린다.
        #   실사례: '금융위의 삼성증권 봐주기?…중징계 감경될 듯'이 주의로 발송.
        #   → strip 후 entity/entities를 함께 보고, 그래도 못 찾으면 '제목에
        #     경쟁사명이 있고 당사·타 종목이 주체가 아닌' 경우로 판정한다.
        _ent = (a.get("entity") or "").strip()
        _ents = [(_e or "").strip() for _e in (a.get("entities") or [])]
        _title = a.get("title", "")
        if "한국투자증권" in _title:
            continue  # 당사가 제목에 있으면 당사 관련 사안이므로 제외
        _is_broker_entity = (
            _ent in _BROKER_ENTITIES
            or any(_e in _BROKER_ENTITIES for _e in _ents)
            # entity가 비었거나 같은 그룹 계열사명일 때만 제목으로 보완한다.
            # ★entity가 '경쟁사와 무관한 별개 종목'이면 적용하면 안 된다 —
            #   'NH투자증권 직원이 DI동일 주가조작 가담'처럼 경쟁사가 가해자이고
            #   피해종목(DI동일)이 따로 있는 기사가 강등되면 미탐이 된다
            #   (검증에서 확인).
            or (_kw_hit(_title, _BROKER_ENTITIES)
                and (not _ent or _is_same_group(_ent, _title)))
        )
        if _is_broker_entity and _kw_hit(_title, _OTHER_BROKERS):
            # 익스포저 조회는 '제목에서 찾은 경쟁사'를 우선 사용 — entity가
            # 비어 있으면 조회 자체가 안 돼 잘못 배제될 수 있다.
            if _ent not in _BROKER_ENTITIES:
                _hit = _kw_hits(_title, _BROKER_ENTITIES)
                if _hit:
                    _ent = _hit[0]
            _has_exp = bool(find_exposure(_ent, exposure_data or {}))
            if not _has_exp:
                print(f"  [경쟁사 리스크·익스포저없음 완전배제] {_ent}: {_title[:35]}")
                a["_excluded"] = True
                continue
            if a.get("grade") != "참고":
                print(f"  [경쟁사 자체리스크 참고강등] {_ent}: {_title[:35]}")
            a["grade"] = "참고"
            # ★결정론적 강등은 이후 AI 재검증이 되돌리지 못하도록 잠근다.
            #   (7/29 07시 KB증권 사례: regrade가 참고로 강등했는데
            #    Sonnet 재검증이 주의로 되올려 경쟁사 기사가 주의로 발송됨.
            #    등급이 이미 참고여도 잠가야 하므로 조건문 밖에 둔다.)
            a["_grade_locked"] = True
            a["_force_urgent"] = False
            a["customer_notice"] = None
    articles = [a for a in articles if not a.get("_excluded")]

    # ── ETF·ETN 구조적 상장폐지 등급 상한 (2026-08-14 신설) ──────────────
    # 상관계수 미달·추적오차·순자산 미달·존속기한 만료로 인한 ETF 상폐는
    # 발행사 부실이 아니라 지수 추종 실패에 따른 정리로, 투자자는 NAV 기준
    # 환매를 받는다. '확정된 손실·부실·제재'인 긴급 대역과 성격이 다르다.
    #   실사례(8/14 21시 'TIME 미국배당다우존스액티브'): 상관계수 미달 상폐가
    #   주식 상폐와 동일하게 9.3 긴급으로 발송 — 등급 과대.
    # → 주의 상한. 단 매도 가능 기한(거래정지일)이 있어 고객 안내는 필요하므로,
    #   customer_notice 생성 예외 플래그를 남겨 안내 문구는 유지한다.
    #   기초자산 폭락·조기청산 등 실손실 사유는 이 규칙에 걸리지 않는다.
    # ETF 표식: 종목명에 'ETF'가 없는 상품이 많아(실사례 'TIME 미국배당다우존스
    # 액티브') 브랜드 접두어까지 본다. 나아가 '상관계수 미달·추적오차·존속기한·
    # 순자산 미달'은 ETF·ETN에만 존재하는 상폐 사유라 표식 없이도 인정한다.
    _ETF_MARK_RE = re.compile(
        r'ETF|ETN|상장지수|KODEX|TIGER|KBSTAR|ARIRANG|HANARO|ACE\s|SOL\s|PLUS\s|'
        r'RISE\s|TIME\s|TREX|FOCUS|히어로즈|마이다스|레버리지|인버스|액티브'
    )
    _ETF_ONLY_REASON_RE = re.compile(
        r'상관계수|추적\s*오차|추적오차|존속\s*기한|순자산\s*(?:총액)?\s*미달'
    )
    _ETF_GENERIC_REASON_RE = re.compile(
        r'규모\s*미달|신탁계약\s*해지|자진\s*해지|만기\s*(?:해지|상환)'
    )
    for a in articles:
        _t = a.get("title", "")
        _e = (a.get("entity") or "")
        _ctx = _t + " " + (a.get("summary") or "")
        if not re.search(r'상장\s*폐지|상폐', _t):
            continue
        _marked = bool(_ETF_MARK_RE.search(_t) or _ETF_MARK_RE.search(_e))
        if not (_ETF_ONLY_REASON_RE.search(_ctx)
                or (_marked and _ETF_GENERIC_REASON_RE.search(_ctx))):
            continue
        # (2026-08-19 수정) 사건 판정과 등급 강등을 분리한다.
        # 기존엔 grade == "긴급" 인 건만 검사해, AI가 처음부터 주의를 준 ETF
        # 상폐는 규칙 전체를 건너뛰고 _notice_exempt도 붙지 않았다.
        #   실사례(8/17 21시 TIME 미국배당다우존스액티브): AI가 주의 6.3으로
        #   판정 → 강등 경로를 안 타 고객 안내 문구가 아예 생성되지 않았다.
        #   18일 거래정지가 예정된 건이라 안내가 꼭 필요했다.
        # 안내 필요 여부는 '등급이 어떻게 정해졌는지'가 아니라 '매도 시한이
        # 있는 ETF 상폐인지'라는 사실에 달렸다. 그래서 exempt는 사실에 붙이고,
        # 강등은 긴급인 경우에만 수행한다.
        a["_notice_exempt"] = True     # 등급과 무관하게 고객 안내 문구는 생성한다
        if a.get("grade") == "긴급":
            print(f"  [ETF 구조적 상폐 주의강등] {_e}: {_t[:35]}")
            a["grade"] = "주의"
            a["_grade_locked"] = True  # AI 재검증이 긴급으로 되돌리지 못하게 잠금
            a["_force_urgent"] = False
        else:
            print(f"  [ETF 구조적 상폐 — 안내문구 유지] {_e}: {_t[:35]}")

    # ── 전망·가능성 기사 긴급 과대 강등 (2026-08-19 신설) ────────────────
    # 긴급 대역의 정의는 '확정된 손실·부실·제재'다. 확정되지 않은 전망·위기설
    # 기사가 긴급으로 나가면 임원이 당일 대응할 사건과 구분되지 않는다.
    #   실사례(8/19 14시) 한빛소프트: "한빛, 상장폐지 위험권 … 향후 시장 전망은?"
    #   → 긴급 8.9. 주가 1,000원 하회에 따른 '위험권' 진입 관측 기사로 확정
    #   사건이 아니고, 정작 대응방안도 "하회 지속 시 편입 여부 재확인"이라는
    #   가능성 서술이었다. 고객문구까지 "해당할 수 있습니다"로 나가 민원 소지.
    # 2차 검증 프롬프트에 '가능성·심의 예정이면 주의로' 규칙이 이미 있으나
    # 지켜지지 않은 사례라, 프롬프트 의존을 걷고 코드로 게이트를 둔다.
    # 오강등(미탐)이 더 위험하므로 확정 표현이 하나라도 있으면 손대지 않는다.
    _SPECULATIVE_RE = re.compile(
        r'위험권|위기설|기로|조짐|기미|경고음|빨간불|위태|전망은|전망\?|'
        r'가능성|우려|예상된다|관측된다|할\s*수도|될\s*수도|어쩌나|괜찮나'
    )
    _CONFIRMED_RE = re.compile(
        r'확정|결정|의결|지정|공시|접수|발생|선고|개시|착수|부과|적발|'
        r'정지한다|정지된다|미지급|디폴트|불이행|거절|해지|파산|부도|피소|기소'
    )
    for a in articles:
        if a.get("grade") != "긴급" or a.get("_grade_locked"):
            continue
        _t = a.get("title", "")
        if not _SPECULATIVE_RE.search(_t):
            continue
        if _CONFIRMED_RE.search(_t):
            continue      # 확정 표현이 있으면 전망성으로 보지 않는다
        print(f"  [전망성 기사 긴급→주의] {a.get('entity','')}: {_t[:35]}")
        a["grade"] = "주의"
        a["_grade_locked"] = True

    for a in articles:
        a["_risk_score"] = calc_risk_score(a, exposure_data)

    # ── 당사 직접 이슈 긴급 강제 (confidence·GRADE_LIMITS 면제) ──────────
    # 전사 발송 안전장치: 강제 긴급은 반드시 '한국투자증권'이 제목에 있고
    # 타 증권사가 주체가 아닐 때만. MTS·HTS·전산장애 키워드 단독으로는 강제하지
    # 않는다(키움 MTS 장애 등 타사 기사가 당사 긴급으로 오발송되는 것 방지).
    for a in articles:
        title = a.get("title", "")
        _has_company = DIRECT_COMPANY_KW in title
        _has_other_broker = _kw_hit(title, _OTHER_BROKERS)
        # 당사 직접 이슈: 제목에 한국투자증권이 있고, 그 맥락이 부정적(장애·제재 등)일 때만.
        # MTS/HTS/전산장애 키워드도 '한국투자증권'과 함께 있을 때만 인정.
        _is_direct = (
            _has_company
            and not _has_other_broker
            and _kw_hit(title, (DIRECT_NEGATIVE_KW | DIRECT_INCIDENT_KW))
        )
        if _is_direct:
            if a.get("grade") != "긴급":
                print(f"  [직접이슈 강제긴급] {title[:40]}")
            a["grade"] = "긴급"
            a["_force_urgent"] = True

    # ── 리스크 해소 국면 참고 강등 ──
    # 당사 직접이슈(_force_urgent)는 예외 — 강제긴급 판정을 뒤집지 않는다.
    for a in articles:
        if a.get("_force_urgent"):
            continue
        if a.get("grade") in ("긴급", "주의") and is_risk_resolved(a.get("title", "")):
            print(f"  [리스크 해소 참고강등] {a.get('title','')[:40]}")
            a["grade"] = "참고"
            a["customer_notice"] = None
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
            a["_grade_locked"] = True   # AI 재검증이 되돌리지 못하게 잠금
            ref.append(a)
            print(f"  [confidence 강등] 주의→참고 (conf={conf:.2f}): {a['title'][:30]}")

    for i, a in enumerate(urgent):
        # _force_urgent는 GRADE_LIMITS 상한도 면제
        if a.get("_force_urgent") or i < GRADE_LIMITS["긴급"]:
            result.append(a)
        else:
            a["grade"] = "주의"
            a["customer_notice"] = None
            a["_grade_locked"] = True   # AI 재검증이 되돌리지 못하게 잠금
            caution.append(a)
            print(f"  [강등] 긴급→주의: {a['title'][:35]}")

    caution_sorted = sorted(caution, key=lambda x: x.get("_risk_score") or 0, reverse=True)
    for i, a in enumerate(caution_sorted):
        if i < GRADE_LIMITS["주의"]:
            result.append(a)
        else:
            a["grade"] = "참고"
            a["_grade_locked"] = True   # AI 재검증이 되돌리지 못하게 잠금
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
        entity_val = (a.get("entity") or "").strip()
        if a.get("_force_urgent"):
            continue                         # 당사직접 면제
        if not entity_val:
            # (2026-08-02 계측) 8/2 21시 지역농협 건이 익스포저 없음에도 주의로
            # 발송됐는데, 로컬·러너 재현에서는 모두 정상 강등돼 우회 지점을
            # 특정하지 못했다. 면제로 빠지는 건을 기록해 다음 회차에서 확인한다.
            if a.get("grade") in ("긴급", "주의"):
                print(f"  [강등면제:entity없음] {a.get('grade')}: {a.get('title','')[:40]}")
                _RUN_STATS.setdefault("demote_exempt", []).append(
                    f"entity없음|{a.get('grade')}|{a.get('title','')[:24]}")
            continue                         # 시장전체 이슈 면제 (반대매매·서킷브레이커 등)
        _exp_hit = find_exposure(entity_val, exposure_data or {})
        if _exp_hit:
            if a.get("grade") in ("긴급", "주의"):
                print(f"  [강등면제:익스포저{len(_exp_hit)}행] entity={entity_val!r} "
                      f"{a.get('grade')}: {a.get('title','')[:34]}")
                _RUN_STATS.setdefault("demote_exempt", []).append(
                    f"{entity_val}|exp{len(_exp_hit)}|{a.get('grade')}|{a.get('title','')[:24]}")
            continue                         # 익스포저 있음 — 강등 없음
        # (2026-08-12 추적) 강등 판정 결과를 run_stats에 남긴다.
        # 8/2 지역농협·8/12 수창건설·놀부가 익스포저 없음에도 주의로 발송됐는데,
        # 로컬·러너 재현에서는 모두 정상 강등돼 우회 지점을 특정하지 못했다.
        # Actions 로그는 회수가 어려워 커밋되는 run_stats.jsonl에 기록한다.
        _RUN_STATS.setdefault("no_exp_demote", []).append(
            f"{entity_val}|{a.get('grade')}|{a.get('title','')[:24]}")
        # ★익스포저가 없으면 등급과 무관하게 먼저 잠근다 (2026-08-12).
        #   기존엔 잠금이 '긴급/주의일 때'만 걸려서, Gemini가 처음부터 '참고'로
        #   준 기사는 강등할 게 없어 잠금도 안 걸렸다. 그 틈으로 뒤이은 Sonnet
        #   전건 재검증이 참고→주의로 되올려, 익스포저 0인 기사가 주의로
        #   발송됐다(8/2 지역농협 5.5, 8/12 수창건설 5.8·놀부 5.7).
        #   Sonnet 프롬프트에는 익스포저 유무가 전달되지 않아 기사 내용만 보고
        #   격상하므로, 잠금이 유일한 방어선이다.
        #   방증: 8/12 07시에 주의가 4건 나갔는데 GRADE_LIMITS["주의"]는 3이다.
        #   상한은 이 함수 안에서 걸리므로, 초과분은 함수 이후에 승급된 것이다.
        a["_grade_locked"] = True   # AI 재검증이 되돌리지 못하게 잠금
        # 익스포저 없음 → 참고로 직행 (긴급/주의 불문)
        if a.get("grade") in ("긴급", "주의"):
            prev_grade = a["grade"]
            a["grade"] = "참고"
            a["customer_notice"] = None
            print(f"  [익스포저없음 강등] {prev_grade}→참고: {a['title'][:40]}")
    # ─────────────────────────────────────────────────────────────────

    # ── 최종 등급 기준 confidence 상한 정합 (점수-등급 괴리 방지) ──────
    # 모든 강등(confidence·LIMITS·익스포저없음·주가보정) 완료 후 일괄 적용.
    # 참고 카드가 8점대(파산확정급) 점수를 달고 나가는 모순 제거.
    # 상한은 confidence 강등 기준선과 동일(주의 0.84 / 참고 0.60).
    # 원값은 _conf_raw로 보존 — 필터링 로그·캘리브레이션 추적용.
    _CONF_CAP = {"주의": 0.84, "참고": 0.60}
    for a in result:
        _cap  = _CONF_CAP.get(a.get("grade"))
        _conf = a.get("_ai_confidence") or 0
        if _cap and _conf > _cap and not a.get("_force_urgent"):
            a["_conf_raw"] = _conf
            a["_ai_confidence"] = _cap
            a["_risk_score"] = calc_risk_score(a, exposure_data)
    # ─────────────────────────────────────────────────────────────────

    # 강등 후 등급 카운트 재산출
    urgent_cnt  = sum(1 for a in result if a.get("grade") == "긴급")
    caution_cnt = sum(1 for a in result if a.get("grade") == "주의")
    ref_cnt     = sum(1 for a in result if a.get("grade") == "참고")
    print(f"  등급 조정 완료 → 긴급 {urgent_cnt}건 / 주의 {caution_cnt}건 / 참고 {ref_cnt}건")

    # ── 사건단위 대표기사 선정 ──────────────────────────────────────
    # 키: (entity, event_type, grade) — 동일 사건·동일 등급 내 1건만
    # 우선순위: ① 리스크점수 높은 것 ② 동점 시 미디어 신뢰도 높은 것
    event_seen = {}
    result_deduped = []

    def _media_score(url: str) -> float:
        """MEDIA_TRUST 기반 미디어 점수 — 없으면 0.0"""
        return get_media_boost(url) if url else 0.0

    for a in sorted(result,
                    key=lambda x: (x.get("_risk_score") or 0,
                                   _media_score(x.get("url",""))),
                    reverse=True):
        entity     = (a.get("entity") or "").strip()
        event_type = (a.get("event_type") or "").strip()
        grade      = a.get("grade","")
        event_key  = a.get("event_key","")

        # event_key 기반 dedup — 같은 entity+event_type 조합
        # event_key 없으면 (entity, grade) fallback
        if event_key:
            eg_key = ("ek", event_key, grade)
        elif entity:
            eg_key = ("et", entity, event_type or "", grade)
        else:
            eg_key = None

        if eg_key and eg_key in event_seen:
            print(f"  [사건단위 dedup] 동일사건 제거: [{grade}] {a['title'][:40]}")
            continue
        if eg_key:
            event_seen[eg_key] = True
        result_deduped.append(a)

    return result_deduped


def _verify_high_risk_by_claude(articles: list):
    """Gemini가 분류한 고위험 기사(긴급 전체 + 리스크점수 5.0 이상)를 Sonnet이
    재검증 — 인플레이스 등급 수정. _force_urgent(당사 직접 이슈)는 호출 전에
    이미 제외됨.
    """
    if not articles:
        return

    lines_txt = "\n".join(
        f"{i+1}. [{a.get('entity','')}] {a['title']} "
        f"(현재등급: {a.get('grade','')}, 점수: {a.get('_risk_score',0):.1f}, "
        f"reason: {a.get('reason','')}, conf: {a.get('_ai_confidence',0):.2f})"
        for i, a in enumerate(articles)
    )
    _verify_static = (
        "당신은 한국투자증권 개인고객그룹 리스크 담당자입니다.\n"
        "【역할 구분】 이 단계는 '등급 조정' 전담입니다. 기사의 포함/제외 판정은\n"
        "이후 본문 기반 2차 검수에서 별도로 수행하므로, 여기서는 제목·요약 수준에서\n"
        "판단 가능한 '등급의 과대/과소'만 바로잡으세요.\n"
        "Gemini AI가 아래 기사들을 리스크 등급(긴급/주의/참고)으로 분류했습니다.\n"
        "각 기사의 등급이 실제 내용에 맞는지 재검토하고, 필요시 조정하세요.\n\n"
        "긴급 기준: 상장폐지·거래정지·부도·파산·회생 확정, MTS 장애, 당사 직접 제재 등 확정된 손실·부실\n"
        "주의 기준: 손실 가능성·조사 착수·심의 예정 등 아직 확정 아닌 리스크\n"
        "참고 기준: 직접 손실 없는 동향, 경쟁사 자체 리스크, 배경 설명성 기사\n"
        "강등 판단: 심의 예정·가능성·우려·조사 착수·감사의견 미확정이면 주의로,\n"
        "  직접 손실과 무관한 배경·동향·SNS반응·가십성 소재면 참고로 낮출 것.\n"
        "  제목에 '거래정지'·'상장폐지' 등 하락형 키워드가 있어도, 실제 내용이\n"
        "  단기 급등·투기 과열에 대한 경계 조치(투자경고·투자위험종목 지정)이면\n"
        "  부실이 아니므로 참고로 낮출 것. 판단: '급등', '광란', '폭등', '불쏘시개',\n"
        "  '투기', '과열' 등 상승 맥락 단어가 있으면 하락 리스크로 오인하지 말 것\n"
        "  (예: \"거래정지? 오히려 불쏘시개였다···금호건설 4천원→1만6천원 광란\"\n"
        "   → 주가 폭등 기사이므로 참고로 강등. 반대매매·강제청산 리스크 아님)\n"
        "승격 판단: 실제로는 확정된 손실·부실인데 과소평가됐으면 긴급으로 올릴 것.\n\n"
        'JSON 배열만 반환. 예시: [{"id":1,"grade":"긴급"},{"id":2,"grade":"주의"},{"id":3,"grade":"참고"}]\n\n'
        "검토 대상 기사:"
    )
    _verify_dynamic = f"\n{lines_txt}"
    try:
        _res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "prompt-caching-2024-07-31",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 500,
                "temperature": 0.0,
                "system": "당신은 JSON API입니다. 설명 없이 JSON 배열만 출력하세요.",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": _verify_static,
                     "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": _verify_dynamic},
                ]}],
            },
            timeout=20,
        )
        _res.raise_for_status()
        _raw = (_res.json().get("content", [{}])[0].get("text") or "").strip()
        _raw = _raw.replace("```json", "").replace("```", "").strip()
        _s = _raw.find("["); _e = _raw.rfind("]") + 1
        if _s != -1 and _e > _s:
            _raw = _raw[_s:_e]
        _verdicts = json.loads(_raw)
        _vmap = {v["id"]: v.get("grade") for v in _verdicts if isinstance(v, dict)}
        _valid_grades = {"긴급", "주의", "참고"}
        for i, a in enumerate(articles):
            _vg = _vmap.get(i + 1)
            _og = a.get("grade", "")
            # ★결정론적 규칙(경쟁사 자체리스크·GRADE_LIMITS 상한·confidence·
            #   익스포저없음)으로 강등된 건은 AI 판단이 달라도 되돌리지 않는다.
            #   규칙이 확률적 판단에 밀리면 경쟁사 기사가 긴급으로 나간다
            #   (7/29 18:31 키움증권 전산사고 긴급 6.2 실사례).
            if a.get("_grade_locked"):
                print(f"  [Sonnet 재검증] {_og} 고정(규칙 강등): {a['title'][:40]}")
                continue
            if _vg in _valid_grades and _vg != _og:
                a["grade"] = _vg
                if _vg != "긴급":
                    a["customer_notice"] = None
                print(f"  [Sonnet 재검증] {_og}→{_vg}: {a['title'][:40]}")
            else:
                print(f"  [Sonnet 재검증] {_og} 유지: {a['title'][:40]}")
    except Exception as e:
        print(f"  [Sonnet 재검증] 오류 — 원래 등급 유지: {e}")


def ai_filter_and_grade(articles: list, exposure_data: dict = None) -> list:
    """전체 기사를 50건씩 배치로 나눠 AI 필터링 후 중복 제거"""
    if not articles:
        return []
    result = []
    batch_size = 50
    ai_fail_count = 0
    MAX_AI_FAILS = 3
    _used_gemini = False  # 긴급 재검증 트리거용
    for i in range(0, len(articles), batch_size):
        if ai_fail_count >= MAX_AI_FAILS:
            print(f"  ❗ AI 연속 {MAX_AI_FAILS}회 실패 — circuit breaker 작동, 필터링 중단")
            break
        batch = articles[i:i+batch_size]
        print(f"  배치 {i//batch_size+1}/{-(-len(articles)//batch_size)} 처리 중... ({len(batch)}건)")
        # ── 1차: Gemini Flash / 실패 시 Claude fallback ──────────────
        if GOOGLE_API_KEY:
            batch_result = ai_filter_batch_gemini(batch, offset=i)
            if batch_result is None:
                print(f"  [Gemini 실패] Claude fallback (배치 {i//batch_size+1})")
                _RUN_STATS["gemini_fail"] += 1
                batch_result = ai_filter_batch(batch, offset=i)
            else:
                _used_gemini = True
                _RUN_STATS["gemini_ok"] += 1
        else:
            batch_result = ai_filter_batch(batch, offset=i)
        # ─────────────────────────────────────────────────────────────
        if batch_result is None:
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

    # ── Gemini 사용 시 Sonnet 등급 재검증 (전건) ──────────────────────────
    # 기존엔 '긴급 or 5.0점↑'만 대상이었으나, 참고로 강등된 기사에 6.5점이
    # 매겨지는 등 등급-점수 불일치가 반복돼 전건으로 확대(정확도 우선).
    if _used_gemini:
        _to_verify = [a for a in result if not a.get('_force_urgent')]
        if _to_verify:
            print(f"  [Sonnet 등급 재검증] {len(_to_verify)}건 검증 중...")
            _verify_high_risk_by_claude(_to_verify)
    # ─────────────────────────────────────────────────────────────────

    return result

# ── 계열사 확장 허용 사건유형 (2026-07-31 신설) ──
# GROUP_ENTITIES_MAP 확장은 원래 "관련 계열사 익스포저를 놓치지 말자"는 장치였으나,
# event_type 무관하게 항상 전개돼 개별 종목 사건에도 그룹 전체가 카드에 실렸다.
# 실사례(7/31 07시 SK하이닉스): 프리마켓 1주 체결에서 파생된 개별 종목 가격
# 이벤트인데 SK 계열 11개사가 붙어 카드가 26행이 됐다(본인 2행 + 계열사 24행).
# 계열사 익스포저가 판단에 실제로 필요한 것은 '신용이 그룹으로 전이되는' 사건뿐이다.
#   전이성 있음 → 지주·계열 간 자금지원·교차보증·연쇄 디폴트가 실제로 발생
#   전이성 없음 → 개별 종목·개별 사업장에서 종결
# ※ 횡령배임은 경계 사례(대주주 횡령이 계열 신용에 파급될 수 있음)라 일단 제외.
#   운영하며 미탐이 관측되면 아래 집합에 추가할 것.
_GROUP_CONTAGIOUS_EVENTS = {"파산부도", "기업회생", "유동성위기", "차환실패", "신용등급강등"}

def allow_group_expansion(article: dict) -> bool:
    """이 기사에서 계열사 익스포저까지 표시할지 여부."""
    if not article:
        return False
    if article.get("_force_urgent"):      # 당사 직접 이슈는 기존대로 넓게 본다
        return True
    return (article.get("event_type") or "").strip() in _GROUP_CONTAGIOUS_EVENTS

def build_exposure_html(entity, exposure_data: dict, ref_date: str, border_color: str = "#c0392b", article: dict = None) -> str:
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
            row_key = (row.get("종목명",""), row.get("종목유형",""), row.get("종목코드",""))
            if row_key not in seen_row_keys:
                seen_row_keys.add(row_key)
                all_rows.append(row)

    # 사건 주체 — 정렬 시 최상단 고정용. article["entity"]가 기사가 지목한 종목이며,
    # entities_list는 계열사 확장 결과까지 포함하므로 앞쪽 원본 entity를 우선한다.
    _subject_names = []
    if article and article.get("entity"):
        _subject_names.append(article["entity"])
    for _e in entities_list:
        if _e not in _subject_names:
            _subject_names.append(_e)
            break   # 확장분까지 주체로 보지 않는다 — 원본 1개면 충분

    date_label = f"기준일: {ref_date}" if ref_date else ""
    _AI_BADGE = ('<span style="font-size:10px;font-weight:400;color:#1d4ed8;'
                 'background:#dbeafe;padding:1px 5px;border-radius:2px;margin-left:4px;">'
                 '관련주 AI 추출</span>')

    # ── RS 블랙리스트 (조기반환 경로와 공유) ────────────────────────
    _RS_BL = {
        "삼성전자", "SK하이닉스", "현대차", "LG에너지솔루션", "삼성바이오로직스",
        "현대모비스", "기아", "셀트리온", "POSCO홀딩스", "KB금융", "신한지주",
        "하나금융지주", "삼성생명", "삼성화재", "메리츠금융지주", "카카오", "NAVER",
    }

    def _early_related_html(rs_raw, seen_set):
        """조기반환 경로용 관련주 — '관련주' 배지 + 종목명 나열만 (P1~P3 적용)"""
        rs_list = [s.strip() for s in (rs_raw or []) if s and s.strip()][:3]
        names = []
        for rs_name in rs_list:
            if rs_name in seen_set or rs_name in _RS_BL:
                continue
            if exposure_data and not find_exposure(rs_name, exposure_data):
                continue
            seen_set.add(rs_name)
            names.append(rs_name)
        if not names:
            return ""
        chips = " &nbsp;·&nbsp; ".join(
            f'<span style="font-weight:600;color:#334155;">{n}</span>' for n in names)
        return (
            f'<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:8px;padding-top:6px;border-top:1px dashed #e2e8f0;"><tr>'
            f'<td valign="top" style="width:80px;white-space:nowrap;">'
            f'<span style="font-size:10px;background:#dbeafe;color:#1d4ed8;padding:1px 6px 1px 6px;border-radius:2px 0 0 2px;font-weight:700;">관련주</span>'
            f'<span style="font-size:9px;background:#eff6ff;color:#60a5fa;padding:1px 5px;border-radius:0 2px 2px 0;font-weight:600;">AI 추출</span></td>'
            f'<td style="padding-left:8px;font-size:12px;color:#334155;">{chips}</td>'
            f'</tr></table>'
        )

    # 3개 다 없으면 → 관련주 확인 후 없으면 잔고 없음
    if not all_rows:
        _art = article or {}
        _event_type = _art.get("event_type", "")
        _force = _art.get("_force_urgent", False)
        _allow_related = _force or _event_type in ("시스템장애", "금감원제재", "상장폐지", "거래정지", "기업회생", "파산부도", "PF부실", "신용등급강등", "발행어음부실", "유동성위기", "대규모환매", "감사의견거절", "횡령배임", "차환실패")
        related_name = None
        related_rows = []
        if _allow_related:
            for ent in entities_list:
                cand = RELATED_STOCK_MAP.get(ent)
                if cand:
                    cand_rows = find_exposure(cand, exposure_data) if exposure_data else []
                    if cand_rows:
                        related_name = cand
                        related_rows = cand_rows
                        break
        if related_name and related_rows:
            inner_r = (
                f'<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
                f'<td valign="top" style="width:80px;white-space:nowrap;">'
                f'<span style="font-size:10px;background:#dbeafe;color:#1d4ed8;padding:1px 6px 1px 6px;border-radius:2px 0 0 2px;font-weight:700;">관련주</span>'
            f'<span style="font-size:9px;background:#eff6ff;color:#60a5fa;padding:1px 5px;border-radius:0 2px 2px 0;font-weight:600;">AI 추출</span></td>'
                f'<td style="padding-left:8px;font-size:12px;color:#334155;font-weight:600;">{related_name}</td>'
                f'</tr></table>'
            )
            # AI related_stocks도 추가
            _seen_e = set(entities_list) | {related_name}
            for _ge in entities_list:
                _seen_e.update(GROUP_ENTITIES_MAP.get(_ge, []))
            _ai_rs_html = _early_related_html(_art.get("related_stocks"), _seen_e)
            return f'''<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;">
      <tr><td style="padding:10px 16px;">
        <p style="margin:0 0 6px 0;font-size:11px;font-weight:700;color:#1e293b;">한국투자증권 익스포저
          <span style="font-weight:400;color:#94a3b8;">{date_label}</span>
        </p>
        {inner_r}{_ai_rs_html}
      </td></tr>
    </table>'''
        # RELATED_STOCK_MAP도 없음 → AI related_stocks만 확인
        _seen_e2 = set(entities_list)
        for _ge in entities_list:
            _seen_e2.update(GROUP_ENTITIES_MAP.get(_ge, []))
        _rs2_html = _early_related_html((_art).get("related_stocks"), _seen_e2)
        _inner2 = '<div style="font-size:12px;color:#94a3b8;">잔고 없음</div>' if not _rs2_html else ""
        return f'''<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;">
      <tr><td style="padding:10px 16px;">
        <p style="margin:0 0 4px 0;font-size:11px;font-weight:700;color:#1e293b;">한국투자증권 익스포저
          <span style="font-weight:400;color:#94a3b8;">{date_label}</span>
        </p>
        {_inner2}{_rs2_html}
      </td></tr>
    </table>'''

    # ── 종목유형 분류 ──────────────────────────────────────────────
    STOCK_TYPES     = {"주식"}
    OVERSEAS_TYPES  = {"해외주식"}
    BOND_TYPES      = {"채권"}
    YEOSIN_TYPES    = {"여신", "해외대출"}  # 여신 통합 (신용+대출 → 여신으로 CSV 통일)

    domestic_stock_rows = [r for r in all_rows if r.get("종목유형","") in STOCK_TYPES]
    overseas_stock_rows = [r for r in all_rows if r.get("종목유형","") in OVERSEAS_TYPES]
    bond_rows           = [r for r in all_rows if r.get("종목유형","") in BOND_TYPES]
    yeosin_rows         = [r for r in all_rows if r.get("종목유형","") in YEOSIN_TYPES]

    # 종목명 정규화 — 법인 표기차이 제거 후 그룹핑 키로 사용
    #   "에스엘엘중앙 주식회사" / "에스엘엘중앙" → "에스엘엘중앙"
    #   "중앙일보(주)" / "중앙일보" → "중앙일보"
    _LEGAL_SUFFIX_RE = re.compile(r'(\(주\)|㈜|주식회사)')

    def _canon_name(name: str) -> str:
        return _LEGAL_SUFFIX_RE.sub('', name).strip()

    # 종목명별 잔고·고객수 합계 (법인 표기차이·종목코드 상이 — 분할발행 등 통합)
    # 표시명은 정규화된(법인 표기 제거) 이름 사용
    def _unify_prefix_names(names):
        """법인명 잘림 변형 통합 — 한 이름이 다른 이름의 접두(6자 이상)면 동일
        법인으로 보고 긴(완전한) 이름으로 통일. 엔티티 추출의 '접두 6자 공통 →
        동일 법인' 원칙과 동일 기준.
        실사례(7/24 07시): 채권 종목명이 '제이알글로벌위탁관리'(잘림)와
        '제이알글로벌위탁관리부동산투자회사'로 나뉘어 같은 법인 익스포저가
        카드에 609억/391억 두 줄로 중복 노출됨.
        접두 6자 미만(예: 롯데케미칼↔롯데케미칼타이탄 5자)은 별개 법인
        가능성이 있어 병합하지 않는다."""
        mapping = {}
        for n in names:
            target = n
            for other in names:
                if other != n and len(n) >= 6 and other.startswith(n) and len(other) > len(target):
                    # 우선주는 본주와 별개 종목이므로 병합하지 않는다.
                    # (2026-08-01) 'SK이노베이션'(주식 1,372억)과 'SK이노베이션우'
                    # (38억)가 병합돼 1,410억 한 줄로 표시되고, 대표명까지 우선주로
                    # 잡혀 사건 주체가 'SK이노베이션우'로 보이는 문제가 있었다.
                    if _PREF_STOCK_SUFFIX_RE.fullmatch(other[len(n):]):
                        continue
                    target = other  # n이 other의 접두 → 더 긴(완전한) 이름으로
            mapping[n] = target
        return mapping

    def _merge_by_name(rows):
        canon_names = [_canon_name(r.get("종목명","")) for r in rows]
        name_map = _unify_prefix_names(set(canon_names))
        merged = {}
        for r in rows:
            name = name_map[_canon_name(r.get("종목명",""))]
            bal = _num(r.get("잔고(억)"))
            cus = int(_num(r.get("고객수")))
            if name not in merged:
                merged[name] = {"잔고": 0, "고객수": 0, "뱅잔고": 0.0, "뱅고객수": 0, "영잔고": 0.0, "영고객수": 0, "_ch": False}
            merged[name]["잔고"] += bal
            merged[name]["고객수"] += cus
            if "뱅잔고" in r:  # 20컬럼 스키마 — 채널 병기용 집계
                def _mf(v):
                    try:
                        return _num(v)
                    except (ValueError, TypeError):
                        return 0.0
                merged[name]["뱅잔고"]   += _mf(r.get("뱅잔고"))
                merged[name]["뱅고객수"] += int(_mf(r.get("뱅고객수")))
                merged[name]["영잔고"]   += _mf(r.get("영잔고"))
                merged[name]["영고객수"] += int(_mf(r.get("영고객수")))
                merged[name]["_ch"] = True
        return merged  # {종목명: {잔고, 고객수, (채널합계)}}

    # 여신잔고 합산 — 종목명별 잔고·고객수 합계 (신용+대출+해외대출 통합)
    _merge_yeosin = _merge_by_name

    def _fmt_merged(name, v):
        return (
            f'<div style="font-size:13px;color:#1e293b;line-height:1.7;">'
            f'<span style="font-weight:700;">{name}</span>'
            f' {v["잔고"]:,.0f}억원 / {v["고객수"]:,}명</div>'
        )

    MAX_DISPLAY_ITEMS = 3       # 구(12컬럼) 단일 리스트 표시 개수 — 기존 유지
    CHANNEL_MAX_ITEMS = 1       # 기사와 직접 연동되는 top1만 기본 노출, 나머지는 外 접기(details)

    _C_BANK   = '#2563eb'  # 뱅키스 채널 컬러 (여신표와 동일)
    _C_BRANCH = '#8b5e3c'  # 영업점 채널 컬러
    _VAL_W = 205  # 값 컬럼 고정폭(px) — 헤더·top1·外 각 행이 독립된 nested table이라
                  # width=50% 등 상대폭을 쓰면 행마다 텍스트 길이에 따라 실제 렌더 폭이
                  # 미세하게 달라져 구분선이 행마다 어긋나 보임. 고정 px로 모든 행·헤더에
                  # 동일하게 적용해 세로 구분선이 항상 같은 위치에 오도록 하고, 카드 실제
                  # 폭(약 602px)에 맞춰 값을 계산해 우측 여백도 최소화

    def _ch_val(v, pre):
        """채널 셀 값 — 'X억 (Y명)', 잔고·고객 모두 0이면 '-'"""
        bal, cus = v.get(f"{pre}잔고", 0), v.get(f"{pre}고객수", 0)
        if bal <= 0 and cus <= 0:
            return '<span style="color:#cbd5e1;">-</span>'
        bal_str = f"{bal:,.1f}".rstrip('0').rstrip('.') if bal < 10 else f"{bal:,.0f}"
        return f'{bal_str}억 ({cus:,}명)'

    def _trunc_name(n: str, limit: int = 14) -> str:
        """종목명 말줄임 — 긴 명칭(리츠·SPC 등)이 컬럼을 밀어내 섹션 간
        값 컬럼 정렬이 깨지는 문제 방지 (문자수 제한 + CSS 폭 강제 이중 적용)"""
        short = n if len(n) <= limit else n[:limit - 1] + '…'
        # td width="100"만으로는 긴 한글 종목명이 여전히 넘칠 수 있어(14자 기준으로도
        # 폭 초과 가능) div로 감싸 overflow:hidden 하드 클립 이중 적용 — table-layout:fixed
        # 라 컬럼폭 자체는 보장되지만, 셀 내부 텍스트가 폭을 넘으면 시각적으로 삐져나올 수 있음
        return f'<div style="max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{short}</div>'

    def _fmt_merged_limited(merged: dict) -> str:
        """종목유형 섹션 본문 — 채널 데이터 있으면 종목명|뱅키스|영업점 단일 평면
        3컬럼 표(헤더와 완전히 동일한 컬럼 구조: width=100+205+205, nesting 없음)로
        구성. 상위 2개만 노출, 나머지는 外 요약행(합산 미표기 — 중복고객으로
        단순합산 부정확), 없으면(구 12컬럼) 기존 단일 리스트.
        기사와 직접 연동되는 top1만 기본 노출, 나머지(外)는 <details>로
        접어둠(기본 닫힘) — 클릭하면 개별 종목 행으로 펼쳐짐.
        [정렬 원칙] 중첩 테이블을 쓰면 헤더(단일 레벨)와 데이터 행(중첩 레벨)
        컬럼 폭 계산 경로가 달라져 렌더링 엔진에 따라 미세하게 어긋날 수 있음
        — 헤더·top1·外 모두 동일한 단일 레벨 3컬럼 구조로 통일해 원천 차단."""
        if any(v.get("_ch") for v in merged.values()):
            # [정렬 우선순위] ① 사건 주체(기사가 지목한 종목) ② 잔고 큰 순
            # 계열사 확장이 켜지는 사건(파산·회생·유동성위기 등)에서 잔고순만 쓰면
            # 사건 주체가 접힘(<details>) 안으로 밀려 클릭해야 보이는 문제가 있었다.
            # 실사례(검증 발송): SK이노베이션 회생 기사인데 최상단이 SK하이닉스(잔고
            # 6.4만억)였고, 정작 주체인 SK이노베이션은 '外 10개 종목 더보기' 안에
            # 있었다. 주체는 판단의 기준점이므로 항상 먼저 보여야 한다.
            def _subject_rank(name: str) -> int:
                """주체 정확일치 → 유사명(우선주 등) → 그 외 순."""
                cn = _canon_name(name)
                for i, e in enumerate(_subject_names):
                    ce = _canon_name(e)
                    if cn == ce:
                        return i * 2          # 본주 우선 — 'SK이노베이션'
                    # prefix 매칭은 양쪽 모두 6자 이상일 때만 — 'SK' 같은 짧은
                    # 지주사명이 'SK이노베이션'의 주체 자리를 가로채는 것을 방지
                    # (find_exposure의 6자 prefix 규칙과 동일 기준)
                    if len(cn) >= 6 and len(ce) >= 6 and \
                       (cn.startswith(ce[:6]) or ce.startswith(cn[:6])):
                        return i * 2 + 1      # 유사명 후순위 — 'SK이노베이션우'
                return len(_subject_names) * 2

            items = sorted(merged.items(),
                           key=lambda kv: (_subject_rank(kv[0]),
                                           -max(kv[1].get("뱅잔고", 0), kv[1].get("영잔고", 0))))
            shown, rest = items[:CHANNEL_MAX_ITEMS], items[CHANNEL_MAX_ITEMS:]

            _td_n = 'width="100" style="font-size:12px;font-weight:700;color:#1e293b;padding:8px 6px 8px 0;white-space:nowrap;vertical-align:top;"'
            # 모바일(≤600px)에서는 exp-val-td 폭을 CSS로 축소해 가로 스크롤 방지
            # (width HTML 속성은 !important CSS width로 재정의 시 우선순위 밀림 — 아래 <style> 참고)

            def _row(n, v, muted=False):
                bank = _ch_val(v, "뱅"); branch = _ch_val(v, "영")
                color = "#94a3b8" if muted else "#1e293b"
                fs = "11" if muted else "12"
                return (
                    f'<tr style="border-bottom:1px solid #f8fafc;">'
                    f'<td class="exp-name-td" {_td_n}>{_trunc_name(n)}</td>'
                    f'<td class="exp-val-td" width="{_VAL_W}" align="center" style="box-sizing:border-box;text-align:center;font-size:{fs}px;color:{color};padding:8px 6px;white-space:nowrap;vertical-align:top;">{bank}</td>'
                    f'<td class="exp-val-td" width="{_VAL_W}" align="center" style="box-sizing:border-box;text-align:center;font-size:{fs}px;color:{color};padding:8px 6px;white-space:nowrap;vertical-align:top;">{branch}</td>'
                    f'</tr>'
                )

            top_table = f'<table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;table-layout:fixed;">{"".join(_row(n, v) for n, v in shown)}</table>'

            if not rest:
                return top_table

            rest_table = f'<table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;table-layout:fixed;">{"".join(_row(n, v) for n, v in rest)}</table>'
            # 外 항목은 기본 닫힘(details, open 속성 없음) — 클릭 시 개별 종목 행으로 펼쳐짐.
            # 합산잔고·고객수는 표기하지 않음(중복고객 존재로 단순합산 부정확 — 안내 문구만)
            fold = (
                f'<details style="margin-top:0;">'
                f'<summary style="cursor:pointer;list-style:revert;padding:2px 0;font-size:11px;color:#94a3b8;">'
                f'外 {len(rest)}개 종목 더보기'
                f'</summary>{rest_table}</details>'
            )
            return top_table + fold
        items = sorted(merged.items(), key=lambda kv: -kv[1]["잔고"])
        shown = items[:MAX_DISPLAY_ITEMS]
        rest = items[MAX_DISPLAY_ITEMS:]
        html = "".join([_fmt_merged(n, v) for n, v in shown])
        if rest:
            rest_bal = sum(v["잔고"] for _, v in rest)
            rest_cus = sum(v["고객수"] for _, v in rest)
            html += (
                f'<div style="font-size:12px;color:#94a3b8;line-height:1.7;">'
                f'外 {len(rest)}개 종목 {rest_bal:,.0f}억원 / {rest_cus:,}명 (중복포함)</div>'
            )
        return html

    NONE_HTML = '<div style="font-size:13px;color:#94a3b8;line-height:1.7;">잔고 없음</div>'

    def _section(label, bg, color, rows_html):
        return (
            f'<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:4px;">'
            f'<tr>'
            f'<td valign="top" style="padding-top:6px;width:80px;white-space:nowrap;">'
            f'<span style="font-size:10px;background:{bg};color:{color};padding:1px 5px;border-radius:2px;font-weight:700;">{label}</span>'
            f'</td>'
            f'<td style="padding-left:8px;">{rows_html}</td>'
            f'</tr></table>'
        )

    DIVIDER = (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0;">'
        '<tr><td style="height:1px;background:#e2e8f0;font-size:0;line-height:0;">&nbsp;</td></tr>'
        '</table>'
    )

    # ── 0억 행 숨김 (2026-07-31 신설) ──
    # 잔고가 표시상 "0억"(500만원 미만)인 종목은 카드에 "0억 (7명)" 형태로 노출돼
    # 왔다. 전체 10,864행 중 2,282행(21.0%)이 여기 해당하며, 전부 고객수만 >0이다.
    # 메일 상단의 위험고객 정의(단일종목 여신 1억원 이상)에도 미달해 담당자가 취할
    # 액션이 없는 정보이므로 행에서 제외한다.
    # 다만 해당 종목유형이 '전부' 0억이면 삭제 대신 "소액 (N명)"으로 축약해
    # 보유 고객이 있다는 사실 자체는 남긴다 — 완전 삭제는 미탐이 된다.
    _EXPO_ZERO_EPS = 0.05  # 억 단위. 표시 포맷상 "0억"으로 찍히는 경계

    def _drop_zero(merged: dict) -> tuple:
        """(0억 제외 merged, 제외된 고객수 합계). 채널 스키마(_ch) 없으면 그대로 통과."""
        if not merged or not any(v.get("_ch") for v in merged.values()):
            return merged, 0
        kept, dropped_cust = {}, 0
        for name, v in merged.items():
            if v.get("뱅잔고", 0) < _EXPO_ZERO_EPS and v.get("영잔고", 0) < _EXPO_ZERO_EPS:
                dropped_cust += int(v.get("뱅고객수", 0)) + int(v.get("영고객수", 0))
                continue
            kept[name] = v
        return kept, dropped_cust

    def _section_body(merged: dict) -> str:
        """종목유형 섹션 본문 — 0억 행 제외 후 렌더. 전량 0억이면 소액 축약."""
        kept, dropped_cust = _drop_zero(merged)
        if kept:
            return _fmt_merged_limited(kept)
        if dropped_cust > 0:
            return (f'<div style="font-size:12px;color:#94a3b8;line-height:1.7;">'
                    f'소액 ({dropped_cust:,}명)</div>')
        return NONE_HTML

    sections = []
    yeosin_merged = _merge_yeosin(yeosin_rows)
    yeosin_html = _section_body(yeosin_merged) if yeosin_merged else NONE_HTML

    # ── 국내주식 블록 ────────────────────────────────────────────
    if domestic_stock_rows:
        sections.append(_section("주식잔고", "#fee2e2", "#c0392b",
                                 _section_body(_merge_by_name(domestic_stock_rows))))
        sections.append(_section("여신잔고", "#fef3c7", "#b45309", yeosin_html))

    # ── 해외주식 블록 ────────────────────────────────────────────
    if overseas_stock_rows:
        sections.append(_section("해외주식잔고", "#fee2e2", "#c0392b",
                                 _section_body(_merge_by_name(overseas_stock_rows))))
        sections.append(_section("여신잔고", "#fef3c7", "#b45309", yeosin_html))

    # ── 채권 블록 ────────────────────────────────────────────────
    if bond_rows:
        sections.append(_section("채권잔고", "#ede9fe", "#5b21b6",
                                 _section_body(_merge_by_name(bond_rows))))

    # ── 주식 없고 여신만 있는 경우 ───────────────────────────────
    if not domestic_stock_rows and not overseas_stock_rows and not bond_rows and yeosin_merged:
        sections.append(_section("여신잔고", "#fef3c7", "#b45309", yeosin_html))

    if not sections:
        inner = '<div style="font-size:12px;color:#94a3b8;">잔고 없음</div>'
    else:
        inner = DIVIDER.join(sections)
        # 채널 모드 — 뱅키스 | 영업점 컬럼 헤더.
        # 데이터 행과 '동일한 래핑 구조'(badge td width=80 + padding-left:8px로
        # 감싼 name(100)+val(205)+val(205) 3컬럼 표)를 그대로 재사용 — 헤더만
        # 별도의 단일 평면 6컬럼 표를 썼을 때 데이터 행(2단 래핑)과 컬럼 폭
        # 계산 경로가 달라 미세하게 어긋나던 근본 원인을 제거
        if any('뱅잔고' in r for r in all_rows):
            _bb = 'border-bottom:1px solid #e2e8f0;'
            _header_inner = (
                f'<table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;table-layout:fixed;"><tr>'
                f'<td class="exp-name-td" width="100" style="{_bb}">&nbsp;</td>'
                f'<td class="exp-val-td" width="{_VAL_W}" align="center" style="box-sizing:border-box;text-align:center;padding:2px 6px 5px;font-size:10px;font-weight:700;color:{_C_BANK};{_bb}white-space:nowrap;">뱅키스</td>'
                f'<td class="exp-val-td" width="{_VAL_W}" align="center" style="box-sizing:border-box;text-align:center;padding:2px 6px 5px;font-size:10px;font-weight:700;color:{_C_BRANCH};{_bb}white-space:nowrap;">영업점</td>'
                f'</tr></table>'
            )
            _ch_header = (
                '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:8px;"><tr>'
                f'<td style="width:80px;{_bb}">&nbsp;</td>'
                f'<td style="padding-left:8px;{_bb}">{_header_inner}</td>'
                '</tr></table>'
            )
            inner = _ch_header + inner

    # ── AI 추출 관련주 섹션 ─────────────────────────────────────────
    def _build_related_html(related_stocks_raw: list, seen: set) -> str:
        """AI 추출 관련주 — '관련주' 배지 + 종목명 나열만 (수치 미표기)
        - P1: 최대 3개 제한
        - P2: seen(entities + GROUP_ENTITIES_MAP 계열사) 중복 제외
        - P3: _RS_BL 대형주 제외
        - 당사 익스포저 보유 종목만 나열 (find_exposure 매칭 기준)
        """
        _rs_list = [s.strip() for s in (related_stocks_raw or []) if s and s.strip()]
        _rs_list = _rs_list[:3]  # P1: 코드단 강제 3개 제한
        _names = []
        for rs_name in _rs_list:
            if rs_name in seen or rs_name in _RS_BL:  # P2, P3
                continue
            if exposure_data and not find_exposure(rs_name, exposure_data):
                continue
            seen.add(rs_name)
            _names.append(rs_name)
        if not _names:
            return ""
        _chips = " &nbsp;·&nbsp; ".join(
            f'<span style="font-weight:600;color:#334155;">{n}</span>' for n in _names)
        return (
            f'<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:10px;padding-top:8px;border-top:1px dashed #e2e8f0;"><tr>'
            f'<td valign="top" style="width:80px;white-space:nowrap;">'
            f'<span style="font-size:10px;background:#dbeafe;color:#1d4ed8;padding:1px 6px 1px 6px;border-radius:2px 0 0 2px;font-weight:700;">관련주</span>'
            f'<span style="font-size:9px;background:#eff6ff;color:#60a5fa;padding:1px 5px;border-radius:0 2px 2px 0;font-weight:600;">AI 추출</span></td>'
            f'<td style="padding-left:8px;font-size:12px;color:#334155;">{_chips}</td>'
            f'</tr></table>'
        )

    _art = article or {}
    _rs_raw = _art.get("related_stocks") or []
    # P2: _seen_related에 entities + GROUP_ENTITIES_MAP 계열사 모두 포함
    _seen_related = set(entities_list)
    for _ent in entities_list:
        _seen_related.update(GROUP_ENTITIES_MAP.get(_ent, []))
    related_html = _build_related_html(_rs_raw, _seen_related)

    _title = (
        f'<span style="font-size:11px;font-weight:700;color:#1e293b;">한국투자증권 익스포저</span>'
        f' <span style="font-weight:400;color:#94a3b8;font-size:11px;">{date_label}</span>'
    )

    return f'''<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;">
      <tr><td class="exp-card-td" style="padding:14px 18px;">
        <p style="margin:0 0 10px 0;">{_title}</p>
        {inner}{related_html}
      </td></tr>
    </table>'''

# ── 사건유형 배지 (2026-07-31 신설) ──
# 기존 배지는 a["keyword"](= 크롤링 검색어)를 그대로 노출해, 검색어와 실제 사건이
# 다르면 임원에게 잘못된 라벨이 전달됐다. 실사례(7/30~7/31 발송분 4건):
#   · 차바이오텍 '소액주주 12억 배상 확정' → 배지 "상장폐지"
#   · 더 테크놀로지 '정리매매·상폐 확정'   → 배지 "거래정지"
#   · SK하이닉스(국내주) 파생 청산        → 배지 "해외주식 급락"
# → AI가 판정한 event_type(사건과 일치하도록 프롬프트로 강제됨)을 라벨 원천으로 삼는다.
#   event_type이 없거나 "기타리스크"면 배지를 생략한다 — 틀린 라벨보다 무라벨이 안전.
_EVENT_LABEL = {
    "상장폐지": "상장폐지",       "거래정지": "거래정지",
    "기업회생": "기업회생",       "파산부도": "파산·부도",
    "PF부실": "PF 부실",          "신용등급강등": "신용등급 강등",
    "반대매매": "반대매매",       "금감원제재": "금감원 제재",
    "시스템장애": "시스템 장애",  "발행어음부실": "발행어음 부실",
    "유동성위기": "유동성 위기",  "대규모환매": "대규모 환매",
    "감사의견거절": "감사의견 거절", "횡령배임": "횡령·배임",
    "차환실패": "차환 실패",
}

# ── event_type 근거 검증 (2026-08-19 신설) ──────────────────────────
# event_type은 AI 생성물이라 오분류가 잦다. 점수 계산부는 이미 이를 알고
# 제목 기준으로 가중치를 매기는데(calc_risk_score 주석), 배지 라벨은 AI 값을
# 그대로 표시해 방어가 비대칭이었다.
#   실사례(8/19 14시) 우리금융지주: "신한 3040억 벌 때 우리 -571억…임종룡의
#     해외사업, 왜 거꾸로 가나" = 해외사업 실적 부진 기사인데 '횡령·배임' 배지.
#   실사례(8/19 07시) 위메이드: "주주…금감원에 민원 제기" = 주주가 민원을
#     넣은 기사인데 '금감원 제재' 배지. 제재를 받은 쪽은 위메이드가 아니다.
# 둘 다 대형주(우리금융 41,077명)라 전사 발송됐다. 배지를 믿고 기사를 열었을 때
# 내용이 다르면 시스템 전체의 신뢰가 흔들린다 — 오탐 중에서도 타격이 큰 유형.
# → 사건유형별 근거 표현이 기사(제목+본문 발췌)에 실제로 있을 때만 배지를 단다.
#   근거가 없으면 배지를 생략한다. 위 주석대로 "틀린 라벨보다 무라벨이 안전".
#   등급·점수·발송 여부는 건드리지 않는다 — 라벨 표시만의 문제다.
_EVENT_EVIDENCE = {
    "상장폐지":     r'상장\s*폐지|상폐|퇴출|정리매매|실질\s*심사|상장\s*적격성',
    "거래정지":     r'거래\s*정지|매매거래\s*정지|거래\s*중단|매매\s*중단',
    "기업회생":     r'회생|법정관리|워크아웃|기업개선|자율협약',
    "파산부도":     r'파산|부도|청산|디폴트|채무\s*불이행|폐업',
    "PF부실":       r'PF|프로젝트\s*파이낸싱|브릿지론|대출\s*연체|시행사|본PF',
    "신용등급강등": r'등급\s*(?:하향|강등)|신용등급|아웃룩|부정적\s*검토|워치리스트',
    "반대매매":     r'반대매매|담보\s*부족|마진콜|담보\s*비율',
    # '금감원'만으론 부족하다 — 민원·질의 기사가 제재로 둔갑한다(위메이드 사례)
    "금감원제재":   r'제재|징계|과징금|과태료|기관\s*경고|영업\s*정지|검사\s*착수|'
                    r'중징계|시정\s*명령|경영\s*유의',
    "시스템장애":   r'전산\s*장애|시스템\s*장애|접속\s*장애|먹통|서비스\s*중단|마비',
    "발행어음부실": r'발행어음|어음\s*부실',
    # (2026-08-21 보강) '미상환'이 빠져 정상 건이 과차단됐다.
    #   실사례(8/21 14시 셀루메드): "140억 대여금 회수 '빨간불'…담보 미설정·
    #   만기 미상환" — 유동성위기가 맞는 분류인데 배지가 사라졌다.
    # 채무 불이행 표현은 어미가 다양해(미상환·미지급·연체·회수 불투명) 넓게 잡되,
    # 어느 것도 실적 부진 기사에는 쓰이지 않으므로 오탐 위험은 낮다.
    "유동성위기":   r'유동성|자금난|자금\s*경색|미지급|미상환|연체|상환\s*불능|'
                    r'회수\s*(?:불확실|불투명|의문|지연)|빨간불|손상|대여금|차입금|'
                    r'만기\s*도래|채무\s*보증',
    "대규모환매":   r'환매|펀드런|자금\s*이탈|유출',
    "감사의견거절": r'의견\s*거절|한정\s*의견|부적정|감사\s*의견|검토\s*의견',
    # 실적 부진 기사가 횡령으로 둔갑한다(우리금융 사례) → 수사·비위 표현을 요구
    "횡령배임":     r'횡령|배임|유용|비자금|구속|기소|검찰|압수수색|수사|고발|'
                    r'혐의|송치|영장',
    "차환실패":     r'차환|만기\s*연장\s*실패|롤오버|재발행',
}

def _event_type_supported(a: dict) -> bool:
    """AI가 붙인 event_type이 기사 본문 근거로 뒷받침되는가."""
    ev = (a.get("event_type") or "").strip()
    pat = _EVENT_EVIDENCE.get(ev)
    if not pat:
        return True     # 매핑에 없는 유형은 판정하지 않는다 (기존 동작 유지)
    # reason은 AI 생성물이라 근거로 쓰면 순환논증이 된다 — 기사 원문만 본다.
    src = " ".join(str(a.get(k) or "") for k in ("title", "desc", "body"))
    # 판정할 원문이 사실상 없으면 손대지 않는다. 본문 크롤이 실패하면 desc·body가
    # 비는 회차가 실제로 있어(8/16 대호에이엘), 원문 부재를 '무근거'로 처리하면
    # 정상 사건의 배지까지 사라진다. 미탐(배지 과잉)보다 과차단이 더 나쁘다.
    if len(src.strip()) < 10:
        return True
    return bool(re.search(pat, src))

def _event_badge_label(a: dict) -> str:
    """배지에 표시할 사건유형 라벨. 미확정·기타·무근거면 빈 문자열(배지 생략)."""
    ev = (a.get("event_type") or "").strip()
    if not ev or ev == "기타리스크":
        return ""
    if not _event_type_supported(a):
        print(f"  [사건유형 무근거 — 배지 생략] {a.get('entity','')}: "
              f"{ev} ← {a.get('title','')[:35]}")
        return ""
    if ev in _EVENT_LABEL:
        return _EVENT_LABEL[ev]
    # 프롬프트 어휘 밖의 값 — HTML 안전을 위해 한글·영숫자·공백만 통과
    return re.sub(r'[^0-9A-Za-z가-힣·\s]', '', ev)[:20].strip()

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


# ── 회차 운영 지표 ──────────────────────────────────────────────────────
# Actions 로그는 외부망에서 내려받기 어렵고 90일 뒤 삭제된다. 튜닝 판단에
# 필요한 최소 지표만 레포에 누적해 언제든 조회할 수 있게 한다.
# (Gemini 무료 티어 RPM 초과로 Claude fallback이 얼마나 나는지가 핵심)
_RUN_STATS = {"gemini_ok": 0, "gemini_fail": 0, "gemini_err": "",
              "gemini_model_switched": False}


def save_run_stats(collected: int, selected: int, verify_model: str,
                   self_only: bool, path: str = "run_stats.jsonl"):
    """회차별 운영 지표를 1줄 JSON으로 누적 기록."""
    from datetime import datetime, timezone, timedelta
    _kst = timezone(timedelta(hours=9))
    rec = {
        "ts": datetime.now(_kst).strftime("%Y-%m-%d %H:%M"),
        "collected": collected,
        "selected": selected,
        "gemini_ok": _RUN_STATS["gemini_ok"],
        "gemini_fail": _RUN_STATS["gemini_fail"],
        # 실패 사유를 남겨야 '모델명 오류인지 할당량 초과인지'를 사후에 가린다.
        # (2026-07-29: fallback 100%인데 사유가 없어 진단 불가했음)
        "gemini_err": _RUN_STATS.get("gemini_err", "")[:120],
        "gemini_model": GEMINI_MODEL,   # 전환됐다면 최종 사용 모델
        "verify_model": verify_model,
        "scope": "self" if self_only else "full",
        # 익스포저없음 강등 추적 (2026-08-12) — 강등 대상/면제/최종등급을 남겨
        # 강등이 실행됐는지, 이후 되돌려졌는지 사후 대조한다.
        "no_exp_demote": _RUN_STATS.get("no_exp_demote", [])[:12],
        "demote_exempt": _RUN_STATS.get("demote_exempt", [])[:12],
        "final_grades": _RUN_STATS.get("final_grades", [])[:12],
        # 고객문구 절단 계측 (2026-08-16) — 190자 프롬프트 상한의 준수율을
        # 데이터로 보기 위한 지표. notice_trunc가 0에 수렴하면 상한이 지켜지는
        # 것이고, action_lost가 쌓이면 행동유도 보존 로직 추가를 검토한다.
        "notice_total": getattr(truncate_at_sentence, "total", 0),
        "notice_trunc": getattr(truncate_at_sentence, "truncated", 0),
        "notice_action_lost": getattr(truncate_at_sentence, "action_lost", 0),
    }
    try:
        # 최근 200줄만 유지 — 무한 증식 방지
        lines = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()[-199:]
        lines.append(json.dumps(rec, ensure_ascii=False))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"  [운영지표] Gemini 성공 {rec['gemini_ok']} / fallback "
              f"{rec['gemini_fail']} — {path} 기록")
    except Exception as e:
        print(f"  [운영지표] 기록 실패(무시): {type(e).__name__}")


def _model_label() -> str:
    """메일 헤더에 표기할 AI 모델 라벨.

    2차 본문검증에 실제 사용된 모델을 반영한다. 전체 발송이 예상되는 회차는
    상위 모델(Opus)로 검증하므로, 수신자가 '이 메일이 어느 수준의 검증을
    거쳤는지' 알 수 있어야 한다.
    """
    _m = globals().get("_LAST_VERIFY_MODEL") or CLAUDE_MODEL
    try:
        _tier = _m.split("-")[1].capitalize()      # claude-opus-4-6 → Opus
    except (IndexError, AttributeError):
        _tier = "Sonnet"
    return f"Claude {_tier} / Gemini {GEMINI_MODEL.replace('gemini-', '')}"


def decide_send_scope(filtered: list, exposure_data: dict, ref_date: str = "") -> dict:
    """발송 범위(전체/본인한정)를 판정한다.

    main()에서 인라인으로 처리하던 로직을 함수로 분리한 것.
    사유: 테스트가 이 로직을 '복제'하면 실제 코드가 깨져도 잡지 못한다.
    (2026-07-28 변이 테스트에서 확인 — 시장급락 집계 호출을 지워도
     test_send_decision이 통과했다. 테스트가 자체 재현본을 검사했기 때문)
    → 테스트와 운영이 같은 함수를 호출하도록 추출한다.

    반환: {self_only, force_full, max_score, has_urgent, has_strong_caution,
           market_crash, alerted_count, alerted_rbal, triggers}
    """
    _actionable = [a for a in filtered if a.get("grade") in ("긴급", "주의")]
    _max_score = max((a.get("_risk_score") or 0) for a in _actionable) if _actionable else 0
    _has_urgent = any(a.get("grade") == "긴급" for a in filtered)

    def _strong_caution_ok(a) -> bool:
        """고신뢰 주의 우회 발송 자격 — conf와 '익스포저 규모'를 함께 본다."""
        if a.get("grade") != "주의":
            return False
        if (a.get("_conf_raw") or a.get("_ai_confidence") or 0) < 0.80:
            return False
        _rows = find_exposure((a.get("entity") or "").strip(), exposure_data)
        if not _rows:
            return False
        return sum(_num(r.get("잔고(억)")) for r in _rows) >= STRONG_CAUTION_MIN_EXPOSURE

    _has_strong_caution = any(_strong_caution_ok(a) for a in filtered)

    # ★집계값은 build_price_alert_section()이 함수 속성에 채운다. 이 함수는
    #   build_email_html() 내부에서도 호출되는데, HTML 생성이 판정보다 뒤로
    #   가면 판정 시점엔 0이 읽힌다(7/28 급락장 미발송 사고의 원인).
    #   → 여기서 명시적으로 1회 호출해 확보한다.
    build_price_alert_section(exposure_data, ref_date)
    _alerted_count = getattr(build_price_alert_section, "last_alerted_count", 0)
    _alerted_rbal = getattr(build_price_alert_section, "last_alerted_rbal", 0)
    _market_crash = (_alerted_count >= MARKET_CRASH_STOCK_THRESHOLD
                     and _alerted_rbal >= MARKET_CRASH_RBAL_THRESHOLD)

    _force_full = _has_urgent or _has_strong_caution or _market_crash
    _self_only = (_max_score < SELF_ONLY_MAX_SCORE) and not _force_full

    _triggers = []
    if _has_urgent:
        _triggers.append("긴급 기사 존재")
    if _has_strong_caution:
        _triggers.append(f"고신뢰 주의(conf≥0.80·익스포저 {STRONG_CAUTION_MIN_EXPOSURE:,.0f}억↑)")
    if _market_crash:
        _triggers.append(f"시장급락({_alerted_count}종목·{_alerted_rbal:,.0f}억)")

    return {"self_only": _self_only, "force_full": _force_full,
            "max_score": _max_score, "has_urgent": _has_urgent,
            "has_strong_caution": _has_strong_caution, "market_crash": _market_crash,
            "alerted_count": _alerted_count, "alerted_rbal": _alerted_rbal,
            "triggers": _triggers}


def filter_articles_for_scope(filtered: list, exposure_data: dict, self_only: bool) -> list:
    """발송 범위에 맞춰 메일에 실을 기사를 추린다.

    전체 발송 시 '참고' 등급은 익스포저가 매우 큰 종목만 남긴다.
    (실측: 오탐의 90%가 참고 등급이었고, 참고 자체 오탐률 78%)
    본인 한정 발송에는 전부 유지해 담당자 모니터링 공백을 없앤다.
    """
    if self_only:
        return filtered

    def _keep(a):
        if a.get("grade") != "참고":
            return True
        _e = (a.get("entity") or "").strip()
        if not _e:
            return False
        _bal = sum(_num(r.get("잔고(억)")) for r in find_exposure(_e, exposure_data))
        return _bal >= REF_FULLSEND_MIN_EXPOSURE

    return [a for a in filtered if _keep(a)]


def build_email_html(articles: list, total_count: int = 0, ai_summary: str = '', exposure_data: dict = None, ref_date: str = '', competitor_notices: list = None, today_str: str = '', now_override=None):
    """now_override: 헤더의 '기준 시각'을 명시 지정(수동 보정 발송용).
    자동 발송분과 동일한 기준시각으로 재발송할 때 사용하며,
    미지정 시 기존대로 실행 시각을 쓴다."""
    exposure_data = exposure_data or {}
    now = now_override or datetime.now(timezone(timedelta(hours=9)))
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
              <span style="font-size:16px;font-weight:bold;color:{gs["label_color"]};">{grade}</span>
              <span style="display:inline-block;width:20px;height:20px;line-height:20px;text-align:center;background:{gs["border_left"]};color:#fff;font-size:11px;font-weight:700;border-radius:50%;margin-left:6px;vertical-align:middle;">{len(items)}</span>
            </td>
            <td align="right" class="grade-header-right" style="padding:10px 14px;white-space:nowrap;">
              <span style="font-size:11px;{'background:#fee2e2;color:#c0392b;padding:2px 10px;border-radius:10px;font-weight:600;' if grade == '긴급' else 'color:#94a3b8;'}">{GRADE_DESC[grade]}</span>
            </td>
          </tr>
        </table>'''
        for a in display_items:
            # entities 복수 지원 — 없으면 entity 단수로 fallback
            a_entities = a.get("entities") or ([a.get("entity","")] if a.get("entity") else [])
            # GROUP_ENTITIES_MAP — 계열사 익스포저는 '신용 전이성' 사건에서만 추가.
            # (개별 종목 사건에까지 그룹 전체가 붙어 카드가 비대해지던 문제)
            if allow_group_expansion(a):
                _group_extra = []
                for _ent in list(a_entities):
                    for _extra in GROUP_ENTITIES_MAP.get(_ent, []):
                        if _extra not in a_entities and _extra not in _group_extra:
                            _group_extra.append(_extra)
                if _group_extra:
                    a_entities = list(a_entities) + _group_extra
            if grade == "참고":
                r_risk = display_risk_score(a)
                if r_risk:
                    r_filled = min(int(r_risk), 10)
                    r_bar = "█" * r_filled + "░" * (10 - r_filled)
                    r_score_html = f'<div style="font-size:9px;color:#94a3b8;font-family:monospace;text-align:right;">{r_risk:.1f}<br>{r_bar}</div>'
                else:
                    r_score_html = ""
                rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" class="ref-bg" style="border:1px solid {gs["card_border"]};border-top:none;background:#f8fbff;">
          <tr>
            <td style="padding:6px 16px;font-size:13px;word-break:keep-all;color:#7a9abf;">
              · <a href="{_esc(a['url'])}" style="color:#7a9abf;text-decoration:none;">{_esc(a['title'][:45])}{"..." if len(a['title'])>45 else ""}</a>
              <span style="font-size:10px;color:#94a3b8;margin-left:4px;">{a.get("pub_str","").split("(")[0].strip() if a.get("pub_str") else ""}</span>
            </td>
            <td align="right" valign="middle" style="padding:6px 16px 6px 4px;white-space:nowrap;">{r_score_html}</td>
          </tr>
        </table>'''
            else:
                badges = ""
                _evl = _event_badge_label(a)
                if _evl:
                    badges += f'<span style="display:inline-block;font-size:10px;color:#3b5491;background:#e8f0fe;padding:2px 7px;margin-right:4px;margin-bottom:6px;border-radius:3px;white-space:nowrap;">{_evl}</span>'
                if a.get("entity") and a.get("entity") != _evl:
                    badges += f'<span style="display:inline-block;font-size:10px;color:#7a9abf;background:#f1f5f9;padding:2px 7px;margin-right:4px;margin-bottom:6px;border-radius:3px;white-space:nowrap;">{a["entity"]}</span>'
                badges += _price_badge(a)  # 등락률 뱃지 — 키워드 옆

                if grade == "주의":
                    c_exp_html = build_exposure_html(a_entities, exposure_data or {}, ref_date, border_color=gs["border_left"], article=a)
                    c_action_row = f'<tr><td style="padding:10px 16px;background:#ffffff;border-top:1px solid {gs["card_border"]};border-bottom:1px solid {gs["card_border"]};"><p style="margin:0 0 5px 0;font-size:11px;font-weight:bold;letter-spacing:0.3px;"><span style="background:#dc2626;color:#fff;padding:2px 6px;font-size:10px;margin-right:5px;border-radius:3px;">⚡ 대응방안</span></p><p style="margin:0;font-size:13px;color:#1e293b;line-height:1.6;font-weight:500;word-break:keep-all;">{_esc(a["action"])}</p></td></tr>' if a.get("action") else ""
                    c_exp_row   = f'<tr><td style="padding:0;">{c_exp_html}</td></tr>' if c_exp_html else ""
                    c_risk = display_risk_score(a)
                    if c_risk:
                        c_filled = min(int(c_risk), 10)
                        c_bar = "█" * c_filled + "░" * (10 - c_filled)
                        c_score_html = (
                            f'<div style="text-align:right;min-width:90px;">'
                            f'<div style="font-size:10px;color:#94a3b8;margin-bottom:2px;">리스크 점수</div>'
                            f'<div style="font-size:14px;font-weight:700;color:#b45309;margin-bottom:2px;">{c_risk:.1f}<span style="font-size:9px;color:#94a3b8;font-weight:400;"> / 10</span></div>'
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
                  <td class="badge-wrap" style="word-break:keep-all;">{badges}</td>
                  <td align="right" valign="top" style="white-space:nowrap;padding-left:8px;">{c_score_html}</td>
                </tr>
              </table>
              <a href="{_esc(a['url'])}" class="title-link caution-title" style="font-weight:bold;font-size:15px;text-decoration:none;color:#1e293b;line-height:1.6;word-break:keep-all;display:block;">{_esc(a['title'])}</a>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 0 0;">
                <tr>
                  <td style="font-size:11px;"><a href="{_esc(a['url'])}" style="color:#4a6099;text-decoration:none;">↗ 기사 보기</a></td>
                  <td align="right" style="font-size:12px;color:#94a3b8;">{a.get("pub_str") or ""}</td>
                </tr>
              </table>
            </td>
          </tr>
          {c_action_row}{c_exp_row}
        </table>'''
                else:
                    exposure_html = build_exposure_html(a_entities, exposure_data or {}, ref_date, article=a)
                    if exposure_html and "잔고 없음" not in exposure_html:
                        a["_has_exposure"] = True
                    risk_score = display_risk_score(a)
                    if risk_score:
                        filled = min(int(risk_score), 10)
                        empty  = 10 - filled
                        bar_str = "█" * filled + "░" * empty
                        risk_score_html = (
                            f'<div style="text-align:right;min-width:90px;">'
                            f'<div style="font-size:10px;color:#94a3b8;margin-bottom:2px;">리스크 점수</div>'
                            f'<div style="font-size:14px;font-weight:700;color:#c0392b;margin-bottom:2px;">{risk_score:.1f}<span style="font-size:9px;color:#94a3b8;font-weight:400;"> / 10</span></div>'
                            f'<div style="font-size:9px;color:#c0392b;letter-spacing:1px;font-family:monospace;">{bar_str}</div>'
                            + f'</div>'
                        )
                    else:
                        risk_score_html = ""
                    urgent_badges = ""
                    _evl = _event_badge_label(a)
                    if _evl:
                        urgent_badges += f'<span style="font-size:10px;background:#e8f0fe;color:#3b5491;padding:2px 7px;border-radius:3px;margin-right:4px;margin-bottom:4px;font-weight:600;white-space:nowrap;display:inline-block;">{_evl}</span>'
                    if a.get("entity") and a.get("entity") != _evl:
                        urgent_badges += f'<span style="font-size:10px;background:#f1f5f9;color:#4a6099;padding:2px 7px;border-radius:3px;font-weight:600;white-space:nowrap;display:inline-block;">{a["entity"]}</span>'
                    urgent_badges += _price_badge(a)  # 등락률 뱃지 — 키워드 옆
                    action_row = f'<tr><td class="action-td" bgcolor="#ffffff" style="padding:10px 16px;border-bottom:1px solid {gs["card_border"]};background:#ffffff;"><p style="margin:0 0 5px 0;font-size:11px;font-weight:bold;letter-spacing:0.3px;"><span style="background:#dc2626;color:#fff;padding:2px 6px;font-size:10px;margin-right:5px;border-radius:3px;">⚡ 대응방안</span></p><p style="margin:0;font-size:12px;color:#1e293b;line-height:1.6;font-weight:600;word-break:keep-all;">{_esc(a["action"])}</p></td></tr>' if a.get("action") else ""
                    exposure_row = f'<tr><td style="padding:0;border-top:1px solid #e2e8f0;border-bottom:1px solid {gs["card_border"]};background:#ffffff;">{exposure_html}</td></tr>' if exposure_html else ""
                    notice_text = _esc(truncate_at_sentence(a.get("customer_notice") or "", 200))
                    notice_row = f'<tr><td class="care-td" bgcolor="#f8fafc" style="padding:10px 16px;background:#f8fafc;border-top:1px solid #e2e8f0;"><p style="margin:0 0 5px 0;font-size:11px;font-weight:bold;letter-spacing:0.3px;"><span style="background:#2563eb;color:#fff;padding:2px 6px;font-size:10px;margin-right:5px;border-radius:3px;">✦ AI</span><span style="color:#334155;">고객케어 안내 추천 문구</span></p><p style="margin:0;font-size:12px;color:#334155;line-height:1.7;white-space:pre-line;word-break:keep-all;">{notice_text}</p></td></tr>' if a.get("customer_notice") else ""
                    bottom_box = f'<tr><td bgcolor="#fff8f8" style="background:#fff8f8;border-top:1px solid {gs["card_border"]};padding:0;"><table width="100%" cellpadding="0" cellspacing="0" border="0">{action_row}{notice_row}{exposure_row}</table></td></tr>' if (action_row or exposure_row or notice_row) else ""
                    is_last = (display_items.index(a) == len([x for x in display_items if x.get("grade")=="긴급"]) - 1 + sum(1 for x in display_items if x.get("grade")!="긴급"))
                    divider = "" if is_last else f'<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0;"><tr><td style="padding:0;height:1px;background:#ef4444;font-size:0;line-height:0;">&nbsp;</td></tr></table>'
                    rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid {gs["card_border"]};border-top:none;background:{gs["card_bg"]};margin-bottom:0;">
          <tr>
            <td class="card-bg card-inner" bgcolor="#fff8f8" style="padding:12px 16px;background:#fff8f8;border-bottom:1px solid #f5c6c6;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:8px;">
                <tr>
                  <td class="badge-wrap" style="word-break:keep-all;">{f"{urgent_badges}" if urgent_badges else ""}</td>
                  <td align="right" valign="top" style="white-space:nowrap;padding-left:8px;">{risk_score_html}</td>
                </tr>
              </table>
              <a href="{_esc(a['url'])}" class="title-link" style="font-weight:700;font-size:16px;text-decoration:none;color:#1e293b;line-height:1.6;word-break:keep-all;display:block;">{_esc(a['title'])}</a>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:5px 0 8px 0;">
                <tr>
                  <td style="font-size:13px;"><a href="{_esc(a['url'])}" style="color:#4a6099;text-decoration:none;font-weight:500;">↗ 기사 보기</a></td>
                  <td align="right" style="font-size:12px;color:#94a3b8;">{a.get("pub_str") or ""}</td>
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
    .action-td  {{ background: #ffffff !important; }}
    .care-td    {{ background: #f8fafc !important; }}
    .ref-bg     {{ background: #f8fbff !important; }}
    a           {{ color: #4a6099 !important; }}
  }}
  @media only screen and (max-width: 600px) {{
    .outer {{ padding: 0 !important; }}
    .main {{ width: 100% !important; max-width: 100% !important; }}
    .header-td {{ padding: 16px 16px !important; }}
    .card-td {{ padding: 10px 12px !important; }}
    .summary-td {{ padding: 12px 12px !important; }}
    .rows-td {{ padding: 0 0 12px 0 !important; }}
    .footer-td {{ padding: 12px 12px !important; }}
    .title-link {{ font-size: 14px !important; line-height: 1.5 !important; }}
    .caution-title {{ font-size: 13px !important; }}
    .desc-p {{ font-size: 12px !important; }}
    .action-p {{ font-size: 13px !important; }}
    .dash-num {{ font-size: 20px !important; }}
    .grade-header-right {{ font-size: 10px !important; white-space: normal !important; word-break: keep-all !important; }}
    .ref-date {{ display: none !important; }}
    .card-inner {{ padding: 10px 12px !important; }}
    .action-inner {{ padding: 8px 12px !important; }}
    .care-inner {{ padding: 8px 12px !important; }}
    .badge-wrap {{ word-break: keep-all !important; }}
    .badge-wrap span {{ white-space: nowrap !important; display: inline-block !important; margin-bottom: 4px !important; }}
    .score-num {{ font-size: 12px !important; }}
    /* 여신잔고 표 모바일 최적화 */
    .price-alert-td {{ font-size: 11px !important; padding: 7px 4px !important; white-space: normal !important; }}
    .price-alert-wrap {{ overflow-x: auto !important; -webkit-overflow-scrolling: touch !important; }}
    .price-alert-table th {{ font-size: 10px !important; padding: 6px 3px !important; white-space: normal !important; word-break: keep-all !important; }}
    .price-alert-table td {{ font-size: 11px !important; padding: 7px 3px !important; white-space: normal !important; word-break: keep-all !important; }}
    .loan-hdr-right {{ display: none !important; }}
    /* 한국투자증권 익스포저 카드 모바일 대응 — 고정폭(100+205+205=510px) 컬럼이
       375px 이하 화면에서 가로 스크롤을 유발하던 문제(Playwright 실측 48px 초과 확인).
       정렬 정확도를 위해 데스크톱은 고정 px를 유지하되, 모바일에서만 CSS width로
       재정의(HTML width 속성보다 우선순위 높음)해 축소 */
    .exp-card-td {{ padding: 10px 12px !important; }}
    .exp-name-td {{ width: 58px !important; font-size: 11px !important; padding-right: 4px !important; }}
    .exp-val-td {{ width: 68px !important; font-size: 10px !important; padding: 6px 3px !important; white-space: normal !important; word-break: keep-all !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;color-scheme:light only;font-family:'Apple SD Gothic Neo','Malgun Gothic',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f1f5f9;">
<tr><td align="center" class="outer" style="padding:0;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" class="main" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">

  <!-- 헤더 H-3 -->
  <tr>
    <td class="header-td" style="background:#3b5491;padding:18px 26px 14px;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:12px;">
        <tr>
          <td valign="middle">
            <p style="margin:0 0 4px 0;font-size:19px;font-weight:bold;color:#ffffff;">🤖 개인고객그룹 리스크 탐지봇</p>
            <p style="margin:0 0 3px 0;font-size:10px;color:#c8d8f0;text-align:right;">{_model_label()}</p>
            <p style="margin:0;font-size:13px;color:#c8d8f0;">{now.strftime('%Y년 %m월 %d일 %H:%M')} 기준 (KST)</p>
          </td>
        </tr>
      </table>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:6px;">
        <tr>
          <td style="font-size:11px;color:#c8d8f0;">
            수집 {total_count}건 &nbsp;·&nbsp; <span style="color:#6ee7b7;font-weight:600;">{len(articles)}건 선별</span>
          </td>
          <td align="right" style="font-size:12px;white-space:nowrap;">
            <span style="color:#ef4444;font-weight:700;">긴급 {len(sections['긴급'])}</span>
            <span style="color:#4a6099;margin:0 5px;">·</span>
            <span style="color:#f59e0b;font-weight:700;">주의 {len(sections['주의'])}</span>
            <span style="color:#4a6099;margin:0 5px;">·</span>
            <span style="color:#94a3b8;font-weight:700;">참고 {len(sections['참고'])}</span>
          </td>
        </tr>
        {f'<tr><td colspan="2" style="padding-top:5px;font-size:11px;color:#c8d8f0;letter-spacing:0.2px;">💡 {_esc(ai_summary)}</td></tr>' if ai_summary else ""}
      </table>
    </td>
  </tr>

  {('<tr><td>' + build_competitor_html(competitor_notices or [], today_str) + '</td></tr>') if competitor_notices else ""}

  <tr><td style="padding:0;">{build_price_alert_section(exposure_data, ref_date)}</td></tr>

  <tr><td style="padding:0;">
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:14px;border-top:3px solid #3b5491;border-bottom:1px solid #e2e8f0;background:#f8fafc;">
      <tr>
        <td style="padding:10px 14px;">
          <span style="font-size:16px;font-weight:700;color:#3b5491;">📰 리스크 뉴스</span>
        </td>
        <td align="right" style="padding:10px 14px;font-size:10px;color:#94a3b8;white-space:nowrap;">
          긴급 {len(sections['긴급'])} &nbsp;·&nbsp; 주의 {len(sections['주의'])} &nbsp;·&nbsp; 참고 {len(sections['참고'])}
        </td>
      </tr>
    </table>
  </td></tr>

  <tr><td class="rows-td" style="padding:0 0 12px 0;">{rows if rows else '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:14px;"><tr><td style="padding:32px 24px;text-align:center;"><p style="margin:0;font-size:15px;color:#94a3b8;line-height:1.8;">리스크에 해당하는 뉴스가 없습니다.<br><span style="font-size:13px;color:#cbd5e1;">여신잔고 위험고객 현황을 확인하시기 바랍니다.</span></p></td></tr></table>'}</td></tr>

  <tr>
    <td class="footer-td" style="padding:14px 22px;background:#fff;border-top:1px solid #e2e8f0;">
      <p style="margin:0;font-size:13px;color:#94a3b8;line-height:2.0;">
        본 이메일은 Claude, Gemini가 심층 분석·선별하여 발송합니다.<br>
        담당자 &nbsp;최진후 차장
      </p>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:12px;border-top:1px solid #e2e8f0;padding-top:10px;">
        <tr>
          <td style="font-size:12px;font-weight:700;color:#4a6099;padding-bottom:6px;" colspan="2">리스크 점수 기준 — 점수 구간이 곧 <b>등급</b>입니다</td>
        </tr>
        <tr>
          <td style="font-size:11px;padding:2px 0;color:#c0392b;font-weight:600;width:80px;">7.0 ~ 10.0</td>
          <td style="font-size:11px;padding:2px 0;color:#7a9abf;"><b>긴급</b> — 확정된 손실·부실·제재 · 당일 내 확인·점검 필요</td>
        </tr>
        <tr>
          <td style="font-size:11px;padding:2px 0;color:#b7791f;font-weight:600;">5.0 ~ 7.0</td>
          <td style="font-size:11px;padding:2px 0;color:#7a9abf;"><b>주의</b> — 손실·부실 가능성 · 주시 및 선제 점검 권고</td>
        </tr>
        <tr>
          <td style="font-size:11px;padding:2px 0;color:#7a9abf;font-weight:600;">0 ~ 5.0</td>
          <td style="font-size:11px;padding:2px 0;color:#7a9abf;"><b>참고</b> — 직접 손실 없는 동향 · 참고 파악용</td>
        </tr>
        <tr>
          <td style="font-size:11px;padding:6px 0 0 0;color:#94a3b8;" colspan="2">점수는 등급 대역 안에서 AI 확신도·사건 키워드·익스포저 규모를 반영해 산출됩니다. 같은 등급 안에서 대응 순서를 정할 때 참고하세요 — 점수만으로 등급이 바뀌지는 않습니다.</td>
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
<tr><td align="center" class="outer" style="padding:0;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" class="main" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">
  <tr>
    <td class="header-td" style="background:#3b5491;padding:22px 26px;">
      <p style="margin:0 0 6px 0;font-size:20px;font-weight:bold;color:#ffffff;">🤖 개인고객그룹 리스크 탐지봇
        <span style="font-size:12px;color:#ffffff;padding:2px 8px;background:#5a7abf;margin-left:8px;">{_model_label()}</span>
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
      <p style="margin:0;font-size:13px;color:#94a3b8;line-height:2.0;">
        본 이메일은 Claude, Gemini가 심층 분석·선별하여 발송합니다.<br>
        담당자 &nbsp;최진후 차장
      </p>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:12px;border-top:1px solid #e2e8f0;padding-top:10px;">
        <tr>
          <td style="font-size:12px;font-weight:700;color:#4a6099;padding-bottom:6px;" colspan="2">리스크 점수 기준 — 점수 구간이 곧 <b>등급</b>입니다</td>
        </tr>
        <tr>
          <td style="font-size:11px;padding:2px 0;color:#c0392b;font-weight:600;width:80px;">7.0 ~ 10.0</td>
          <td style="font-size:11px;padding:2px 0;color:#7a9abf;"><b>긴급</b> — 확정된 손실·부실·제재 · 당일 내 확인·점검 필요</td>
        </tr>
        <tr>
          <td style="font-size:11px;padding:2px 0;color:#b7791f;font-weight:600;">5.0 ~ 7.0</td>
          <td style="font-size:11px;padding:2px 0;color:#7a9abf;"><b>주의</b> — 손실·부실 가능성 · 주시 및 선제 점검 권고</td>
        </tr>
        <tr>
          <td style="font-size:11px;padding:2px 0;color:#7a9abf;font-weight:600;">0 ~ 5.0</td>
          <td style="font-size:11px;padding:2px 0;color:#7a9abf;"><b>참고</b> — 직접 손실 없는 동향 · 참고 파악용</td>
        </tr>
        <tr>
          <td style="font-size:11px;padding:6px 0 0 0;color:#94a3b8;" colspan="2">점수는 등급 대역 안에서 AI 확신도·사건 키워드·익스포저 규모를 반영해 산출됩니다. 같은 등급 안에서 대응 순서를 정할 때 참고하세요 — 점수만으로 등급이 바뀌지는 않습니다.</td>
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
    ai_conf_raw_map      = {a.get("title",""): a.get("_conf_raw") for a in ai_filtered if a.get("_conf_raw")}

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
            "conf_raw"  : ai_conf_raw_map.get(title),  # 클램프 발동 시 원값 (캘리브레이션 추적)
            # 2차 검증(Claude 본문 판독)의 구조화 판정 근거.
            # 오탐 발생 시 ①핵심사건 파악 ②손실주체 판단 ③확정여부 중
            # 어느 단계에서 틀렸는지 사후 추적하기 위해 저장한다.
            "judgment"  : a.get("_judgment"),
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

def _sanitize_header_text(text: str) -> str:
    """헤더(Subject/표시명) 전용 새니타이저 — 게이트웨이가 거부하는 기호 유니코드만 제거.
    사유: 일부 수신 게이트웨이가 '550 5.7.1 unicode character in disallowed header'로
    거부(7/18 인사이트봇 반송 실사례, 원인=전각대시·이모지). ASCII는 거부 사유가
    될 수 없으므로 ASCII 전체(0x20~0x7E)를 통째로 허용해 'M&A', '!', '?' 등이
    깨지지 않게 한다. 한글(완성형+자모)도 수개월 정상 배달 이력이 있어 허용,
    그 외(이모지·전각기호 등)만 제거한다. HTML 본문에는 적용하지 않는다."""
    text = text.replace("—", "-").replace("–", "-")   # 전각대시 → ASCII 하이픈
    return "".join(
        c for c in text
        if (0x20 <= ord(c) <= 0x7E)                    # ASCII 전체(문장부호 포함)
        or ("\uac00" <= c <= "\ud7a3")                 # 한글 완성형
        or ("\u3131" <= c <= "\u3163")                 # 한글 자모
    ).strip()


def _from_header() -> str:
    """발신자 From 헤더를 RFC 2047 규격으로 인코딩해 반환.
    기존엔 f"❗ 개인고객그룹 리스크봇 <{EMAIL_SENDER}>" 형태로 한글·이모지가
    섞인 문자열을 인코딩 없이 그대로 헤더에 넣었음 — 이러면 이메일 클라이언트마다
    헤더 파싱 결과가 달라져(RFC 5322 위반 소지), 일부 클라이언트에서 발신자
    표시명이 깨지거나 CC에 포함된 구글그룹 주소(risk_aigent@googlegroups.com)로
    잘못 표시되는 현상이 있었음. formataddr()+Header()로 표시명만 RFC 2047
    인코딩하고 주소는 순수 ASCII로 분리해 모든 클라이언트에서 일관되게
    "❗리스크봇"으로만 표시되도록 함 (주소는 표시명 뒤에 숨어
    기본 목록 뷰에서는 노출되지 않음 — 클릭·원본보기 시에는 SMTP 특성상 확인 가능)"""
    return formataddr((str(Header(_sanitize_header_text("리스크봇"), "utf-8")), EMAIL_SENDER))


def _addr_header(addr: str) -> str:
    """수신자(To/Cc) 표시명도 발신자와 동일하게 '❗리스크봇'으로 통일해 반환.
    risk_vip@googlegroups.com 같은 그룹 주소 자체가 목록에 그대로 노출되던 것을
    표시명으로 가려 발신자·수신자 전부 '❗리스크봇'만 보이도록 함 — 주소는
    From과 마찬가지로 클릭·원본보기 시에만 확인 가능(SMTP 특성상 완전 은닉 불가)."""
    return formataddr((str(Header(_sanitize_header_text("리스크봇"), "utf-8")), addr))


_MSGID_DOMAIN = (EMAIL_SENDER.split("@", 1)[1] if "@" in EMAIL_SENDER else "localhost")


def _build_msg(subject: str, html_body: str, to_addr: str) -> MIMEMultipart:
    """단일 수신자용 메시지를 완전한 헤더로 구성해 반환.
    - Date·Message-ID: RFC 5322 필수 헤더. MIMEMultipart()+sendmail() 조합은
      이 둘을 자동으로 채워주지 않는다(send_message()라면 자동 보완되지만
      sendmail()+as_string() 조합엔 그런 보정이 없음) — Date 누락은 특히
      Gmail 등이 정책위반으로 조용히 거부(5.7.1류)할 수 있는 요인이라 필수.
    - To: 항상 이 메시지의 실제 SMTP envelope 수신자(to_addr) 하나와
      정확히 일치시킴 — 헤더와 envelope이 불일치하는 BCC 방식은 기업
      보안 게이트웨이(Exchange Online/Defender 등)가 스팸 패턴으로 보고
      무음 차단하는 경우가 있어, 발신 대상 각각에 개별 발송하며 To를
      항상 그 수신자로 맞춘다."""
    msg = MIMEMultipart("alternative")
    msg["Subject"]    = _sanitize_header_text(subject)
    msg["From"]       = _from_header()
    msg["To"]         = _addr_header(to_addr)
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=_MSGID_DOMAIN)
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


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
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e2e8f0;">
  <tr>
    <td style="background:#7f1d1d;padding:20px 26px;">
      <p style="margin:0 0 4px 0;font-size:19px;font-weight:bold;color:#ffffff;">❗ 개인고객그룹 리스크 탐지봇 — 런타임 오류</p>
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

    msg = _build_msg(f"❗ [리스크봇 오류] {now_str} 기준 — 런타임 오류 발생", html_body, receiver)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.ehlo()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            refused = server.sendmail(EMAIL_SENDER, [receiver], msg.as_string())
        if refused:
            print(f"  ⚠️ 오류 메일 수신자 거부됨: {refused}")
        else:
            print(f"  오류 메일 발송 완료 → {receiver}")
    except Exception as e:
        print(f"  오류 메일 발송 실패: {e}")

def send_email_no_result(subject: str, html_body: str):
    """결과 없을 때 특정인(NO_RESULT_RECEIVER)에게만 발송"""
    receiver = NO_RESULT_RECEIVER if NO_RESULT_RECEIVER else EMAIL_SENDER
    msg = _build_msg(subject, html_body, receiver)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.ehlo()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            refused = server.sendmail(EMAIL_SENDER, [receiver], msg.as_string())
        if refused:
            print(f"  ⚠️ 결과없음 메일 수신자 거부됨: {refused}")
        else:
            print(f"  결과없음 메일 발송 완료 → {receiver}")
    except smtplib.SMTPAuthenticationError as e:
        print(f"  결과없음 메일 인증 실패 (앱 비밀번호 확인 필요): {e}")
    except smtplib.SMTPException as e:
        print(f"  결과없음 메일 발송 실패 (SMTP): {e}")
    except Exception as e:
        print(f"  결과없음 메일 발송 실패: {e}")

def _notify_partial_failure(subject: str, refused: dict, total: int):
    """일부/전원 수신자 거부 시 본인에게 경고 메일 별도 발송 (자기 자신도
    거부 대상에 포함돼 있으면 이 경고 자체도 실패할 수 있음 — 그 경우
    GitHub Actions 로그가 최후 수단)"""
    _warn = _build_msg(
        f"⚠️ [리스크봇] 수신자 {len(refused)}/{total}명 거부됨 — {subject[:40]}",
        f"<p>본문 메일 발송 중 다음 수신자가 실패했습니다:</p><pre>{_esc(str(refused))}</pre>",
        EMAIL_SENDER,
    )
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.ehlo()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, [EMAIL_SENDER], _warn.as_string())
    except Exception as _e:
        print(f"  ⚠️ 거부 경고 메일 발송도 실패: {_e}")


def send_email(subject: str, html_body: str, self_only: bool = False):
    """리스크 메일 발송.
    self_only=True면 전체 수신자 대신 보낸사람(NO_RESULT_RECEIVER 우선, 없으면 SENDER)
    에게만 발송한다. 카드 본문(html_body)은 동일하게 유지된다.

    [BCC(envelope-only) → 수신자별 개별발송 전환]
    기존엔 To를 발신자 본인으로 고정하고 실제 수신자(risk_vip 등)는 SMTP
    envelope(RCPT TO)에만 넣는 순수 BCC 방식이었음 — 헤더의 To/Cc가 실제
    envelope 수신자와 전혀 일치하지 않는 상태로 발송하면, 기업 보안
    게이트웨이(Exchange Online/Defender 등)가 스팸/벌크메일의 전형적
    패턴으로 보고 무음 차단하는 경우가 있다(개인 Gmail끼리는 통과해
    테스트 단계에서 놓치기 쉬움). 수신자 한 명당 메시지를 개별 구성해
    To를 항상 그 수신자와 정확히 일치시키는 방식으로 전환 — 수신자끼리
    서로의 주소를 볼 수 없다는 원래 목적(숨은참조)은 그대로 유지된다
    (각자 자기 앞으로 온 메일만 받으므로).
    """
    if self_only:
        targets = [NO_RESULT_RECEIVER if NO_RESULT_RECEIVER else EMAIL_SENDER]
    else:
        # 발신자 본인도 명시적으로 대상에 포함 — To 헤더에만 넣고 envelope에서
        # 빠뜨리면 본인도 미착신되는 문제를 방지. dict.fromkeys로 순서 유지 중복제거
        targets = list(dict.fromkeys([EMAIL_SENDER] + EMAIL_RECEIVERS + EMAIL_CC))

    pending = list(targets)
    succeeded = []
    all_refused = {}
    last_exc = None

    for attempt in range(3):
        if not pending:
            break
        still_pending = []
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.ehlo()
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                for addr in pending:
                    msg = _build_msg(subject, html_body, addr)
                    try:
                        refused = server.sendmail(EMAIL_SENDER, [addr], msg.as_string())
                        # sendmail()은 수신자 중 최소 1명만 성공하면 예외를 던지지
                        # 않고 정상 반환한다(여기선 수신자가 1명뿐이므로 해당 없지만,
                        # 방어적으로 반환값을 항상 확인) — 거부는 반환 dict로만 옴
                        if refused:
                            all_refused.update(refused)
                        else:
                            succeeded.append(addr)
                    except smtplib.SMTPRecipientsRefused as e:
                        all_refused.update(getattr(e, "recipients", {addr: (550, b"refused")}))
                    except (smtplib.SMTPServerDisconnected, smtplib.SMTPException) as e:
                        last_exc = e
                        still_pending.append(addr)
        except smtplib.SMTPAuthenticationError as e:
            print(f"이메일 인증 실패 (비밀번호/앱 비밀번호 확인 필요): {e}")
            raise
        except smtplib.SMTPException as e:
            last_exc = e
            still_pending = still_pending or list(pending)
        pending = still_pending
        if pending and attempt < 2:
            wait = 10 * (2 ** attempt)
            print(f"이메일 발송 실패({len(pending)}명, SMTP): {last_exc} — {wait}초 후 재시도")
            time.sleep(wait)

    if pending:  # 3회 재시도에도 연결/전송 자체가 실패한 수신자
        for addr in pending:
            all_refused[addr] = ("connection_failed", str(last_exc))

    if self_only:
        if succeeded:
            print(f"이메일 발송 완료 (보낸사람 한정 → {', '.join(succeeded)})")
        if all_refused:
            print(f"  ⚠️ 본인한정 발송 실패: {all_refused}")
            raise RuntimeError(f"본인한정 메일 발송 실패: {all_refused}")
        return

    print(f"이메일 발송 완료 ({len(succeeded)}/{len(targets)}명 성공)"
          + (f", 실패 {len(all_refused)}명" if all_refused else ""))

    if all_refused:
        print(f"  ⚠️ 수신자 거부/실패: {all_refused}")
        _notify_partial_failure(subject, all_refused, total=len(targets))

    if len(all_refused) == len(targets):
        # 전원 실패 — "발송완료" 로그로 오인되지 않도록 무음 실패를 예외로 승격
        raise RuntimeError(f"SMTP 전원 거부(무음 실패 방지용 예외 승격): {all_refused}")

def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 뉴스 모니터링 시작")
    now_kst         = datetime.now(timezone(timedelta(hours=9)))
    now_str_full    = now_kst.strftime("%m월 %d일 %H시")
    seen_urls           = load_seen_urls()
    seen_combos         = load_seen_combos()
    seen_stages         = load_seen_stages()   # (entity, stage_kw) 3일 — stage 기반 dedup
    new_stages_this_run = set()
    seen_context        = load_seen_context()
    seen_entities_today = load_seen_entities_today()
    known_entities      = load_known_entities()  # entity별 최초 발송 days_ago
    # ── known_cases.json 종목 seed 병합 ──
    # 기지 사건(홈플러스·중앙그룹·금양 등) 종목은 seen 이력이 없어도
    # 처음부터 D+3(강등)으로 취급. NEXT_STAGE(신규 법적단계)는 아래 강등 로직에서
    # is_next_stage 예외로 자동 통과되므로 진짜 새 사건은 정상 발송된다.
    for _kce in load_known_case_entities():
        if known_entities.get(_kce, 0) < 3:
            known_entities[_kce] = 3
    if seen_entities_today:
        print(f"  오늘 발송 entity: {sorted(seen_entities_today)}")
    if known_entities:
        aged = {e: d for e, d in known_entities.items() if d >= 3}
        if aged:
            print(f"  장기 이슈 entity(3일↑): {aged}")
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
        subject = f"❗ [리스크 탐지] {now_str_full} 기준 — 신규 뉴스 없음"
        send_email_no_result(subject, build_empty_html(now))
        save_seen_urls(seen_urls)
        save_filter_log([], [], [], [])
        return

    before_hard = len(raw_articles)
    hard_excluded_articles = []
    raw_articles_kept      = []
    for _a in raw_articles:
        _excl, _reason = is_hard_excluded(_a.get("title",""), _a.get("desc",""), _a.get("url",""))
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

    # 새 법적 국면 진행 신호 — 동일 entity·사건이어도 새 단계면 재발송.
    # ※ 리스크 '해소' 신호(기각·취하·철회·재개·재상장·상장유지·보류)는 제외.
    #   해소 기사는 하드필터·프롬프트에서 이미 false 처리되며, 여기서 재발송
    #   트리거로 삼으면 "해소됐다"는 알림이 반복돼 알림 피로만 유발한다.
    NEXT_STAGE_KEYWORDS = [
        "가처분", "효력정지", "집행정지", "이의신청", "항고", "재항고",
        "인용", "판결",
        "파산선고", "청산절차", "청산 절차", "청산신청", "청산 신청", "폐업", "법정관리",
        "회생신청", "회생 신청", "회생인가", "회생계획",
        "변제", "채무조정", "출자전환",
        "추가제재", "과징금", "검찰고발", "수사착수",
        "확정판결", "최종확정", "선고확정",
        # 새 단계 진행 — 동일 entity여도 새 사건으로 탐지
        "워크아웃 개시", "워크아웃 신청", "워크아웃 확정",
        "추가 계열사", "신규 회생", "추가 부도",
        "상폐 확정", "상장폐지 확정", "상장폐지 결정",
        "검찰 기소", "구속 영장", "구속 기소",
    ]

    def _stage_hits(title: str, desc: str) -> list:
        """매칭된 stage 키워드 추출 — 제목 우선, desc는 제목 무매칭 시만 (우연 매칭 오기록 방지)"""
        hits = [kw for kw in NEXT_STAGE_KEYWORDS if kw in (title or "")]
        if not hits:
            hits = [kw for kw in NEXT_STAGE_KEYWORDS if kw in (desc or "")]
        return hits

    def is_next_stage(title: str, desc: str, entity: str = "") -> bool:
        # 해소성 표현 가드 — '가처분 기각', '집행정지 인용'(=상폐 정지=해소),
        # '미지급금 청산'(=완납) 등 악화가 아닌 리스크 완화 국면은 재발송
        # 트리거로 보지 않는다.
        # 단, 아래 _ALWAYS_NEW(확정적 신규 악화 이벤트)는 해소 표현이 같은
        # 기사에 섞여 있어도 항상 새 국면으로 인정한다 — 복합 기사(예: "회생
        # 신청, 미지급금은 청산 완료")에서 진짜 새 소식이 묻히는 것을 방지.
        # ('가처분'처럼 그 자체가 기각/인용 여부에 따라 의미가 뒤집히는 키워드는
        # _ALWAYS_NEW에 넣지 않는다 — 여기 넣으면 '가처분 기각'도 새 국면으로
        # 오판하게 됨)
        _RESOLVE_KW = ("기각", "취하", "철회", "각하", "거래재개", "거래 재개",
                       "상장유지", "재상장", "정상화", "해제", "졸업",
                       "전액 지급", "전액 청산", "미지급금 지급", "미지급금 청산",
                       "완납", "완제")
        _ALWAYS_NEW = ("회생신청", "회생 신청", "회생인가", "회생계획",
                       "파산선고", "청산절차", "청산 절차", "청산신청", "청산 신청",
                       "워크아웃 개시", "워크아웃 신청", "워크아웃 확정",
                       "추가 계열사", "신규 회생", "추가 부도",
                       "상폐 확정", "상장폐지 확정", "상장폐지 결정",
                       "검찰 기소", "구속 영장", "구속 기소", "법정관리")
        _t = (title or "") + " " + (desc or "")
        hits = _stage_hits(title, desc)
        if any(rk in _t for rk in _RESOLVE_KW):
            hits = [h for h in hits if h in _ALWAYS_NEW]
            if not hits:
                return False
        if not hits:
            return False
        if not entity:
            return True  # entity 미지정 시 기존 동작 유지
        # 매칭 키워드 전부가 이미 발송된 stage면 차단, 하나라도 새 키워드면 통과
        return any((entity, kw) not in seen_stages for kw in hits)

    before_combo = len(filtered)
    filtered_final = []
    prev_title_norms = seen_context.get("title_norms", [])
    prev_desc_norms  = seen_context.get("desc_norms",  [])
    new_title_norms  = []
    new_desc_norms   = []
    _entity_canon = load_entity_canonical_map()

    for a in filtered:
        entity   = (a.get("entity") or "").strip()
        entity   = canonicalize_entity(entity, _entity_canon, exposure_data)
        keyword  = (a.get("keyword") or "").strip()
        event_type = (a.get("event_type") or "").strip()
        combo    = (entity, event_type) if entity and event_type else \
                   (entity, keyword) if entity and keyword else None
        kw_only  = ("", keyword) if keyword else None
        t_norm   = _norm(a.get("title", ""))
        d_norm   = _norm(a.get("desc",  ""))
        matched  = False
        reason   = ""

        # event_key 기반 seen 비교 (entity+event_type 조합, 가장 정밀)
        # 별칭 대칭화: event_key는 AI가 원본 entity로 생성하므로("중앙일보_워크아웃"),
        # 정규화된 entity로 재구성해 비교한다 — 저장 시에도 동일 방식으로 재구성해
        # 비교·저장 키를 완전히 일치시킴 (2026-07-13 패치: 67b1f02는 비교만
        # 정규화하고 저장은 원본이라 별칭이 먼저 저장되면 dedup이 뚫리던 문제)
        event_key  = (a.get("event_key") or "").strip()
        if entity and event_type:
            event_key = f"{entity}_{event_type}"  # entity는 위에서 정규화 완료
        ek_combo   = ("ek", event_key) if event_key else None
        if not matched and ek_combo and ek_combo in seen_combos:
            if not a.get("_force_urgent") and not is_next_stage(a.get("title",""), a.get("desc",""), entity):
                matched = True; reason = "동일 사건(event_key) 이미 발송"

        if combo and combo in seen_combos:
            if a.get("_force_urgent") or is_next_stage(a.get("title",""), a.get("desc",""), entity):
                pass
            else:
                matched = True; reason = "동일 사건(entity+kw) 이미 발송"

        if not matched and not entity and kw_only and kw_only in seen_combos:
            matched = True; reason = "동일 키워드 이미 발송"

        # ── 가격 연동 재발송 오버라이드 ──
        # 이미 발송한 사건이라도 해당 종목이 당일 큰 폭으로 추가 하락했다면
        # dedup을 무시하고 재발송한다.
        # 사유: 코오롱티슈진 임상실패(7/23 07시 발송) 이후 연속 하한가로 손실이
        # 확대되는 국면에서, 후속 기사가 전부 동일 조합(entity_기타리스크)으로
        # 판정돼 차단 → 정작 가장 알려야 할 시점에 봇이 침묵한 실사례.
        # 익스포저가 있는 종목만 조회(무관 종목 yfinance 호출 방지).
        # find_exposure() 사용 — 직접 dict 조회는 법인명 표기차이("중앙일보"↔
        # "중앙일보(주)")·영문 별칭(JTBC→제이티비씨)을 못 잡아 실제 익스포저가
        # 있는데도 재발송 기회를 놓친다.
        if matched and entity:
            _exp_rows = find_exposure(entity, exposure_data)
            if _exp_rows:
                _drop = get_entity_price_drop(entity, exposure_data)
                if _drop is not None and _drop <= PRICE_RESEND_THRESHOLD:
                    matched = False
                    reason = ""
                    # 재발송 사유는 바로 아래 로그로 남긴다. 플래그를 두었으나
                    # 읽는 곳이 없어 죽은 코드였음(2026-07-29 정리).
                    print(f"  [dedup 해제] {entity} 당일 {_drop}% 하락 → 재발송 허용")

        # 당일 동일 entity 1건 제한 — event_type 달라도 같은 사건으로 판단
        # 예외: _force_urgent(당사 직접 이슈), is_next_stage, 일반명사 entity
        _GENERIC = {"기업", "시장", "코스닥", "코스피", "증시", "채권", "주식", "부동산", "금융"}
        if (not matched and entity
                and entity in seen_entities_today
                and entity not in _GENERIC
                and not a.get("_force_urgent")
                and not is_next_stage(a.get("title",""), a.get("desc",""), entity)):
            matched = True; reason = f"당일 동일 entity({entity}) 이미 발송"

        if not matched and t_norm and not is_next_stage(a.get("title",""), a.get("desc",""), entity):
            for prev_t in prev_title_norms:
                if _sim(t_norm, prev_t) >= TITLE_SIM_THRESHOLD - 0.02:
                    matched = True; reason = "이전 실행 발송 기사와 제목 유사"
                    break

        if not matched and d_norm and len(d_norm) > 20 and not is_next_stage(a.get("title",""), a.get("desc",""), entity):
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
            # 비교 대상(filtered_final)의 entity도 정규화 — 별칭이 다른 동일 사건 누락 방지
            existing_grades = [GRADE_ORDER[x["grade"]] for x in filtered_final
                               if canonicalize_entity(x.get("entity", "") or "", _entity_canon, exposure_data) == entity
                               and x.get("event_type") == event_type]
            if existing_grades and GRADE_ORDER.get(a["grade"], 9) > min(existing_grades):
                print(f"  [{a['grade']}] '{a['title'][:30]}' — 동일 사건 상위등급 이미 발송, 스킵")
                continue

        filtered_final.append(a)
        new_title_norms.append(t_norm)
        new_desc_norms.append(d_norm)

    filtered = filtered_final
    if before_combo != len(filtered):
        print(f"  중복 사건 제거: {before_combo}건 → {len(filtered)}건")

    # ── 동일 메일 내 entity dedup — 같은 entity는 리스크 점수 최고값 1건만 유지 ──
    _entity_best: dict = {}  # entity(정규화) → 현재 최고 점수 article
    for a in filtered:
        # 별칭 정규화 키 사용 — 같은 메일 안에서 JTBC/중앙일보로 갈려 2장 나가는 것 방지
        ent = canonicalize_entity(a.get("entity", "") or "", _entity_canon, exposure_data)
        if not ent:
            continue
        score = a.get("_risk_score", 0) or 0
        if ent not in _entity_best or score > (_entity_best[ent].get("_risk_score", 0) or 0):
            _entity_best[ent] = a

    _best_ids = {id(a) for a in _entity_best.values()}
    _removed = [a for a in filtered if a.get("entity") and id(a) not in _best_ids]
    for a in _removed:
        print(f"  [entity dedup] 동일 entity 낮은점수 제거: [{a['grade']}] {a['title'][:45]}")

    # entity 없는 기사(시장 전체 이슈 등)는 그대로 유지
    filtered = [a for a in filtered if not a.get("entity") or id(a) in _best_ids]
    if _removed:
        print(f"  entity dedup: {len(_removed)}건 제거 → {len(filtered)}건")
    # ──────────────────────────────────────────────────────────────────────────

    # ── known_entities 기반 장기 이슈 등급 강등·차단 ──────────────────────────
    # D+0~2: 정상, D+3~6: 1단계 강등, D+7+: 완전 차단 (NEXT_STAGE 예외 유지)
    GRADE_DEMOTE = {"긴급": "주의", "주의": "참고", "참고": "참고"}
    # 일반명사 entity — known_entities·seen_entities_today 차단 제외
    GENERIC_ENTITIES = {"기업", "시장", "코스닥", "코스피", "증시", "채권", "주식", "부동산", "금융"}
    _known_removed = []
    for a in list(filtered):
        # 저장된 combos가 정규화 entity 기준이므로 조회 키도 정규화 (별칭 우회 방지)
        ent = canonicalize_entity(a.get("entity", "") or "", _entity_canon, exposure_data)
        if not ent or a.get("_force_urgent") or ent in GENERIC_ENTITIES:
            continue
        days = known_entities.get(ent, 0)
        title = a.get("title", "")
        desc  = a.get("desc", "")
        if days >= 7:
            if not is_next_stage(title, desc, ent):
                print(f"  [장기이슈 차단] D+{days} {ent}: {title[:40]}")
                _known_removed.append(a)
                filtered.remove(a)
        elif days >= 3:
            if not is_next_stage(title, desc, ent):
                old_grade = a.get("grade", "참고")
                new_grade = GRADE_DEMOTE.get(old_grade, "참고")
                if old_grade != new_grade:
                    a["grade"] = new_grade
                    print(f"  [장기이슈 강등] D+{days} {ent} {old_grade}→{new_grade}: {title[:35]}")

    # 강등 후 참고 GRADE_LIMITS 재체크 (최대 5건)
    ref_after_demote = [a for a in filtered if a.get("grade") == "참고"]
    if len(ref_after_demote) > GRADE_LIMITS["참고"]:
        ref_sorted = sorted(ref_after_demote, key=lambda x: x.get("_risk_score", 0), reverse=True)
        keep_ids = {id(a) for a in ref_sorted[:GRADE_LIMITS["참고"]]}
        excess = [a for a in ref_after_demote if id(a) not in keep_ids]
        for a in excess:
            print(f"  [참고 초과 제거] {a['title'][:40]}")
            filtered.remove(a)

    # 차단된 기사도 seen에 등록 (재탐지 방지)
    # 저장 키는 반드시 정규화 entity 기준 — dedup 비교 측과 대칭 유지
    for a in _known_removed:
        sent_urls.add(a.get("url", ""))
        _ent = canonicalize_entity((a.get("entity") or "").strip(), _entity_canon, exposure_data)
        _et  = (a.get("event_type") or "").strip()
        _ek  = (a.get("event_key") or "").strip()
        if _ent and _et:
            _ek = f"{_ent}_{_et}"  # 정규화 entity로 event_key 재구성
        _kw  = (a.get("keyword") or "").strip()
        if _ek:
            new_combos_this_run.add(("ek", _ek))
        if _et and _ent:
            new_combos_this_run.add((_ent, _et))
        elif _kw and _ent:
            new_combos_this_run.add((_ent, _kw))
    # ──────────────────────────────────────────────────────────────────────────

    print(f"필터링 후 {len(filtered)}건 선별")

    total_count = len(raw_articles) + len(hard_excluded_articles)

    if not filtered:
        now = datetime.now(timezone(timedelta(hours=9)))
        # 여신잔고 위험고객 여부 확인 — 있으면 전체 발송
        # [설계 참고] 이 분기(뉴스 0건)는 위험고객 보유 -3%↓ 종목이 1개만 있어도
        # 전체 발송한다. 뉴스가 있는 경우의 시장급락 안전장치(10개 이상, main 하단
        # _MARKET_CRASH_STOCK_THRESHOLD)와 기준이 다른 것은 의도된 설계:
        # 여기서는 메일 콘텐츠가 여신잔고 현황 그 자체라 1개라도 알릴 가치가 있고,
        # 뉴스가 있는 경우엔 저등급 뉴스+소수 종목 하락만으로 전사 발송을 막기 위함.
        _price_section = build_price_alert_section(exposure_data, "")
        # 뉴스 0건 시 전체발송 기준 (2026-07-25 조정)
        # 기존: 경보 종목이 1개라도 있으면 전체발송 → 위험고객 보유 종목이
        # 303개라 그중 1개만 -3% 하락해도 발동, 평상시에도 거의 매일
        # 전사 메일이 나가 임원 신뢰도를 떨어뜨릴 수 있었음.
        # 변경: 5종목 이상 OR 리스크잔고 합계 50억 이상.
        # 잔고 조건을 병행하는 이유 — 실측상 단일 종목 최대 95억,
        # 상위 3종목이 전체의 45%를 차지해 '적은 종목 수 + 큰 잔고' 상황을
        # 종목 수만으로는 놓치기 때문.
        # ★2026-07-29 룰 통일: 뉴스 0건 경로도 '뉴스 있을 때'와 동일 기준을 쓴다.
        #   기존엔 5종목 OR 50억(느슨)이라, 뉴스가 '없을수록' 더 쉽게 전사
        #   발송되는 모순이 있었다(5개 시나리오 중 4개 불일치 실측).
        #   하락장마다 '리스크에 해당하는 뉴스가 없습니다' 메일이 전사로
        #   나가 임원 피로를 유발했다(7/29 21시: 뉴스 0건인데 58종목 하락으로 발송).
        #   뉴스가 없으면 정보량이 오히려 적으므로 더 엄격해야 한다는 판단.
        _NR_STOCK_TH = MARKET_CRASH_STOCK_THRESHOLD
        _NR_RBAL_TH  = MARKET_CRASH_RBAL_THRESHOLD
        _nr_cnt  = getattr(build_price_alert_section, "last_alerted_count", 0)
        _nr_rbal = getattr(build_price_alert_section, "last_alerted_rbal", 0)
        _nr_full = bool(_price_section) and (_nr_cnt >= _NR_STOCK_TH
                                             and _nr_rbal >= _NR_RBAL_TH)
        if _price_section:
            print(f"  [뉴스 0건 발송판정] 경보 {_nr_cnt}종목 / 리스크잔고 {_nr_rbal:,.0f}억 "
                  f"/ 기준 {_NR_STOCK_TH}종목 AND {_NR_RBAL_TH:,.0f}억 → "
                  f"{'충족(전체발송)' if _nr_full else '미달(본인한정)'}")
        if _price_section:
            # 경보가 있으면 내용은 동일하게 만들고 '발송 범위'만 기준으로 가른다.
            # 기준 미달이어도 본인에게는 보내 정보가 사라지지 않게 한다.
            subject = f"❗ [리스크 탐지] {now_str_full} 기준 — 여신잔고 위험고객 탐지"
            _ref_date = next(iter(exposure_data.values()))[0].get("기준일", "") if exposure_data else ""
            _today_str = now.strftime("%m월 %d일")
            _ai_summary = "금일 리스크 뉴스 없음 — 여신잔고 위험고객 현황 확인 필요"
            _html = build_email_html([], total_count=total_count, ai_summary=_ai_summary,
                                     exposure_data=exposure_data, ref_date=_ref_date,
                                     competitor_notices=None, today_str=_today_str)
            print(f"AI 필터링 결과 없음 — 여신잔고 위험고객 {_nr_cnt}종목·"
                  f"{_nr_rbal:,.0f}억 → {'전체 발송' if _nr_full else '본인 한정'}")
            send_email(subject, _html,
                       self_only=(not _nr_full) or FORCE_SELF_ONLY)
        else:
            print("AI 필터링 결과 없음 — 결과 없음 메일 발송 (특정인만)")
            subject = f"❗ [리스크 탐지] {now_str_full} 기준 — 해당 뉴스 없음"
            send_email_no_result(subject, build_empty_html(now))
        save_seen_urls(seen_urls)
        save_filter_log(raw_articles, hard_excluded_articles, ai_filtered_articles, filtered)
        return

    print("  본문 크롤링 중... (전체 등급 — 2차 정밀검수용)")

    def search_alternative_url(title: str) -> str:
        """본문 크롤링 실패 시 네이버 뉴스 검색으로 동일 제목 기사 대체 URL 탐색"""
        try:
            # 제목 앞 30자로 검색 (특수문자 제거)
            query = re.sub(r'[\[\]「」『』【】〔〕\(\)…]', '', title)[:30].strip()
            res = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers={
                    "X-Naver-Client-Id": NAVER_CLIENT_ID,
                    "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
                },
                params={"query": query, "display": 5, "sort": "date"},
                timeout=8,
            )
            if res.status_code != 200:
                return ""
            items = res.json().get("items", [])
            for item in items:
                alt_url = item.get("link") or item.get("originallink", "")
                if alt_url and alt_url != "":
                    return alt_url
        except Exception:
            pass
        return ""

    def crawl_body(article):
        # 전체 등급 본문 크롤링 (기존 긴급·주의만 → 전체로 확장)
        body = fetch_article_body(article["url"])
        if body:
            article["body"] = body
            article["_body_failed"] = False
        else:
            # 1) 네이버 검색으로 대체 URL 시도
            alt_url = search_alternative_url(article.get("title", ""))
            if alt_url and alt_url != article["url"]:
                alt_body = fetch_article_body(alt_url)
                if alt_body:
                    article["body"] = alt_body
                    article["_body_failed"] = False
                    # 대체 URL은 로그로만 남긴다(이전엔 _alt_url 플래그를
                    # 설정만 하고 읽는 곳이 없어 죽은 코드였음 — 2026-07-29 정리)
                    print(f"  본문 대체 URL 성공: {article.get('title','')[:30]}")
                    return article
            # 2) 대체도 실패 → desc fallback, 참고 강등
            print(f"  본문 크롤링 실패(대체 URL도 없음) → 참고 강등: {article.get('title','')[:30]}")
            article["body"] = article.get("desc", "")
            article["_body_failed"] = True
            if article.get("grade") in ("긴급", "주의"):
                article["grade"] = "참고"
        return article

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(crawl_body, a): a for a in filtered}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  본문 크롤링 오류: {e}")

    # ── Claude 본문 기반 2차 검증 ────────────────────────────────────────────
    # 1차(Gemini) 통과 기사 중 본문이 있는 것만 재검증 — 오탐 최종 차단
    print("  [2차 검증] 본문 기반 Claude 리스크 재검증 중...")

    def claude_body_verify(article, model: str = None) -> bool:
        """본문을 읽고 진짜 리스크 기사인지 재판단. True = 유지, False = 제외

        model: 사용할 Claude 모델. 미지정 시 CLAUDE_MODEL(Sonnet).
               전체 발송이 예상되는 회차에는 상위 모델이 주입된다.
        """
        _model = model or CLAUDE_MODEL
        body = article.get("body", "") or ""
        title = article.get("title", "")

        # 본문 없거나 크롤링 실패 → 유지 (이미 참고 강등됨)
        if not body or article.get("_body_failed"):
            return True

        # 본문 앞 1500자 사용 — 600자였을 때 "거래정지 사유는 주식분할"처럼
        # 판정에 결정적인 정보가 뒷부분에 있어 놓치는 사례 발생(7/24 한울앤제주
        # 긴급 오탐). 정확도 우선으로 확대.
        body_preview = body[:1500]

        # ── 당사 익스포저 요약 생성 ──
        # 오판 분석(회귀세트 56건 실측)에서 오판 13건 중 6건(46%)이 "판정 자체는
        # 맞으나 그게 당사 고객 손실인지 몰라서" 발생. 예) "키움증권 MTS 장애"를
        # 투자자 손실로 보고 true 판정 — 당사 익스포저가 없다는 정보가 없었음.
        # AI가 "누가 손실을 보는가" 다음에 "그게 우리 고객인가"를 판단할 수 있도록
        # 실제 보유 현황을 입력으로 제공한다.
        _ent = (article.get("entity") or "").strip()
        _exp_lines = []

        def _f0(v):
            """'1,234' 같은 문자열·빈값을 안전하게 float으로. 실패 시 0.0"""
            try:
                return _num(v)
            except (ValueError, TypeError):
                return 0.0

        try:
            for _r in (find_exposure(_ent, exposure_data) if _ent else [])[:6]:
                _b = _f0(_r.get("뱅잔고")) + _f0(_r.get("영잔고"))
                _c = _f0(_r.get("뱅고객수")) + _f0(_r.get("영고객수"))
                if _b or _c:
                    _exp_lines.append(
                        f"  - {_r.get('종목명','')} ({_r.get('종목유형','')}): "
                        f"{_b:,.0f}억, {_c:,.0f}명")
        except Exception as _e:
            # 실패해도 판정은 계속되지만, 익스포저 없이 판정한 사실은 남겨야
            # 2차 검증 오판을 사후에 설명할 수 있다.
            print(f"  [2차검증] 익스포저 요약 실패({_ent}): {type(_e).__name__}")
        if _exp_lines:
            _exposure_txt = ("당사 보유 현황(뱅키스+영업점 합산):\n"
                             + "\n".join(_exp_lines))
        elif _ent:
            _exposure_txt = (f"당사 보유 현황: '{_ent}' 관련 잔고를 보유 종목 "
                             f"목록에서 찾지 못함 (미등록·표기차이 가능 — "
                             f"보유 없음이 확정된 것은 아님)")
        else:
            _exposure_txt = ("당사 보유 현황: 기사에서 대상 종목이 특정되지 않아 "
                             "조회 불가 (익스포저 없음을 뜻하지 않음)")

        prompt = f"""당신은 한국투자증권 개인고객그룹 리스크 담당자입니다.

【당신의 역할 — 2차 정밀 검수】
1차 필터(Gemini)는 제목·요약만 보고 넓게 걸러낸 결과입니다. 당신은 그 통과분
전건을 **본문까지 읽고** 최종 판정하는 마지막 관문입니다. 1차는 recall 우선
(애매하면 통과)이므로, 오탐을 걸러내는 책임은 전적으로 당신에게 있습니다.
제목만으로는 알 수 없고 본문에만 드러나는 사실(예: 거래정지의 실제 사유)을
반드시 확인하세요.

판단 기준:
- 리스크 O: 상장폐지·파산·부도·기업회생 확정, 당사 채권·PF 손실 가능, 반대매매 급증, MTS 장애, 금감원 제재
- 리스크 X: 연예·방송 인물 에피소드, 회사 상황을 배경으로만 언급한 인터뷰·인물 기사, 산업 트렌드 분석, 시황 브리핑, 이미 알려진 사건의 단순 경과 보도
- ★리스크 X (기술적 거래정지): 거래정지 기사는 **본문에서 정지 사유를 반드시 확인**할 것.
  주식분할·액면분할·주식병합·무상증자·인적/물적분할·전자등록 변경·말소·신주권
  변경상장·주권제출 등 절차상 사유면 손실과 무관 → 리스크 X
  예) "거래소, 한울앤제주 29일부터 주권매매거래정지"(사유: 주식분할에 따른
      전자등록 변경·말소) → 리스크 X
  단, 부도·횡령·배임·감사의견거절·자본잠식·불성실공시·상장폐지 실질심사 등
  실질 사유에 따른 거래정지는 리스크 O
- ★리스크 X (연예·인물 파생): 기업의 회생·파산이 배경일 뿐 실제 내용이 연예인·
  인물의 SNS·브이로그·발언 논란이면 금융 리스크 아님 → 리스크 X
  예) "이나연, 회생 신청한 JTBC '출근 브이로그' 뭇매" → 리스크 X
- ★리스크 X (호재성 상장폐지): 공개매수 프리미엄·잔여지분 현금매입·주식교환에
  의한 완전자회사 편입 등 주주가 보상을 받는 상장폐지 → 리스크 X
  예) "SK시그넷 상장폐지·매각…소액주주 20% 프리미엄가 공개매수" → 리스크 X
- 리스크 X (파생 기사): 이미 기업회생·법정관리가 진행 중인 기업의 영업·인사·콘텐츠·계약 영향 기사
  예) JTBC 회생 진행 중 → "출연료 미지급", "드라마 촬영 중단", "직원 급여 지연" → 리스크 X
  예) 홈플러스 회생 진행 중 → "납품 차질", "입점 업체 피해" → 리스크 X
  단, 파산선고 확정·회생계획 인가·추가 계열사 회생 신청은 리스크 O
- 리스크 X (투자경고·과열): 테마주 단기 급등에 따른 투자경고·투자위험종목 지정·거래정지는
  부실이 아니라 투기 과열 경계 조치 → 리스크 X
  예) "호남 반도체 테마주 무더기 경보", "급등에 투자위험종목 지정" → 리스크 X
  단, 주가 하락·실적 악화·감사의견거절에 따른 거래정지는 리스크 O
- ★리스크 O (당사 보유 해외종목 급락): 해외주식도 당사 고객이 보유하면 실제 평가손이
  발생하므로, '당사 보유 현황'에 해외주식 잔고가 있으면 -8% 이상 급락·실적 쇼크는 리스크 O
  예) "테슬라 하루 -8% 급락"(당사 해외주식 보유) → 리스크 O
- ★리스크 O (ETF 투자유의종목 지정): 거래소의 ETF 투자유의종목 적출·상장폐지 사유 발생은
  당사 보유 고객의 환금성·손실과 직결되므로 리스크 O (테마주 급등 경계와 구분할 것)
  예) "거래소, ACE SK하이닉스 단일종목레버리지 등 3종 투자유의종목 적출" → 리스크 O
- 리스크 X (M&A 자진 상장폐지): 사모펀드 등 최대주주가 잔여 지분을 프리미엄 가격에 공개매수해
  완전자회사화하는 상장폐지는 부실 아닌 호재성 M&A → 리스크 X
  예) "더존비즈온, 잔여 지분 현금 매입 마무리…상장폐지" — EQT 공개매수에 의한 자진 상폐, 소액주주 프리미엄 수령 → 리스크 X
  단, 감사의견거절·실적 악화·재무 부실로 인한 상장폐지는 리스크 O
- 리스크 X (칼럼·코너물): 제목이 "[코너명]" 형태이고 필자 개인 견해·정기 연재 형식이면
  (처음 보는 코너명도 동일 적용 — "OO노트", "OO워치", "줌인", "취재파일" 등)
  본문에 새로운 확정 손실 사건이 없는 한 리스크 X. "[단독]","[속보]","[공시]"는 예외

제목: {title}
본문(앞부분): {body_preview}

{_exposure_txt}

먼저 아래 4가지를 순서대로 판단한 뒤 결론을 내리세요.
① 핵심사건: 이 기사가 보도하는 사건 한 가지를 15자 이내로
② 손실주체: 이 사건으로 손실을 보는 쪽 — "주주" / "회사" / "채권자" / "없음"
   ★ 공개매수 프리미엄·완전자회사 편입·주식분할처럼 주주가 보상을 받거나
     아무 영향이 없으면 "없음"입니다. 이 경우 risk는 반드시 false입니다.
③ 당사연관: "직접" / "당사이슈" / "무관"
   ★★ 매우 중요 — 익스포저 정보는 보조 근거일 뿐입니다.
     '보유 없음'은 보유 종목 목록에서 이름을 찾지 못했다는 뜻일 뿐,
     고객이 실제로 보유하지 않는다는 의미가 아닙니다. 신규 상장사·종목명
     표기 차이·익명 표기(OO건설, XX리츠, H사 등)면 조회가 실패합니다.
     **'보유 없음'만을 이유로 risk=false 하지 마십시오.**
     사건 자체가 상장폐지·거래정지·부도·회생·감사의견거절·채무불이행·
     신용등급 강등·실적 쇼크·반대매매 확정 등 실질 리스크이면 익스포저
     조회 결과와 무관하게 risk=true 입니다.
   ★ "무관"으로 판정해도 되는 경우는 아래로 한정합니다:
     - 경쟁사의 전산장애·고객민원·서비스 품질 이슈 (당사 고객 영향 없음)
     - 시장 전체 통계·전망 기사로 어떤 종목도 지목하지 않고 확정 사건도
       없는 경우 (단, 반대매매 급증·강제청산 확정처럼 고객 손실이 실재하면
       "직접"입니다)
     - 명백히 국내 상장·채권과 무관한 대상(해외 비상장, 가상자산 등)
   ★ 경쟁사라도 대규모 손실·부도·회생·상장폐지처럼 주가 급락을 유발하는
     사건이면 당사 고객이 그 주식을 보유할 수 있으므로 "직접"입니다.
④ 확정여부: "확정" / "가능성" / "무관"

JSON만 출력:
{{"judgment": {{"핵심사건": "...", "손실주체": "...", "당사연관": "...", "확정여부": "..."}}, "risk": true}}
또는
{{"judgment": {{"핵심사건": "...", "손실주체": "...", "당사연관": "...", "확정여부": "..."}}, "risk": false, "reason": "한 줄 이유"}}"""

        try:
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": _model,
                    # judgment(핵심사건·손실주체·확정여부) 필드 추가로 응답이
                    # 길어짐. 80이면 JSON이 중간에 잘려 파싱 실패 → 안전하게
                    # 유지(return True)로 빠져 오탐이 그대로 통과하므로 상향.
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=25,
            )
            if res.status_code != 200:
                return True  # API 오류 → 안전하게 유지
            raw = (res.json().get("content", [{}])[0].get("text") or "").strip()
            data = json.loads(raw)
            is_risk = data.get("risk", True)
            # 판정 근거 기록 — 오탐 발생 시 어느 단계에서 틀렸는지 추적용.
            # save_filter_log()로 filter_log.json에 함께 저장된다.
            _judg = data.get("judgment") or {}
            if _judg:
                article["_judgment"] = _judg
            _jstr = ""
            if _judg:
                _jstr = (f" | 사건:{_judg.get('핵심사건','')} "
                         f"손실주체:{_judg.get('손실주체','')} "
                         f"확정:{_judg.get('확정여부','')}")
            if not is_risk:
                reason = data.get("reason", "")
                print(f"  [2차 제외] {title[:40]} — {reason}{_jstr}")
            elif _judg:
                print(f"  [2차 유지] {title[:34]}{_jstr}")
            return is_risk
        except Exception:
            return True  # 파싱 실패 → 안전하게 유지

    # ── 2차 검증 모델 선택 (예비 발송범위 판정) ────────────────────────
    # 전체 발송이 예상되면 상위 모델로 검증한다. 임원에게 나가는 회차는
    # 오탐 비용이 가장 크므로 마지막 관문만 승급하는 것.
    # 여기서의 판정은 '예비'이며, 2차 검증 결과를 반영해 뒤에서 최종 판정한다.
    # (그래서 Opus가 기사를 대량 제외해도 빈 메일이 전사로 나가지 않는다)
    # ref_date는 이 지점보다 뒤(메일 생성 직전)에서 정의되므로 여기서 계산한다.
    # ★2026-07-29 실환경 테스트에서 NameError로 파이프라인이 중단됐던 지점 —
    #   로컬 단위테스트는 decide_send_scope를 직접 호출해 main의 변수 순서를
    #   타지 않아 잡히지 않았다.
    _pre_ref_date = ""
    if exposure_data:
        try:
            _pre_ref_date = next(iter(exposure_data.values()))[0].get("기준일", "")
        except (StopIteration, IndexError, AttributeError):
            _pre_ref_date = ""
    _pre = decide_send_scope(filtered, exposure_data, _pre_ref_date)
    _verify_model = CLAUDE_MODEL
    if not _pre["self_only"]:
        _verify_model = CLAUDE_VERIFY_HIGH_MODEL
        _rsn = " + ".join(_pre["triggers"]) if _pre["triggers"] else f"점수 {_pre['max_score']:.1f}"
        print(f"  [2차 검증 모델 승급] 전체발송 예상({_rsn}) → {_verify_model}")
    else:
        print(f"  [2차 검증 모델] 본인한정 예상 → {_verify_model}")
    globals()["_LAST_VERIFY_MODEL"] = _verify_model

    # 병렬 검증
    _verify_results = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(claude_body_verify, a, _verify_model): a for a in filtered}
        for future in as_completed(futures):
            a = futures[future]
            try:
                _verify_results[id(a)] = future.result()
            except Exception:
                _verify_results[id(a)] = True

    before_verify = len(filtered)
    _removed_verify = [a for a in filtered if not _verify_results.get(id(a), True)]
    filtered = [a for a in filtered if _verify_results.get(id(a), True)]
    # ★2026-07-29: 2차 검증에서 '전량' 제거되면 뉴스 0건인데도 일반 경로로
    #   흘러가 '[리스크 탐지] …기준' 제목에 본문은 '해당 뉴스가 없습니다'인
    #   메일이 전사로 나갔다(7/29 21시 실사례 — 요약에는 필터 전 종목명이
    #   남아 내용과 어긋나기까지 했다).
    #   뉴스 0건 분기는 L5010에서 이미 지나쳤으므로, 여기서 다시 판정한다.
    if before_verify > 0 and not filtered:
        print(f"  [2차 검증] {before_verify}건 전량 제거 — 뉴스 0건 상태로 발송 판정 진행")

    # 2차 검증 제거 기사 → seen_news에 등록 (다음 실행 재탐지 방지)
    for a in _removed_verify:
        sent_urls.add(a.get("url", ""))
        _ent = (a.get("entity") or "").strip()
        _et  = (a.get("event_type") or "").strip()
        _ek  = (a.get("event_key") or "").strip()
        _kw  = (a.get("keyword") or "").strip()
        if _ek:
            new_combos_this_run.add(("ek", _ek))
        if _et and _ent:
            new_combos_this_run.add((_ent, _et))
        elif _kw and _ent:
            new_combos_this_run.add((_ent, _kw))

    if _removed_verify:
        print(f"  [2차 검증] {len(_removed_verify)}건 제외 → {len(filtered)}건 유지")
    # ──────────────────────────────────────────────────────────────────────────

    # ── 2차 검증 이후 seen_entities_today 재체크 ──────────────────────────────
    # 2차 검증 통과 후에도 당일 동일 entity 1건 제한 적용
    # (1차 dedup에서 놓친 케이스 — 다른 실행에서 이미 발송된 entity)
    _GENERIC_E = {"기업", "시장", "코스닥", "코스피", "증시", "채권", "주식", "부동산", "금융"}
    _recheck_removed = []
    for a in list(filtered):
        ent = a.get("entity", "") or ""
        if not ent or a.get("_force_urgent") or ent in _GENERIC_E:
            continue
        if (ent in seen_entities_today
                and not is_next_stage(a.get("title",""), a.get("desc",""), ent)):
            print(f"  [당일 재체크] 동일 entity 재차단: {ent} — {a.get('title','')[:40]}")
            _recheck_removed.append(a)
            filtered.remove(a)

    # 재체크 차단 기사도 seen_news 등록
    for a in _recheck_removed:
        sent_urls.add(a.get("url", ""))
        _ent = (a.get("entity") or "").strip()
        _et  = (a.get("event_type") or "").strip()
        _ek  = (a.get("event_key") or "").strip()
        _kw  = (a.get("keyword") or "").strip()
        if _ek:
            new_combos_this_run.add(("ek", _ek))
        if _et and _ent:
            new_combos_this_run.add((_ent, _et))
        elif _kw and _ent:
            new_combos_this_run.add((_ent, _kw))
    # ──────────────────────────────────────────────────────────────────────────

    print("  대응방안·고객안내 생성 중... (긴급·주의)")

    def generate_action_and_notice(article):
        grade = article.get("grade")
        if grade not in ("긴급", "주의"):
            return
        _body_failed = article.get("_body_failed", False)
        if _body_failed:
            print(f"  본문 크롤링 실패 — 제목·요약 기반으로 action 생성: {article.get('title','')[:30]}")
        body_text = article.get("body", "") or article.get("desc", "")
        entity    = article.get("entity", "")
        keyword   = article.get("keyword", "")
        # GROUP_ENTITIES_MAP 계열사 포함한 전체 entities로 익스포저 산출
        _act_entities = article.get("entities") or ([entity] if entity else [])
        _act_extra = []
        if allow_group_expansion(article):   # 카드 표시 범위와 동일하게 맞춘다
            for _e in list(_act_entities):
                for _x in GROUP_ENTITIES_MAP.get(_e, []):
                    if _x not in _act_entities and _x not in _act_extra:
                        _act_extra.append(_x)
        _act_entities_full = list(_act_entities) + _act_extra
        exp_rows = []
        _seen_exp = set()
        for _ae in _act_entities_full:
            for r in find_exposure(_ae, exposure_data):
                _rk = (r.get("종목명",""), r.get("종목코드",""))
                if _rk not in _seen_exp:
                    _seen_exp.add(_rk)
                    exp_rows.append(r)
        # 해외주식 여부 — 익스포저 rows의 시장 컬럼 또는 keyword 패턴으로 판단
        is_overseas = any(r.get("시장","국내") == "해외" or r.get("종목유형","") in ("해외주식","해외대출") for r in exp_rows)
        def _fmt_exp(r):
            """익스포저 1행 → AI 전달 문자열. 뱅키스·영업점을 각각 표기한다.

            (2026-08-13) 기존엔 합산값('잔고(억)')만 넘겨서 대응방안에도
            "여신 보유 고객(2억원/15명)"처럼 합산으로 나왔다. 익스포저 카드는
            채널 분리 표시(뱅 1억/10명 · 영 1억/5명)라 임원이 대조하기
            번거로웠다. 메일 전반의 '합산값 미표기' 원칙과도 어긋난다.
            → 채널별로 전달해 대응방안에서도 각자 표기되게 한다.
            구 12컬럼 스키마(채널 컬럼 없음)는 기존처럼 합산값으로 폴백.

            (2026-08-14) 종목명을 앞에 붙인다. 그룹사 기사처럼 여러 종목이
            함께 잡히면 유형만 나열돼 AI가 어느 종목의 잔고인지 알 수 없었다.
            실사례(8/13 21시 금감원-삼성 기사): 삼성생명·삼성화재·삼성증권이
            함께 전달됐는데 실제로는 삼성'증권' 채권인 값을 AI가 "삼성생명 채권
            보유 고객"으로 오귀속. 종목 오귀속은 임원이 잘못된 대상에 조치를
            지시하게 만드는 오류라 수치 오류보다 위험하다.
            """
            def _n(key):
                try:
                    return _num(r.get(key))
                except (ValueError, TypeError):
                    return 0.0
            유형 = r.get('종목유형', '')
            _nm = r.get('종목명', '')
            _head = f"{_nm} {유형}".strip()
            has_ch = any(k in r for k in ('뱅잔고', '영잔고'))
            if not has_ch:
                return f"{_head} {_n('잔고(억)'):,.0f}억원/{int(_n('고객수')):,}명"
            parts = []
            for _label, _bal, _cus in (("뱅키스", '뱅잔고', '뱅고객수'),
                                       ("영업점", '영잔고', '영고객수')):
                b, c = _n(_bal), int(_n(_cus))
                if b <= 0 and c <= 0:
                    continue
                parts.append(f"{_label} {b:,.0f}억원/{c:,}명")
            if not parts:
                return f"{_head} 잔고 없음"
            return f"{_head} {' · '.join(parts)}"

        # 동일 (종목명·종목코드·종목유형) 행이 원천 데이터에 분리 저장된 경우가
        # 있어(8/12 삼성증권 채권: 99/323/0/0 + 4/31/92/96) 카드는 합산 표시하는데
        # AI에는 분리값이 전달돼 대응방안 수치가 카드와 어긋났다. 카드와 동일하게
        # 병합해 전달한다. sanitize용 exp_rows는 원본 그대로 둔다(부분합 허용 유지).
        def _merge_exp_rows(rows):
            _agg, _order = {}, []
            _SUM_KEYS = ('뱅잔고', '뱅고객수', '영잔고', '영고객수', '잔고(억)', '고객수')
            for _r in rows:
                _k = (_r.get('종목명', ''), _r.get('종목코드', ''), _r.get('종목유형', ''))
                if _k not in _agg:
                    _agg[_k] = dict(_r)
                    _order.append(_k)
                    continue
                _base = _agg[_k]
                for _sk in _SUM_KEYS:
                    if _sk in _base or _sk in _r:
                        try:
                            _base[_sk] = _num(_base.get(_sk)) + _num(_r.get(_sk))
                        except (ValueError, TypeError):
                            pass
            return [_agg[_k] for _k in _order]

        exp_str = ", ".join([_fmt_exp(r) for r in _merge_exp_rows(exp_rows)]) if exp_rows else ""
        # 여신 잔고 유무를 명시적으로 알린다 (2026-08-04).
        # exp_str은 보유 유형만 나열해서, 여신이 없으면 그 유형이 목록에서 빠질
        # 뿐 '없다'는 사실이 드러나지 않는다. AI가 부재를 추론해야 하다 보니
        # 신용융자·담보비율·담보계좌 같은 조치가 반복해서 붙었다
        # (8/2 다원시스, 8/3 JR리츠, 8/4 본느 — 3회 연속).
        # action_prompt.txt의 금지 규칙과 짝을 이루도록 신호를 명시한다.
        _yeosin_bal = 0.0
        for _r in (exp_rows or []):
            if _r.get("종목유형", "") in ("여신", "해외대출"):
                try:
                    _yeosin_bal += _num(_r.get("잔고(억)"))
                except (ValueError, TypeError):
                    pass
        if _yeosin_bal <= 0:
            exp_str = ((exp_str + " ") if exp_str else "") + (
                "※ 여신(신용융자·대출) 잔고 없음 — 신용융자·미수·담보비율·"
                "담보계좌·신용계좌·반대매매·마진콜 등 신용거래 관련 조치를 "
                "일절 언급하지 말 것")
        _action_prompt_raw = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "action_prompt.txt"), encoding="utf-8").read()
        _act_static, _act_dynamic_tpl = _action_prompt_raw.split("<<<DYNAMIC_SPLIT>>>", 1)
        _act_dynamic = (
            _act_dynamic_tpl
            .replace("__KW__", keyword)
            .replace("__ENTITY__", entity)
            .replace("__GRADE__", article.get("grade",""))
            .replace("__TITLE__", article.get("title",""))
            .replace("__BODY__", body_text[:400])
            .replace("__EXP__", exp_str)
            .replace("__OVERSEAS__", "해외주식 (신용융자 불가, 담보대출만 가능)" if is_overseas else "국내주식")
            .replace("__BODY_FAIL__", " (※ 본문 크롤링 실패 — 제목·요약 기반만 사용, 추측 금지)" if article.get("_body_failed") else "")
        )
        try:
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "prompt-caching-2024-07-31",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_ACTION_MODEL,
                    "max_tokens": 800,
                    "temperature": 0.0,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": _act_static,
                         "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": _act_dynamic},
                    ]}],
                },
                timeout=20,
            )
            if res.status_code == 429:
                print(f"  대응방안 Rate limit 429 — 스킵: {article.get('title','')[:20]}")
                return
            res.raise_for_status()
            payload = res.json()
            content = payload.get("content", [])
            raw = (content[0].get("text") or "").strip() if content else ""
            if not raw:
                return
            raw = raw.replace("```json", "").replace("```", "").strip()
            # JSON 객체 범위만 추출 (Extra data 방어)
            _s = raw.find("{")
            _e = raw.rfind("}") + 1
            if _s != -1 and _e > _s:
                raw = raw[_s:_e]
            # ── 응답 잘림 계측 (2026-08-07 신설) ──
            # 8/6 14시 CMG제약 건에서 대응방안이 "…관리종목 지정 확정 시 상"으로
            # 끊긴 채 발송됐다. 이 경로에는 stop_reason 검사가 없어 잘려도 감지가
            # 안 되고, json_repair가 잘린 JSON을 '복구'해 잘린 텍스트가 그대로
            # 통과한다. 빈도를 모르면 max_tokens 상향이 해법인지 판단할 수 없어
            # 우선 계측만 넣는다.
            _stop = payload.get("stop_reason", "")
            _title_short = article.get('title', '')[:30]
            if _stop == "max_tokens":
                print(f"  [대응방안 잘림:max_tokens] {_title_short} — "
                      f"응답 {len(raw)}자, 상향 검토 필요")
            try:
                result = json.loads(raw)
            except Exception:
                try:
                    from json_repair import repair_json
                    result = json.loads(repair_json(raw))
                    print(f"  [대응방안 JSON 복구] {_title_short} — "
                          f"원본 파싱 실패(잘림 가능), stop_reason={_stop or 'n/a'}")
                except Exception:
                    result = {}
            if result.get("action"):
                action_text = result["action"]
                # 종결어미 없이 끝나면 잘림 의심 — 정상 문장은 대부분 명사형
                # 조치어(산출·점검·보고 등)나 마침표로 끝난다.
                if action_text and not _ACTION_VERB_RE.search(action_text.strip().rstrip('.')):
                    print(f"  [대응방안 미완결 의심] {_title_short} — "
                          f"말미: ...{action_text.strip()[-24:]!r} (stop={_stop or 'n/a'})")
                # ── 할루시네이션 수치 검증 (원천 차단 계층) ──
                # AI가 생성한 대응방안에 실제 익스포저로 설명 안 되는 창작 수치가
                # 있으면, 오염 수치를 제거하고 코드가 계산한 정확한 값으로 대체한다.
                _src = f"{article.get('title','')} {body_text}"
                _tainted, _bad = sanitize_action_numbers(action_text, exp_rows, _src)
                if _tainted:
                    print(f"  [수치 할루시네이션 차단] {article.get('title','')[:30]} — 창작수치 {_bad}")
                    # 오염으로 지목된 값만 제거 — 기사 사건 규모 등 정상 수치는 보존.
                    # 정확한 익스포저는 카드 하단 섹션에 이미 표시되므로 대응방안에는
                    # 부기하지 않는다 — 중복 노출로 인한 피로도 방지.
                    action_text = _strip_tainted_numbers(action_text, _bad)
                # 익스포저에 없는 유형의 조치(OB 인계·고객 안내) 제거 — 프롬프트
                # 규칙을 AI가 위반하는 경우가 있어 코드에서 결정론적으로 강제
                action_text, _rm = strip_unsupported_action_clauses(action_text, exp_rows)
                if _rm:
                    print(f"  [미보유 유형 조치 제거] {article.get('title','')[:30]} — {_rm}")
                action_text, _dup = dedup_action_phrases(action_text)
                if _dup:
                    print(f"  [중복 조치 정리] {article.get('title','')[:30]} — {_dup}")
                action_text, _ecp = strip_exposure_figures(action_text)
                if _ecp:
                    print(f"  [익스포저 수치 제거] {article.get('title','')[:30]}")
                action_text, _ent = prepend_entity_to_action(
                    action_text, (article.get("entity") or "").strip())
                if _ent:
                    print(f"  [대응방안 종목명 보강] {article.get('entity','')}")
                if _body_failed:
                    action_text += " *(본문 크롤링 실패, 제목 기반 생성)"
                article["action"] = action_text
            if result.get("customer_notice") and (grade == "긴급" or article.get("_notice_exempt")):
                notice_text = result["customer_notice"]
                # ── 고객 안내 문구 검증 (플레이스홀더·연도 환각·창작 수치) ──
                notice_text, _nfix = sanitize_customer_notice(
                    notice_text, exp_rows, f"{article.get('title','')} {body_text}")
                if _nfix:
                    print(f"  [고객문구 정제] {article.get('title','')[:30]} — {_nfix}")
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

    subject = f"❗ [리스크 탐지] {now_str_full} 기준"

    # 발송 범위를 요약보다 먼저 확정한다 (2026-08-19).
    # 기존엔 요약을 filtered(선별 전체)로 만들고 그 뒤에 참고 등급을 걷어냈다.
    # 그래서 메일에 실리지 않은 기사가 요약문에만 등장했다.
    #   실사례(8/19 21시): 선별 1건(듀오백)인데 요약은 "듀오백 거래 정지·
    #   테라사이언스·성원에너텍 회생 관련 리스크 관측" — 뒤 두 종목은 메일
    #   어디에도 없다. 8/14 07시 '급락 관측' 부정합과 같은 계열의 문제로,
    #   메일 안에서 근거를 찾을 수 없는 요약문은 임원 신뢰도를 깎는다.
    # → 실제 발송될 기사만 요약 입력으로 준다.
    _scope = decide_send_scope(filtered, exposure_data, ref_date)
    _self_only = _scope["self_only"]
    _mail_articles = filter_articles_for_scope(filtered, exposure_data, _self_only)
    _summary_src = _mail_articles or filtered

    urgent_cnt = len([a for a in _summary_src if a["grade"]=="긴급"])
    caution_cnt = len([a for a in _summary_src if a["grade"]=="주의"])
    ref_cnt = len([a for a in _summary_src if a["grade"]=="참고"])
    # AI 요약 컨텍스트 — 탐지 기사 중심 (여신잔고는 보조 참고용, 요약에 직접 언급 금지)
    filtered_titles = f"[등급 분포] 긴급 {urgent_cnt}건 / 주의 {caution_cnt}건 / 참고 {ref_cnt}건\n\n" + "\n".join([f"- [{a['grade']}] {a['title']}" for a in _summary_src])
    filtered_titles += ("\n\n[중요] 위 목록은 메일에 실제로 실리는 기사 전부다. "
                        "목록에 없는 기업·사건을 요약문에 넣지 말 것.")
    # 급락 섹션 렌더 여부를 요약 AI에 알린다 (2026-08-14).
    # 07시 회차는 장 시작 전이라 등락률이 없어 '여신잔고 리스크 현황' 표가
    # 렌더되지 않는데, 요약문에는 "관리종목 지정 종목 급락 관측"처럼 급락이
    # 언급돼 본문에 없는 내용을 가리켰다(8/14 07시). 메일 안에서 근거를 찾을
    # 수 없는 문장은 임원 신뢰도를 깎는다.
    # ※ decide_send_scope()가 앞서 호출되며 집계를 채우므로 여기서 읽어도 안전하다.
    _alert_cnt = getattr(build_price_alert_section, "last_alerted_count", 0)
    if not _alert_cnt:
        filtered_titles += ("\n\n[본문 구성] 급락 종목 표 없음(장전 회차 등) — "
                            "'급락'·'하락'·'폭락' 등 주가 하락 표현을 쓰지 말 것")

    # 경쟁사 공지 요약 추가
    if competitor_notices:
        competitor_summary = "\n".join([f"- [경쟁사] {n['company']}: {n['title']}" for n in competitor_notices[:3]])
        filtered_titles += f"\n\n[경쟁사 신용·대출 특이사항]\n{competitor_summary}"

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
                "max_tokens": 100,
                "messages": [{"role": "user", "content": f"아래 오늘의 리스크 탐지 기사를 보고, 핵심 리스크 흐름을 40자 이내 한 문장으로만 작성하세요.\n문장 외 다른 내용 일절 금지. 탐지된 기사 내용만 반영 (여신잔고·위험고객 수치는 직접 언급 금지).\n[톤 규칙] 등급 분포에 맞게 과장 없이 서술하세요. 긴급 0건이면 '급증'·'심화'·'비상'·'초비상' 같은 위기 표현을 쓰지 말고 '관측'·'주시'·'가능성' 등 사실 위주로. 참고 등급 기사(전망·분석·회고성)는 확정 사건처럼 단정하지 말 것.\n[표기 규칙] 항목 구분에는 '·'를 쓰되, 고유명사 안의 가운뎃점은 생략하지 말 것 ('코스피·코스닥'을 '코스피코스닥'으로 붙여 쓰지 말 것). 예(긴급 존재): '알테오젠 상폐·홈플러스 회생 부각'  예(참고 위주): '중앙그룹 회생 관련 후속 보도, 일부 종목 상폐 심사 진행 관측'\n\n{filtered_titles}"}],
            },
            timeout=15,
        )
        _sum_payload = sum_res.json()
        _sum_content = _sum_payload.get("content", [])
        _sum_text = next((b.get("text","") for b in _sum_content if b.get("type")=="text"), "")
        ai_summary = _sum_text.strip()
    except Exception:
        ai_summary = ""

    # ── 발송 직전 리스크점수 최종 확정 ──
    # 이후 단계(강등·검증)에서 등급이 바뀐 카드의 점수 괴리를 방지하기 위해
    # build_email_html(카드 표시) 이전에 재산출 → 카드 점수 = 발송 판단 점수 일치.
    for a in filtered:
        a["_risk_score"] = calc_risk_score(a, exposure_data)

    # ── 전체 발송 여부 결정 ──
    # 원칙: 긴급·주의 카드 중 최고 리스크점수가 임계값 이상이면 전체 발송.
    # 참고 등급은 '직접 손실 없는 동향'이므로 점수가 높아도(경쟁사 익스포저 등)
    # 전체 발송 트리거가 되지 않는다. 단, 긴급/고신뢰 주의/시장급락은 점수와
    # 무관하게 전체 발송(FN 방지).
    # ★판정 로직 전체는 decide_send_scope()에 있다 — main에 인라인으로 두면
    #   테스트가 그 로직을 복제하게 되고, 실제 코드가 깨져도 잡지 못한다.
    #   (2026-07-28 변이 테스트에서 확인)
    # 발송 범위 판정 — 로직은 decide_send_scope()에 있다.
    # (테스트가 재현본이 아니라 실제 함수를 호출하도록 분리했음)
    # ※ _scope/_self_only/_mail_articles는 요약 생성 전에 이미 확정했다
    #   (요약이 발송 제외 기사를 언급하던 문제 — 2026-08-19). 여기서는
    #   재계산하지 않고 그 값을 그대로 쓴다. 두 번 부르면 판정이 갈릴 수 있다.
    _max_score = _scope["max_score"]
    _trg = " + ".join(_scope["triggers"]) if _scope["triggers"] else "없음"

    if _scope["alerted_count"]:
        print(f"  [시장급락 판정] -3%↓ {_scope['alerted_count']}종목 / 리스크잔고 "
              f"{_scope['alerted_rbal']:,.0f}억 / 기준 {MARKET_CRASH_STOCK_THRESHOLD}종목 AND "
              f"{MARKET_CRASH_RBAL_THRESHOLD:,.0f}억 → "
              f"{'충족' if _scope['market_crash'] else '미달'}")
    print(f"  [발송판정] 최고점수 {_max_score:.2f} / 임계 {SELF_ONLY_MAX_SCORE:.2f} / "
          f"강제발송 조건: {_trg}")
    if _self_only:
        print(f"  [본인 한정 발송] 점수 {_max_score:.2f} < {SELF_ONLY_MAX_SCORE:.2f} "
              f"이고 강제발송 조건 없음 — 전체 발송 보류")
    elif _max_score < SELF_ONLY_MAX_SCORE:
        print(f"  [전체 발송] 점수 {_max_score:.2f} < {SELF_ONLY_MAX_SCORE:.2f}이나 "
              f"{_trg} → 전체 발송")
    else:
        print(f"  [전체 발송] 최고 리스크점수 {_max_score:.2f} ≥ {SELF_ONLY_MAX_SCORE:.2f}")

    # 전체 발송 시 참고 등급 축소 — 로직은 filter_articles_for_scope()에 있다.
    # (요약 생성 전에 이미 산출했다 — 위 주석 참고)
    if len(_mail_articles) != len(filtered):
        print(f"  [전체발송 참고 축소] 참고 {len(filtered)-len(_mail_articles)}건 제외 "
              f"(익스포저 {REF_FULLSEND_MIN_EXPOSURE:,.0f}억 미만) — 본인 메일에는 포함")

    # 뉴스가 0건이면 제목·요약을 상황에 맞게 바꾼다 — '리스크 탐지'라는
    # 제목에 본문이 비어 있으면 수신자가 발송 의도를 오해한다.
    if not _mail_articles:
        subject = f"❗ [리스크 탐지] {now_str_full} 기준 — 여신잔고 위험고객 현황"
        ai_summary = "금일 리스크 뉴스 없음 — 여신잔고 위험고객 하락 현황 확인 필요"

    # 최종 발송 등급 기록 (2026-08-12 추적) — 강등 대상이 참고로 남았는지,
    # 이후 단계에서 되돌려졌는지 run_stats에서 대조한다.
    _RUN_STATS["final_grades"] = [
        f"{a.get('entity','')}|{a.get('grade','')}|{a.get('title','')[:24]}"
        for a in _mail_articles
    ]

    html = build_email_html(_mail_articles, total_count=total_count,
                            ai_summary=ai_summary, exposure_data=exposure_data,
                            ref_date=ref_date, competitor_notices=competitor_notices,
                            today_str=today_str)
    if FORCE_SELF_ONLY and not _self_only:
        print("  [FORCE_SELF_ONLY] 전체발송 판정이나 테스트 모드 — 본인 한정으로만 발송")
        _self_only = True
    send_email(subject, html, self_only=_self_only)
    save_run_stats(total_count, len(_mail_articles),
                   globals().get("_LAST_VERIFY_MODEL") or CLAUDE_MODEL, _self_only)

    for a in filtered:
        sent_urls.add(a.get("url", ""))
        # 저장 키는 정규화 entity 기준 — dedup 비교(canonicalize 적용)와 대칭.
        # 원본 a['entity']는 카드 표시·익스포저 매칭용으로 이미 사용 완료라 무영향.
        entity     = canonicalize_entity((a.get("entity") or "").strip(), _entity_canon, exposure_data)
        keyword    = (a.get("keyword") or "").strip()
        event_type = (a.get("event_type") or "").strip()
        event_key  = (a.get("event_key") or "").strip()
        if entity and event_type:
            event_key = f"{entity}_{event_type}"  # 정규화 entity로 재구성
        # stage 기반 dedup — 발송 기사의 매칭 stage 키워드 기록 (차단 기사엔 미기록)
        if entity:
            for _skw in _stage_hits(a.get("title", ""), a.get("desc", "")):
                new_stages_this_run.add((entity, _skw))
        # event_key 우선 저장 → event_type → keyword 순 fallback
        if event_key:
            new_combos_this_run.add(("ek", event_key))
        if event_type and entity:
            new_combos_this_run.add((entity, event_type))
        elif keyword and entity:
            new_combos_this_run.add((entity, keyword))

    # entity dedup으로 제거된 기사도 URL·combo 등록 — 다음 실행 재탐지 방지
    for a in _removed:
        sent_urls.add(a.get("url", ""))
        _ent = canonicalize_entity((a.get("entity") or "").strip(), _entity_canon, exposure_data)
        _kw  = (a.get("keyword") or "").strip()
        _et  = (a.get("event_type") or "").strip()
        _ek  = (a.get("event_key") or "").strip()
        if _ent and _et:
            _ek = f"{_ent}_{_et}"  # 정규화 entity로 event_key 재구성
        if _ek:
            new_combos_this_run.add(("ek", _ek))
        if _et and _ent:
            new_combos_this_run.add((_ent, _et))
        elif _kw and _ent:
            new_combos_this_run.add((_ent, _kw))
    # ── 본인 한정 발송(_self_only)은 사건 단위 dedup에서 제외 ──
    # 전체 수신자에게는 안 나간 기사인데 combo/stage/context가 등록되면,
    # 이후 같은 사건의 후속 기사가 "이미 발송함"으로 차단돼 수신자들은
    # 그 사건을 영영 못 보게 된다(봇이 미탐한 것처럼 보이는 원인).
    # URL만 등록해 동일 기사 재처리는 막고, 사건 단위 키는 남기지 않는다.
    if _self_only:
        print(f"  [dedup 제외] 본인 한정 발송 — 사건 단위 키 미등록 "
              f"(combo {len(new_combos_this_run)}건, stage {len(new_stages_this_run)}건 스킵)")
        new_combos_this_run = set()
        new_stages_this_run = set()
        new_title_norms = []
        new_desc_norms  = []

    save_seen_urls(sent_urls, new_combos_this_run,
                   title_norms=new_title_norms, desc_norms=new_desc_norms,
                   stages=new_stages_this_run)
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
