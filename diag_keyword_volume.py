"""진단 전용 스크립트 — '한국투자증권' 단독 검색어 추가 시 수집량 증가분 실측.

목적: A안(KEYWORDS에 '한국투자증권' 단독 검색어 추가) 적용 전, 실제 최근
6시간 윈도우(crawl_naver_news와 동일 조건) 기준으로 몇 건이 늘어나는지,
그리고 늘어나는 기사들의 성격(홍보/노이즈 비중)을 실측한다.

이메일 발송·CSV/JSON 파일 기록 없음 — Actions 로그에만 출력되는 순수
읽기 전용 진단.
"""
from naver_news_monitor import crawl_naver_news, KEYWORDS, is_hard_excluded

print("=" * 60)
print("① 기존 KEYWORDS(20개) 수집량 — 최근 6시간")
print("=" * 60)
existing_total = 0
existing_urls = set()
for kw in KEYWORDS:
    arts = crawl_naver_news(kw)
    existing_total += len(arts)
    existing_urls.update(a["url"] for a in arts)
    print(f"  [{kw}] {len(arts)}건")
print(f"기존 20개 키워드 합계(중복 URL 포함): {existing_total}건")
print(f"기존 20개 키워드 합계(중복 URL 제거): {len(existing_urls)}건")

print()
print("=" * 60)
print("② '한국투자증권' 단독 검색어 추가 시 — 최근 6시간")
print("=" * 60)
new_arts = crawl_naver_news("한국투자증권")
print(f"'한국투자증권' 단독 검색 결과: {len(new_arts)}건")

overlap = [a for a in new_arts if a["url"] in existing_urls]
net_new = [a for a in new_arts if a["url"] not in existing_urls]
print(f"  - 기존 키워드와 중복(이미 잡히던 기사): {len(overlap)}건")
print(f"  - 순수 신규 추가분: {len(net_new)}건")

print()
print("=" * 60)
print("③ 신규 추가분 하드제외 통과 여부 (현재 필터 기준)")
print("=" * 60)
hard_excluded, passed = [], []
for a in net_new:
    excl, reason = is_hard_excluded(a["title"], a.get("desc", ""), a.get("url", ""))
    (hard_excluded if excl else passed).append((a, reason))

print(f"  - 코드 하드제외로 걸러짐: {len(hard_excluded)}건")
print(f"  - AI(Gemini/Claude) 판단으로 넘어감: {len(passed)}건")

print()
print("── 하드제외된 신규분 샘플(최대 15건, 사유 포함) ──")
for a, reason in hard_excluded[:15]:
    print(f"  [{reason}] {a['title']}")

print()
print("── AI로 넘어가는 신규분 전체 (실제 증가하는 Gemini 1차 필터 부하) ──")
for a, reason in passed:
    print(f"  · {a['title']}")

print()
print("=" * 60)
print("요약")
print("=" * 60)
print(f"현재 1회 실행당 수집량(20개 키워드, 중복제거): {len(existing_urls)}건")
print(f"'한국투자증권' 추가 시 순수 증가분: {len(net_new)}건")
print(f"  └ 그중 AI 1차 필터까지 실제 전달되는 건수: {len(passed)}건")
if existing_urls:
    pct = round(len(net_new) / len(existing_urls) * 100, 1)
    print(f"수집량 증가율(순증분/기존): 약 {pct}%")
