"""
경쟁사 증권사 신용·대출 공지사항 크롤러
data/ 폴더에 증권사별 CSV 저장
컬럼: date, company, title, url
"""

import requests
from bs4 import BeautifulSoup
import csv
import os
import time
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
DATA_DIR = "data"

# ─────────────────────────────────────────────
# 크롤링 대상 증권사 공지사항 URL
# ─────────────────────────────────────────────
BROKERS = [
    {
        "company": "미래에셋증권",
        "url": "https://www.miraeasset.com/cs/noti/getNotiList.do",
        "type": "miraeasset",
    },
    {
        "company": "삼성증권",
        "url": "https://www.samsungsecurities.com/common/bbs/list.do?bbsId=notice",
        "type": "samsung",
    },
    {
        "company": "NH투자증권",
        "url": "https://www.nhqv.com/cs/notice/noticeList.do",
        "type": "nhqv",
    },
    {
        "company": "KB증권",
        "url": "https://www.kbsec.com/go.able?linkcd=s10503",
        "type": "kb",
    },
    {
        "company": "신한투자증권",
        "url": "https://www.shinhaninvest.com/siw/customer-service/notice/list.do",
        "type": "shinhan",
    },
    {
        "company": "키움증권",
        "url": "https://www.kiwoom.com/h/customer/board/VNoticeTypeHView",
        "type": "kiwoom",
    },
    {
        "company": "토스증권",
        "url": "https://tossinvest.com/notices",
        "type": "toss",
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def parse_generic(html: str, base_url: str, company: str) -> list:
    """공통 파서 — a 태그 기반 공지 제목·링크 추출"""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    today = datetime.now(KST).strftime("%Y-%m-%d")

    # 공지 목록 영역 탐색 우선순위
    container = (
        soup.find("table", {"class": lambda c: c and "notice" in c.lower()})
        or soup.find("ul",   {"class": lambda c: c and "notice" in c.lower()})
        or soup.find("div",  {"class": lambda c: c and "notice" in c.lower()})
        or soup.find("tbody")
        or soup.body
    )
    if not container:
        return []

    for a in container.find_all("a", href=True):
        title = a.get_text(strip=True)
        if len(title) < 5:
            continue
        href = a["href"]
        if not href.startswith("http"):
            from urllib.parse import urljoin
            href = urljoin(base_url, href)
        results.append({
            "date"   : today,
            "company": company,
            "title"  : title,
            "url"    : href,
        })
    return results


def crawl_broker(broker: dict) -> list:
    """단일 증권사 공지 크롤링"""
    company = broker["company"]
    url     = broker["url"]
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        items = parse_generic(res.text, url, company)
        print(f"  [{company}] {len(items)}건 수집")
        return items
    except Exception as e:
        print(f"  [{company}] 크롤링 실패: {e}")
        return []


def save_csv(company: str, items: list):
    """증권사별 CSV 저장 — data/{company}.csv"""
    os.makedirs(DATA_DIR, exist_ok=True)
    safe_name = company.replace(" ", "_").replace("/", "_")
    fpath = os.path.join(DATA_DIR, f"{safe_name}.csv")

    # 기존 데이터 로드
    existing = []
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                existing = list(reader)
        except Exception:
            existing = []

    # 중복 제거 — (company, title) 기준
    seen = {(r.get("company",""), r.get("title","")) for r in existing}
    new_items = [
        item for item in items
        if (item["company"], item["title"]) not in seen
    ]

    if not new_items:
        return

    # 최근 30일치만 유지
    kst_now = datetime.now(KST)
    cutoff  = (kst_now - timedelta(days=30)).strftime("%Y-%m-%d")
    all_items = [r for r in existing if r.get("date","") >= cutoff] + new_items

    with open(fpath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date","company","title","url"])
        writer.writeheader()
        writer.writerows(all_items)

    print(f"  [{company}] CSV 저장: {fpath} (+{len(new_items)}건)")


def main():
    print(f"[{datetime.now(KST).strftime('%Y-%m-%d %H:%M')}] 경쟁사 공지 크롤링 시작")
    for broker in BROKERS:
        items = crawl_broker(broker)
        if items:
            save_csv(broker["company"], items)
        time.sleep(1)
    print("경쟁사 공지 크롤링 완료")


if __name__ == "__main__":
    main()
