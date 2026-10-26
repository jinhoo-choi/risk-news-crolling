"""Reject recognisable extraction failures; this is not a fact/attribution check."""
import re


def body_quality(text):
    """Keep the source unchanged and return (status, diagnostic reason)."""
    if not any(c.isalnum() for c in text):
        return 'missing', 'body_text_missing'
    # UTF-8/CP949 decoded as Latin-1, or replacement characters, are not prose.
    latin = len(re.findall(r'[\u0080-\u00ff]', text))
    hangul = len(re.findall(r'[가-힣]', text))
    if (text.count('\ufffd') >= 3 or (latin >= 12 and latin > hangul * 2)):
        return 'unreadable', 'body_encoding_corrupt'
    # Do not let a related headline or a copyright sentence certify the article.
    prose = re.split(
        r'이데일리 추천 뉴스|당신을 위한 맞춤 뉴스|놓친 뉴스와 그 맥락까지|'
        r'관련\s*기사|실시간 주요뉴스|여러분의 제보가|저작권자|ⓒ|'
        r'무단\s*전재|Copyright\b', text, maxsplit=1, flags=re.I)[0]
    summary = re.search(r"자동으로 줄인|세 줄 요약|세줄 요약", prose)
    if summary:
        prose = prose[:summary.start()]
    if (len(prose) < 180 and re.search(r'딜링룸|현황판|전경|모습', prose)
            and len(re.findall(r'[가-힣]다[.!?。]', prose)) <= 1
            and re.search(r'ⓒ|사진\s*[=:]|사진\s*제공|연합뉴스|제공\s*[:=]', text)):
        return 'unreadable', 'body_caption_only'
    # Require article prose, not a byline, date, caption, menu, or headline list.
    korean = re.search(r'[가-힣]다[.!?。](?:\s|$|[\"”’])', prose)
    english = re.search(r'(?:\b[A-Za-z][A-Za-z\'-]*\b[ ,;:\-]+){4,}'
                        r'[A-Za-z][A-Za-z\'-]*[.!?](?:\s|$)', prose)
    if not korean and not english:
        return 'unreadable', 'body_summary_only' if summary else 'body_no_article_prose'
    return 'verified', 'body_prose_present'
