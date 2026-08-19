"""가상 기사 엔드투엔드 시뮬레이션 — 파이프라인 통과 후 결과 자동 판정.

sim_verify.py가 함수 단위 검증이라면, 이 스크립트는 '가상 기사 세트를 실제
파이프라인에 태워 최종 메일이 어떻게 나가는가'를 본다. 함수는 통과하는데
파이프라인에 꽂으면 어긋나는 결함(호출 순서·플래그 전파·렌더 조건)을 잡는 게
목적이다. 실제로 8/17 ETF 안내 누락이 그런 유형이었다 — 함수는 정상인데
호출 조건이 등급 경로에 묶여 있어 문구가 생성되지 않았다.

재현하지 못하는 구간: Gemini 1차 / Claude 2차 (API 키·네트워크 없음).
→ AI 판정 결과를 입력으로 주입하고, 그 이후 전 구간은 프로덕션 함수를 그대로
  호출한다. "AI가 이렇게 판정했다면 메일이 이렇게 나간다"를 검증하는 것이다.

기사는 전부 가상이며 URL도 실재하지 않는다. 발송은 하지 않고 렌더만 한다.
실패 시 종료코드 1.
"""
import os, sys, types, re, io, contextlib

import pandas as pd

# yfinance 스텁 — 급락표가 시세를 조회하지 않도록. (실경로는 건드리지 않는다)
_fake = types.ModuleType("yfinance")
_fake.Ticker = type("_T", (), {"__init__": lambda s, t: None,
                               "history": lambda s, **k: pd.DataFrame()})
sys.modules["yfinance"] = _fake

for _k in ("EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER",
           "ANTHROPIC_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"):
    os.environ.setdefault(_k, "x@t.com" if "EMAIL" in _k else "x")

from datetime import datetime, timezone, timedelta
import naver_news_monitor as M

KST = timezone(timedelta(hours=9))
now = datetime.now(KST)

PASS, FAIL = [], []


def check(case, cond, detail=""):
    (PASS if cond else FAIL).append(case)
    print(f"  {'✅' if cond else '❌'} {case}" + (f"\n       {detail}" if detail else ""))


exposure_data = M.load_exposure_data()
ref_date = ""
for _rows in exposure_data.values():
    if _rows:
        ref_date = _rows[0].get("기준일", "")
        break


def art(**kw):
    """AI 판정 직후 상태의 기사 dict. 이후는 프로덕션 함수가 처리한다."""
    base = {
        "url": "https://example.com/sim", "keyword": "리스크",
        "pub_str": now.strftime("%m/%d %H:%M"), "desc": "", "body": "",
        "_ai_confidence": 0.92, "summary": "", "reason": "시뮬레이션",
        "action": "보유 고객 평가손 즉시 산출 → 컴플라이언스부 보고",
    }
    base.update(kw)
    base.setdefault("entities", [base.get("entity", "")])
    return base


# ── 가상 기사 세트 ──────────────────────────────────────────────────
# 8/19 검수에서 잡은 5건이 각각 어떤 기사에서 터졌는지를 재현하고, 정상 건이
# 함께 들어왔을 때 오작동하지 않는지를 같은 회차 안에서 대조한다.
articles = [
    # [1] ETF 구조적 상폐 — AI가 처음부터 '주의'로 판정한 경로.
    #     8/17 21시 실사례. 강등 경로를 타지 않아 안내문구가 없었다.
    art(id=1, grade="주의", event_type="상장폐지",
        title="[시뮬] 수익률 너무 좋은데 상장폐지! TIME 액티브 ETF, 0.7 상관계수 미달",
        desc="상관계수 미달로 상장폐지가 결정됐다. 18일 거래정지.",
        entity="TIME 미국배당다우존스액티브",
        action="보유 고객 평가손 즉시 산출 → 18일 거래정지 전 환매 절차 점검",
        customer_notice=("[한국투자증권] 안내 보유하신 ETF가 상관계수 미달로 "
                         "상장폐지될 예정입니다. 보유 수량 및 주문 가능 상태를 "
                         "즉시 확인하시기 바랍니다. 관련 공시는 "
                         "KIND(kind.krx.co.kr)에서 확인하시기 바랍니다.")),

    # [2] 사건유형 무근거 — 실적 부진 기사에 '횡령배임' 배지 (8/19 14시 우리금융)
    art(id=2, grade="주의", event_type="횡령배임",
        title="[시뮬] 신한 3040억 벌 때 우리 -571억…임종룡의 해외사업, 왜 거꾸로 가나",
        desc="해외사업 손실이 확대되며 실적이 부진했다.",
        entity="우리금융지주",
        action="보유 고객 평가손 즉시 산출 → 해외사업 손실 추이 모니터링"),

    # [3] 사건유형 무근거 — 주주 민원 기사에 '금감원제재' 배지 (8/19 07시 위메이드)
    art(id=3, grade="참고", event_type="금감원제재",
        title='[시뮬] 뿔난 위메이드 주주…"새 주인 실체 불분명" 금감원에 민원 제기',
        desc="주주들이 금감원에 민원을 제기했다.",
        entity="위메이드",
        action="위메이드 보유 고객 동향 모니터링 강화"),

    # [4] 전망성 기사 긴급 과대 (8/19 14시 한빛소프트)
    art(id=4, grade="긴급", event_type="상장폐지",
        title="[시뮬] 한빛, 상장폐지 위험권 … 향후 시장 전망은?",
        desc="주가 1,000원 하회가 지속되며 위험권 진입이 관측된다.",
        entity="한빛소프트",
        action="한빛소프트 보유 고객 평가손 산출 → 주가 1,000원 하회 지속 시 재점검",
        customer_notice=("[한국투자증권] 안내 보유 종목이 강화된 상장폐지 요건에 "
                         "해당할 수 있습니다. 보유 수량을 확인하시기 바랍니다.")),

    # [5] 정상 확정 건 — 위 게이트들이 오작동해 이 건을 훼손하면 안 된다.
    #     대응방안에 종목명이 없어 보강 대상이기도 하다(8/19 21시 듀오백).
    art(id=5, grade="긴급", event_type="거래정지",
        title="[시뮬] 거래소 듀오백, 20일부터 주권매매거래 정지",
        desc="상장폐지 사유 발생으로 주권매매거래를 정지한다.",
        entity="듀오백",
        action="보유 고객 평가손 즉시 산출 → 거래정지 전 주문 가능 상태 점검",
        customer_notice=("[한국투자증권] 긴급 안내 보유 종목이 20일부터 매매거래가 "
                         "정지될 예정입니다. 보유 수량 및 주문 가능 상태를 즉시 "
                         "확인하시기 바랍니다. 관련 공시는 KIND(kind.krx.co.kr)에서 "
                         "확인하시기 바랍니다.")),

    # [6] 정상 확정 건 2 — 근거 표현이 뚜렷해 배지가 유지돼야 한다.
    art(id=6, grade="긴급", event_type="파산부도",
        title="[시뮬] 브라질 하원, 포스코이앤씨 현지법인 파산 규탄안 의결",
        desc="현지법인 파산 절차가 개시됐다.",
        entity="포스코이앤씨",
        action="포스코이앤씨 채권 보유 고객 평가손 즉시 산출 → 컴플라이언스부 보고"),
]

print("═" * 62)
print("  가상 기사 엔드투엔드 시뮬레이션")
print("═" * 62)
print(f"  익스포저 기준일: {ref_date or '(없음)'} / 기사 {len(articles)}건")

# ── 파이프라인 통과 ────────────────────────────────────────────────
_log = io.StringIO()
with contextlib.redirect_stdout(_log):
    M.regrade_by_score(articles, exposure_data)
    for a in articles:
        # 실제 파이프라인이 대응방안에 적용하는 순서 그대로
        a["action"], _ = M.strip_exposure_figures(a["action"])
        a["action"], _ = M.prepend_entity_to_action(a["action"],
                                                    (a.get("entity") or "").strip())
        if a.get("customer_notice"):
            _src = a.get("title", "") + " " + a.get("desc", "")
            a["customer_notice"], _ = M.sanitize_customer_notice(
                a["customer_notice"], M.find_exposure(a.get("entity", ""), exposure_data),
                _src)
gate_log = _log.getvalue()

byid = {a["id"]: a for a in articles}

print("\n[1] ETF 구조적 상폐 — 등급 경로와 무관하게 안내문구 생성")
check("AI가 주의로 판정해도 _notice_exempt 부여", byid[1].get("_notice_exempt") is True,
      f"grade={byid[1].get('grade')}")
check("렌더 조건 통과 (긴급 아니어도 문구 노출)",
      bool(byid[1].get("customer_notice")) and
      (byid[1].get("grade") == "긴급" or byid[1].get("_notice_exempt")))

print("\n[2] 사건유형 무근거 배지 차단")
check("우리금융 실적기사 → 횡령·배임 배지 생략", M._event_badge_label(byid[2]) == "")
check("위메이드 민원기사 → 금감원 제재 배지 생략", M._event_badge_label(byid[3]) == "")
check("듀오백 거래정지 배지 유지", M._event_badge_label(byid[5]) == "거래정지",
      M._event_badge_label(byid[5]))
check("포스코이앤씨 파산 배지 유지", M._event_badge_label(byid[6]) == "파산·부도",
      M._event_badge_label(byid[6]))

print("\n[3] 전망성 기사 긴급 과대 강등")
check("한빛소프트 긴급 → 강등", byid[4].get("grade") != "긴급", f"grade={byid[4].get('grade')}")
check("듀오백 확정 건은 강등 안 함(로그 기준)",
      "[전망성 기사 긴급→주의] 듀오백" not in gate_log)

print("\n[4] 대응방안 — 익스포저 수치 0 · 종목명 표기")
for _id, _ent in ((1, "TIME"), (5, "듀오백"), (6, "포스코이앤씨")):
    _act = byid[_id]["action"]
    check(f"[{_id}] 종목명 표기", _ent in _act[:24], _act[:46])
    check(f"[{_id}] 익스포저 수치 없음",
          not re.search(r'\(\s*(?:뱅키스|영업점)', _act), _act[:46])

print("\n[5] 고객문구 — 완결·URL 보존")
for _id in (1, 5):
    _n = byid[_id].get("customer_notice", "")
    check(f"[{_id}] 문장 완결", _n.rstrip().endswith(("다.", "요.")), f"…{_n[-22:]}")
    check(f"[{_id}] KIND 주소 훼손 없음", "kind.krx.co.kr" in _n)

# ── 요약 입력이 실제 발송 기사만 담는지 ────────────────────────────
print("\n[6] 요약 입력 = 실제 발송 기사 집합")
_scope = M.decide_send_scope(articles, exposure_data, ref_date)
_mail = M.filter_articles_for_scope(articles, exposure_data, _scope["self_only"])
_mail_titles = {a["title"] for a in _mail}
_dropped = [a["title"] for a in articles if a["title"] not in _mail_titles]
check("발송 제외분이 요약 입력에서도 빠짐",
      all(t not in _mail_titles for t in _dropped),
      f"발송 {len(_mail)}건 / 제외 {len(_dropped)}건")

# ── 최종 HTML 렌더 ────────────────────────────────────────────────
html = M.build_email_html(
    _mail, total_count=1287,
    ai_summary="[시뮬레이션] 파이프라인 점검 — 실제 리스크 아님",
    exposure_data=exposure_data, ref_date=ref_date,
    today_str=now.strftime("%Y-%m-%d"))

print("\n[7] 최종 메일 렌더")
check("HTML 생성", len(html) > 3000, f"{len(html):,} bytes")
check("무근거 배지가 메일에 없음",
      ">횡령·배임<" not in html and ">금감원 제재<" not in html)
check("정상 배지는 메일에 있음", ">거래정지<" in html or "거래정지" in html)
check("잘림 표시('...') 없음", "...</" not in html)

with open("sim_pipeline_output.html", "w", encoding="utf-8") as f:
    f.write(html)

print("\n" + "─" * 62)
print("  [파이프라인 로그]")
for _line in gate_log.strip().split("\n"):
    if _line.strip():
        print("   " + _line.strip())

print("\n" + "═" * 62)
print(f"  엔드투엔드 시뮬레이션: {len(PASS)}건 통과 / {len(FAIL)}건 실패")
if FAIL:
    for f_ in FAIL:
        print(f"    ❌ {f_}")
print(f"  렌더 결과: sim_pipeline_output.html")
print("═" * 62)
sys.exit(1 if FAIL else 0)
