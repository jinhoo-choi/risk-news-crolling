"""14시간 윈도우 확대 후 실측 — 페이지네이션 한도(300건) 초과 키워드 여부 확인. 읽기전용."""
from naver_news_monitor import crawl_naver_news, KEYWORDS

total = 0
for kw in KEYWORDS:
    arts = crawl_naver_news(kw)
    total += len(arts)
    flag = " ⚠️한도근접" if len(arts) >= 250 else ""
    print(f"  [{kw}] {len(arts)}건{flag}")
print(f"\n23개 키워드 합계(14시간, URL중복 포함): {total}건")
