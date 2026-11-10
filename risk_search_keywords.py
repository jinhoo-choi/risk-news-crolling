"""Bounded news searches; query selection never decides relevance or delivery."""
import math


COMMON_KEYWORDS = [
    "기업 부실", "신용등급 하향", "기업 유동성 위기", "채무불이행",
    "기업회생", "상장폐지", "파산", "워크아웃", "부도", "거래정지",
    "반대매매", "신용등급 강등", "PF 부실",
    "회사채 미매각", "CP 미매각", "PF 미매각",
    "발행어음 부실", "발행어음 미상환", "발행어음 지급지연",
    "감사의견", "관리종목", "횡령 배임",
    "한국투자증권 오류", "한국투자증권 장애", "한국투자증권 접속불가",
    "한국투자증권 지연", "한국투자증권 미지급",
    "마진콜", "담보부족", "주식 추가담보", "신용융자 추가담보",
    "담보비율 하회", "깡통계좌",
    "한국투자증권 불통", "한국투자증권 접수", "뱅키스 장애", "한투證 반대매매",
    "반대매매 유예", "반대매매 신청", "신용융자 중단", "담보유지비율",
    "증권사 해킹", "증권사 정보유출",
    "금감원 제재", "금감원 과태료", "금감원 증권사",
    "만기 미상환", "차환 실패", "서킷브레이커", "매도 사이드카",
]

OVERSEAS_KEYWORDS = [
    "반도체주 급락", "실적 쇼크", "가이던스 하향", "나스닥 급락",
    "미국주식 상장폐지", "미국 기업 파산", "파산보호 신청", "해외주식 급락",
]


def portfolio_keywords(exposure_data, ticker_names, top_n=10):
    """Use only held overseas positions and local names; at most 30 extra queries.

    Aggregate existing channel/asset rows before ranking. Unknown issuer names
    keep their exact ticker: building a search must not call a naming API.
    """
    limit = min(10, max(0, int(top_n)))
    balances = {}
    for key, rows in (exposure_data or {}).items():
        name = str(ticker_names.get(key, key)).strip()
        if not name:
            continue
        for row in rows:
            if row.get('종목유형') not in ('해외주식', '해외대출', '해외담보'):
                continue
            try:
                balance = float(str(row.get('잔고(억)', 0)).replace(',', ''))
            except (TypeError, ValueError):
                continue
            if math.isfinite(balance) and balance > 0:
                balances[name] = balances.get(name, 0) + balance
    selected = sorted(balances, key=lambda name: (-balances[name], name))[:limit]
    queries = []
    for name in selected:
        fund = any(word in name.upper() for word in ('ETF', 'ETN', '레버리지', '인버스'))
        terms = ('급락',) if fund else ('급락', '실적 쇼크', '가이던스 하향')
        queries.extend(name + ' ' + term for term in terms)
    return list(dict.fromkeys(queries))
