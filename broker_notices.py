"""
경쟁사 증권사 신용·대출 공지사항 크롤러
data/ 폴더에 증권사별 CSV 저장
컬럼: date, company, title, url
"""

import requests
from bs4 import BeautifulSoup
import csv
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, urljoin

KST = timezone(timedelta(hours=9))
DATA_DIR = "data"

# ─────────────────────────────────────────────
# 크롤링 대상 증권사 공지사항 URL
# ─────────────────────────────────────────────
BROKERS = [
    {
        "company": "미래에셋증권",
        "url": "https://securities.miraeasset.com/bbs/board/message/list.do?categoryId=66&listType=1&curPage=1",
        "type": "miraeasset",
    },
    {
        "company": "삼성증권",
        "url": "https://www.samsungpop.com/mbw/customer/noticeEvent.do?cmd=getNoticeList&currentPage=1&rowsPerPage=10&listRow=10&ntcSect=3&siteGubun=P&sortColumn=ProcDTime2&sortType=DESC&searchType=0&Search=1&SearchText=",
        "type": "samsung",
    },
    {
        "company": "NH투자증권",
        "url": "https://www.nhsec.com/wooriwmBoard/boardList.action?sBoard_Id=1&sType_Cd=0000000002",
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
        "url": "https://docs-api.tossinvest.com/api/v1/post/search?type=NOTICE&page=0&size=20",
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


def parse_notices(res, broker: dict) -> list:
    """수정한 4개 소스의 실제 목록만 파싱. 구조 불일치는 성공으로 숨기지 않는다."""
    kind, company = broker["type"], broker["company"]
    today = datetime.now(KST).strftime("%Y-%m-%d")
    results = []
    if kind in ("samsung", "toss"):
        data = res.json()
        if kind == "toss":
            data = data["result"]
        total = data["pagingParam"]["totalRowSize"] if kind == "toss" else data["totalCount"]
        if not isinstance(total, int) or total < 0:
            raise ValueError("공지 총건수 구조 불일치")
        rows = data["list"]
        if not isinstance(rows, list):
            raise ValueError("공지 list가 배열이 아님")
        if kind == "samsung":
            fixed = data["fixlist"]
            if not isinstance(fixed, list):
                raise ValueError("공지 fixlist가 배열이 아님")
            rows = fixed + rows
        if not rows and total:
            raise ValueError("공지 총건수와 빈 목록 불일치")
        seen = set()
        for row in rows:
            if kind == "samsung":
                title, post_id = row["ntcTitle1"], row["menuSeqNo"]
                link = "https://www.samsungpop.com/mbw/customer/noticeEvent.do?" + urlencode(
                    {"cmd": "noticeView", "MenuSeqNo": post_id})
            else:
                if str(row["type"]).upper() != "NOTICE":
                    raise ValueError("NOTICE 이외의 응답")
                title, post_id = row["title"], row["id"]
                link = "https://corp.tossinvest.com/ko/post?" + urlencode(
                    {"type": "notice", "id": post_id, "category": row["category"]["id"]})
            if not isinstance(title, str) or not title.strip() or not str(post_id).isdigit():
                raise ValueError("공지 제목/ID가 올바르지 않음")
            if link not in seen:
                results.append({"date": today, "company": company, "title": title.strip(), "url": link})
                seen.add(link)
        return results

    if kind not in ("miraeasset", "nhqv"):
        return parse_generic(res.text, broker["url"], company)
    # 두 사이트는 EUC-KR. requests의 ISO-8859-1 기본 추측에 의존하지 않는다.
    soup = BeautifulSoup(res.content, "html.parser", from_encoding="euc-kr")
    selector = "table.bbs_linetype2 .subject a" if kind == "miraeasset" else "table.tblType a[onclick]"
    for a in soup.select(selector):
        if kind == "miraeasset":
            match = re.fullmatch(r"javascript:view\('(\d+)'\s*,\s*'\d+'\)", a.get("href", ""), re.I)
            if not match:
                raise ValueError("미래에셋 공지 링크 구조 불일치")
            link = urljoin(broker["url"], "view.do") + "?" + urlencode(
                {"categoryId": 66, "messageId": match[1]})
        else:
            match = re.search(r"viewUp\(\s*'\d+'\s*,\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'view'\s*\)", a["onclick"])
            if not match:
                raise ValueError("NH 공지 링크 구조 불일치")
            type_cd, board_id, main_no, sub_no, answer_lvl = match.groups()
            link = urljoin(broker["url"], "boardView.action") + "?" + urlencode(
                {"type_cd": type_cd, "board_id": board_id, "main_no": main_no,
                 "sub_no": sub_no, "answer_lvl": answer_lvl, "check": "view",
                 "sBoard_Id": board_id, "sType_Cd": type_cd})
        title = a.get_text(strip=True)
        if not title:
            raise ValueError("공지 제목 누락")
        results.append({"date": today, "company": company, "title": title, "url": link})
    if not results:
        table = soup.select_one("table.bbs_linetype2" if kind == "miraeasset" else "table.tblType")
        if table is None or not re.search(r"(검색|조회|등록).*없습니다", table.get_text()):
            raise ValueError("공지 목록 구조 불일치 또는 미확인 빈 응답")
    return results


def crawl_broker(broker: dict, status=None) -> list:
    """단일 증권사 공지 크롤링"""
    company = broker["company"]
    url     = broker["url"]
    if status is None:
        status = {}
    status.update(company=company, url=url, status="error", count=0, http_status=None)
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        status.update(http_status=res.status_code, response_url=res.url)
        res.raise_for_status()
        items = parse_notices(res, broker)
        status.update(status="ok" if items else "empty", count=len(items))
        if broker["type"] in ("kb", "shinhan", "kiwoom"):
            status["parser"] = "legacy_generic"
        print(f"  [{company}] {len(items)}건 수집")
        return items
    except Exception as e:
        status.update(error_type=type(e).__name__, error=str(e))
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
    statuses = []
    for broker in BROKERS:
        status = {}
        items = crawl_broker(broker, status)
        if items:
            try:
                save_csv(broker["company"], items)
            except Exception as e:
                status.update(status="error", error_type=type(e).__name__, error=str(e))
        statuses.append(status)
        time.sleep(1)
    failed = sum(s["status"] == "error" for s in statuses)
    state = "degraded" if failed else "success"
    report = {"checked_at": datetime.now(KST).isoformat(), "status": state,
              "failed_sources": failed, "total_sources": len(statuses), "sources": statuses}
    with open("broker_notices_status.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    summary = [f"### 경쟁사 공지: {state} ({failed}/{len(statuses)} 소스 실패)",
               "| 소스 | 상태 | HTTP | 건수 | 오류 |", "|---|---|---|---|---|"]
    for s in statuses:
        error = s.get("error", "").replace("|", "\\|").replace("\n", " ")
        summary.append(f"| {s['company']} | {s['status']} | {s['http_status']} | {s['count']} | {error} |")
    summary.append("KB·신한·키움은 기존 공통 파서 유지. empty는 HTTP 성공·0건이며 목록 완전성 보증은 아닙니다.")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(summary) + "\n")
    print(f"경쟁사 공지 크롤링 완료: {state} ({failed}/{len(statuses)} 소스 실패)")
    if failed:
        print(f"::error::경쟁사 공지 degraded — {failed}개 소스 실패")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
