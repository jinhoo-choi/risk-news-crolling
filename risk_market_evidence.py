"""Shared source-only market attribution, not severity or permission to send."""
import re

# Only source words that connect an index subject to its own numeric movement.
# Never consume arbitrary company names between the index and decline predicate.
_MOVEMENT_VALUES = (
    r'(?>(?:포인트|퍼센트|p|약|이상|가량|정도|장중|개장\s*직후|'
    r'오전|오후|현재|일|시|분|대|넘게|[\d\s.,%+\-()])*)'
)
_INDEX_DECLINE = re.compile(
    r'(?:코스피|코스닥)\s*(?:지수)?(?!\s*(?:보다|대비))(?:는|은|가|이|도)?\s*'
    + _MOVEMENT_VALUES +
    r'(?:(?:전\s*거래일|전일|전날|직전\s*거래일)(?:\s*종가)?'
    r'(?:\s*\([\d.,%]+\))?\s*(?:대비하여|대비해|대비|보다)?\s*)?'
    + _MOVEMENT_VALUES +
    r'(?:급락|폭락|하락|내렸|내린|내려|붕괴)'
)
_NEGATED = re.compile(r'않|아니|없|할\s*수|가능|전망|예상|우려')


def has_credit_policy_change(segments):
    """Source-backed adopted credit/collateral rules, never generic easing.

    This is a review/routing guard. Relevance, dates and applicability still
    require source review; it does not establish a customer's liquidation stay.
    """
    for segment in segments:
        text = re.sub(r'\s+', ' ', segment)
        if not re.search(r'정부|금융당국|금융위원회|금융위|금융감독원|금감원', text):
            continue
        if not re.search(r'\d+\s*(?:개월|달|일|주)\s*(?:간|동안)|\d+일\s*부터|시행일|적용일', text):
            continue
        for clause in re.split(r'(?<!\d)[.!?。;](?!\d)', text):
            if not re.search(r'정부|금융당국|금융위원회|금융위|금융감독원|금감원', clause):
                continue
            if not re.search(r'반대\s*매매|(?:신용(?:거래)?\s*융자|증권사).{0,60}담보\s*(?:비율|유지)', clause):
                continue
            if re.search(r'검토|건의|요구|제안|가능성|가정|예시|아니|않|철회|취소|잘못|오보|부인', clause):
                continue
            if re.search(r'(?:면제|유예|완화|중단|재개|변경|조정)(?:하기로|키로|한다고|한다|했다)', clause):
                return True
    return False


def has_market_event_evidence(event_type, segments):
    """Require an index's own movement; callers authenticate body segments."""
    segments = tuple(segments)
    if event_type == '반대매매':
        return any(re.search(r'반대매매|강제\s*청산|마진콜', segment) for segment in segments)
    if event_type != '기타리스크':
        return False
    for segment in segments:
        normalized = re.sub(r'\s+', ' ', segment)
        clauses = re.split(
            r'(?<!\d)[.!?。;](?!\d)',
            normalized,
        )
        for clause in clauses:
            if re.search(r'서킷\s*브레이커|사이드\s*카', clause):
                return True
            for match in _INDEX_DECLINE.finditer(clause):
                if not _NEGATED.search(clause[match.end():match.end() + 24]):
                    return True
    return False
