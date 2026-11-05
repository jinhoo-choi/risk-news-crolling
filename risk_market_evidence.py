"""Shared source-only market attribution, not severity or permission to send."""
import re

# Only source words that connect an index subject to its own numeric movement.
# Never consume arbitrary company names between the index and decline predicate.
_INDEX_DECLINE = re.compile(
    r'(?:코스피|코스닥)\s*(?:지수)?(?!\s*(?:보다|대비))(?:는|은|가|이|도)?\s*'
    r'(?:(?:전\s*거래일|전일|전날|직전\s*거래일|종가|대비|보다|대비해|대비하여|'
    r'포인트|퍼센트|p|약|이상|가량|정도|장중|개장\s*직후|오전|오후|현재|일|시|분|대|넘게)|'
    r'[\d\s.,%+\-()])*'
    r'(?:급락|폭락|하락|내렸|내린|내려|붕괴)'
)
_NEGATED = re.compile(r'않|아니|없|할\s*수|가능|전망|예상|우려')


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
