"""신규 후보 키워드 3개('한국투자증권 지연/미지급/이슈') 수집량 실측 — 읽기전용."""
from naver_news_monitor import crawl_naver_news, KEYWORDS, is_hard_excluded

existing_urls = set()
for kw in KEYWORDS:
    existing_urls.update(a["url"] for a in crawl_naver_news(kw))

candidates = ["한국투자증권 지연", "한국투자증권 미지급", "한국투자증권 이슈"]
for kw in candidates:
    arts = crawl_naver_news(kw)
    net_new = [a for a in arts if a["url"] not in existing_urls]
    print(f"\n=== [{kw}] 전체 {len(arts)}건 / 기존과 중복 제외 신규 {len(net_new)}건 ===")
    for a in net_new:
        excl, reason = is_hard_excluded(a["title"], a.get("desc",""), a.get("url",""))
        tag = f"하드제외:{reason}" if excl else "AI전달"
        print(f"  [{tag}] {a['title']}")
