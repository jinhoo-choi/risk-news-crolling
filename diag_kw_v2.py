"""'횡령 배임'(AND) 재실측 — 읽기전용."""
from naver_news_monitor import crawl_naver_news, KEYWORDS, is_hard_excluded
existing_urls = set()
for kw in KEYWORDS:
    existing_urls.update(a["url"] for a in crawl_naver_news(kw))
arts = crawl_naver_news("횡령 배임")
net_new = [a for a in arts if a["url"] not in existing_urls]
print(f"[횡령 배임] 전체 {len(arts)}건 / 신규 {len(net_new)}건")
for a in net_new:
    excl, reason = is_hard_excluded(a["title"], a.get("desc",""), a.get("url",""))
    tag = f"하드제외:{reason}" if excl else "AI전달"
    print(f"  [{tag}] {a['title']}")
