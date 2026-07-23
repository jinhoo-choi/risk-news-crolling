"""신규 후보 키워드 3개(감사의견/관리종목/횡령 혐의) + 제거 후보 2개(서킷브레이커/신용융자) 실측. 읽기전용."""
from naver_news_monitor import crawl_naver_news, KEYWORDS, is_hard_excluded

existing_urls = set()
for kw in KEYWORDS:
    existing_urls.update(a["url"] for a in crawl_naver_news(kw))

print("=== 추가 후보 3개 — 신규 유입량·품질 ===")
for kw in ["감사의견", "관리종목", "횡령 혐의"]:
    arts = crawl_naver_news(kw)
    net_new = [a for a in arts if a["url"] not in existing_urls]
    passed = hard = 0
    print(f"\n[{kw}] 전체 {len(arts)}건 / 신규 {len(net_new)}건")
    for a in net_new:
        excl, reason = is_hard_excluded(a["title"], a.get("desc",""), a.get("url",""))
        if excl: hard += 1
        else:
            passed += 1
            print(f"  [AI전달] {a['title']}")
    print(f"  → 하드제외 {hard} / AI전달 {passed}")

print("\n=== 제거 후보 2개 — 이 키워드로만 잡히는 기사(다른 키워드와 미중복) ===")
others = set()
for kw in KEYWORDS:
    if kw in ("서킷브레이커", "신용융자"):
        continue
    others.update(a["url"] for a in crawl_naver_news(kw))
for kw in ["서킷브레이커", "신용융자"]:
    arts = crawl_naver_news(kw)
    only = [a for a in arts if a["url"] not in others]
    print(f"\n[{kw}] 전체 {len(arts)}건 / 이 키워드 단독 수집 {len(only)}건")
    for a in only[:10]:
        print(f"  · {a['title']}")
