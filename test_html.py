"""이메일 HTML 생성 자동 검증.

배경: 전수점검에서 '수동 검수만 하던' 마지막 영역. 과거 이 경로에서
발송 자체가 중단되는 결함이 나왔다.
  · AI 응답의 null(entity·url·pub_str)에서 AttributeError → 회차 메일 전체 유실
  · 담보비율 0%가 '최고위험 고객 0%'로 표시되어 오독 유발

메일은 임원이 직접 보는 산출물이라 깨짐이 곧 신뢰 훼손이다.
실패 시 종료코드 1.
"""
import sys, types, os, io, re, contextlib
import pandas as pd

_fake = types.ModuleType("yfinance")


class _T:
    def __init__(self, tk):
        pass

    def history(self, **k):
        return pd.DataFrame()


_fake.Ticker = _T
sys.modules["yfinance"] = _fake
for _k in ["EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER",
           "ANTHROPIC_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"]:
    os.environ.setdefault(_k, "x@t.com" if "EMAIL" in _k else "x")

import importlib.util
_spec = importlib.util.spec_from_file_location("nm", "naver_news_monitor.py")
nm = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(nm)
except SystemExit:
    pass

with contextlib.redirect_stdout(io.StringIO()):
    EXPO = nm.load_exposure_data()

fails = []


def check(name, cond, detail=""):
    if not cond:
        fails.append((name, detail))
    print(f"  {'OK  ' if cond else 'FAIL'} {name}{('  ' + detail) if detail and not cond else ''}")


def build(articles, expo=None):
    with contextlib.redirect_stdout(io.StringIO()):
        return nm.build_email_html(articles, total_count=len(articles), ai_summary="요약",
                                   exposure_data=expo if expo is not None else EXPO,
                                   ref_date="2026-07-27", today_str="2026-07-28")


def tag_balance(html):
    """MSO 조건부 주석은 Outlook 전용 코드라 제외하고 균형을 본다."""
    clean = re.sub(r'<!--\[if mso\]>.*?<!\[endif\]-->', '', html, flags=re.S)
    clean = re.sub(r'<!--.*?-->', '', clean, flags=re.S)
    bad = []
    for tag in ["html", "body", "table", "tr", "td", "a", "span", "div", "p", "details"]:
        o = len(re.findall(rf"<{tag}[\s>]", clean))
        c = clean.count(f"</{tag}>")
        if o != c:
            bad.append(f"{tag} {o}/{c}")
    return bad


BASE = {"url": "http://x", "keyword": "부도", "pub_str": "07/28", "entities": []}

CASES = [
    ("정상 3건", [
        {**BASE, "id": 1, "grade": "긴급", "title": "A사 부도", "entity": "삼성전자",
         "entities": ["삼성전자"], "_risk_score": 8.0, "action": "확인 요망"},
        {**BASE, "id": 2, "grade": "주의", "title": "B사 회생", "entity": "코오롱티슈진",
         "entities": ["코오롱티슈진"], "_risk_score": 6.0},
        {**BASE, "id": 3, "grade": "참고", "title": "C사 동향", "entity": "", "_risk_score": 3.0},
    ]),
    ("빈 목록", []),
    ("None 값 다수", [
        {"id": 1, "grade": "주의", "title": "T", "url": None, "entity": None,
         "entities": None, "keyword": None, "pub_str": None, "_risk_score": None,
         "event_type": None, "action": None},
    ]),
    ("초장문 제목(300자)", [
        {**BASE, "id": 1, "grade": "참고", "title": "가" * 300, "entity": "삼성전자",
         "entities": ["삼성전자"], "_risk_score": 3.0},
    ]),
    ("미등록 종목", [
        {**BASE, "id": 1, "grade": "긴급", "title": "미등록사 부도", "entity": "없는회사XYZ",
         "entities": ["없는회사XYZ"], "_risk_score": 8.0},
    ]),
    ("XSS·특수문자", [
        {**BASE, "id": 1, "grade": "긴급",
         "title": '<script>alert(1)</script> & "부도" \u201c전각\u201d', "url": "http://a?x=1&y=2",
         "entity": "삼성전자", "entities": ["삼성전자"], "_risk_score": 8.0},
    ]),
]

print("=" * 74)
print("[1] 생성 안정성 — 예외 없이 유효한 HTML")
print("=" * 74)
for name, arts in CASES:
    try:
        h = build(arts)
        bad = tag_balance(h)
        ph = len(re.findall(r'\{[a-z_]+\}', h))
        nn = h.count(">None<")
        xss = "<script>alert" in h
        ok = (not bad) and ph == 0 and nn == 0 and (not xss) and "</html>" in h
        detail = f"태그{bad} 플레이스홀더{ph} None{nn} XSS{xss}"
        check(f"{name:20} {len(h):>7,}자", ok, detail)
    except Exception as e:
        check(f"{name:20}", False, f"{type(e).__name__}: {e}")

print("\n" + "=" * 74)
print("[2] 필수 요소 — 임원이 보는 핵심 정보")
print("=" * 74)
h = build(CASES[0][1])
for tok, label in [("긴급", "등급 헤더"), ("삼성전자", "종목명"),
                   ("한국투자증권 익스포저", "익스포저 카드"),
                   ("뱅키스", "채널 마커"), ("영업점", "채널 마커"),
                   ("⚡ 대응방안", "대응방안"), ("리스크 점수 참고 기준", "점수 안내"),
                   ("최진후 차장", "담당자")]:
    check(f"{label:14} '{tok}'", tok in h)

print("\n" + "=" * 74)
print("[3] 여신잔고 표 열 순서 — 종목명 → 전체 여신 → 위험고객 → 최고 리스크")
print("=" * 74)
with contextlib.redirect_stdout(io.StringIO()):
    ph = nm.build_price_alert_section(EXPO, "2026-07-27")
if ph:
    ths = re.findall(r'<th[^>]*>([^<]+)</th>', ph)
    expect = ["종목명", "전체 여신", "⚠ 위험고객", "최고 리스크"]
    check(f"헤더 {ths}", ths == expect, f"기대 {expect}")
else:
    print("  SKIP 가격 데이터 없음(오프라인) — 열 순서는 소스로 확인")
    src = open("naver_news_monitor.py", encoding="utf-8").read()
    i = src.index(">종목명</th>")
    seg = src[i:i + 700]
    order_ok = seg.index("전체 여신") < seg.index("⚠ 위험고객")
    check("소스상 헤더 순서(전체 여신 → 위험고객)", order_ok)

print("\n" + "=" * 74)
print("[4] 담보비율 이상치 방어 — 100 미만은 미표시")
print("=" * 74)
src = open("naver_news_monitor.py", encoding="utf-8").read()
s = src.index("    def _top_line(dot_color, rbal, cust, ratio):")
e = src.index("    def _top_risk_cell", s)
ns = {}
exec("def _w():\n" + src[s:e] + "\n    return _top_line\n", ns)
f = ns["_w"]()
for v, expect in [("0", None), ("99", None), ("140.58", "141"),
                  ("149.9", "150"), ("", None)]:
    out = f("#2563eb", "1", "오*구", v)
    m = re.findall(r'>([\d.]+)%<', out)
    got = m[0] if m else None
    check(f"담보비율 {v!r:9} → {got or '미표시'}", got == expect,
          f"기대 {expect or '미표시'}")
    if expect is None and v:
        check(f"  └ 잔고·고객은 유지", "1억" in out)

print("\n" + "=" * 74)
print(f"결과: {'전체 통과' if not fails else f'{len(fails)}건 실패'}")
print("=" * 74)
for n, d in fails:
    print(f"  · {n} — {d}")
sys.exit(0 if not fails else 1)
