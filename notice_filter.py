"""Shared title-only filter for broker CSVs and competitor notice output."""
from html import unescape
import re

NOTICE_KEYWORDS = ("신용", "대출", "오류")


def matches_notice_title(title) -> bool:
    """OR-match the three keywords in parsed title text, ignoring whitespace.

    Keep the prior output filter's whitespace-insensitive substring semantics:
    e.g. 신용융자, 담보대출 and 주문오류 match. No synonyms or detail-body search.
    Missing/non-string titles never match; response validation stays in parsers.
    """
    if not isinstance(title, str) or not title:
        return False
    # HTML parsers already extract visible text. Do not parse title text again:
    # a literal title such as "API<Error 오류>안내" must retain its keyword.
    compact = re.sub(r"\s+", "", unescape(title))
    return any(keyword in compact for keyword in NOTICE_KEYWORDS)
