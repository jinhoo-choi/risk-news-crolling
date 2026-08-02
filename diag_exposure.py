"""익스포저없음 강등 미작동 원인 진단 (2026-08-02 21시 지역농협 건).

8/2 21:04 발송분에서 "지역농협 NPL펀드 셀프투자 허용"이 주의 5.5로 나갔다.
regrade_by_score()에는 "익스포저 없음 → 참고 직행" 로직이 있고 발송 시점
코드에도 포함돼 있었으며, 샌드박스에서 재현하면 정상 강등된다. 실환경에서만
우회된 셈이라 파이프라인 각 지점의 실제 값을 찍어 원인을 좁힌다.

이메일은 발송하지 않는다 — 진단 결과만 diag_exposure.txt에 기록 후 커밋해
회수한다(Azure blob 로그 URL은 샌드박스에서 접근 불가하므로 기존 방식 준용).
"""
import io
import json
import os
import sys
import contextlib

import naver_news_monitor as nm

OUT = []
def w(line=""):
    print(line)
    OUT.append(str(line))

w("=" * 64)
w("익스포저없음 강등 진단")
w("=" * 64)

exposure_data = nm.load_exposure_data()
w(f"\n[1] 익스포저 로드: {len(exposure_data)}개 종목키")

# ── 2. entity 매칭 실태 ────────────────────────────────────────────
w("\n[2] find_exposure() 매칭 결과")
for ent in ["지역농협", "농협", "NH농협", "농협금융지주", "농협은행",
            "농협중앙회", "지역 농협", "NH농협은행"]:
    rows = nm.find_exposure(ent, exposure_data)
    names = sorted({r.get("종목명", "") for r in rows})
    w(f"    {ent:12s} → {len(rows):2d}행  {names[:3]}")

# ── 3. 강등 로직 단독 재현 ─────────────────────────────────────────
w("\n[3] regrade_by_score() 재현 — 8/2 실제 기사")
art = {
    "title": "“23조 부실 털자”…지역농협 NPL펀드 셀프투자 허용",
    "entity": "지역농협",
    "grade": "주의",
    "event_type": "유동성위기",
    "_ai_confidence": 0.75,
    "_risk_score": 5.5,
    "url": "https://www.sedaily.com/article/20074827",
    "link": "https://www.sedaily.com/article/20074827",
    "pub_date": "2026-08-02 18:19",
}
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    out = nm.regrade_by_score([dict(art)], exposure_data)
w(f"    결과 등급: {out[0].get('grade')}")
w(f"    _grade_locked: {out[0].get('_grade_locked')}")
for line in buf.getvalue().splitlines():
    if line.strip():
        w(f"    | {line.strip()}")

# ── 4. 면제 조건 개별 평가 ─────────────────────────────────────────
w("\n[4] 면제 조건 평가 (어느 분기로 빠지는가)")
for label, a in [
    ("정상형(entity=지역농협)", dict(art)),
    ("entity 공백",            {**art, "entity": ""}),
    ("entity 앞뒤 공백",        {**art, "entity": " 지역농협 "}),
    ("_force_urgent",          {**art, "_force_urgent": True}),
]:
    ev = (a.get("entity") or "").strip()
    exempt = ("_force_urgent" if a.get("_force_urgent")
              else "entity없음" if not ev
              else "익스포저있음" if nm.find_exposure(ev, exposure_data)
              else "-")
    w(f"    {label:22s} entity={a.get('entity')!r:14s} 면제={exempt}")

# ── 5. 하드 제외 게이트 (신규 규제완화 규칙) ───────────────────────
w("\n[5] is_hard_excluded() — 신규 규제완화 게이트")
excluded, reason = nm.is_hard_excluded(art["title"])
w(f"    차단여부={excluded}  사유={reason}")
w("    ※ 이 게이트가 켜진 뒤로는 애초에 AI 판정 전에 걸러진다.")

# ── 6. 실제 파이프라인 순서 확인 ───────────────────────────────────
w("\n[6] 코드 내 강등 로직 위치 확인")
src = open("naver_news_monitor.py", encoding="utf-8").read()
for marker in ["익스포저없음 강등", "_grade_locked", "_verify_high_risk_by_claude("]:
    w(f"    '{marker}' 출현 {src.count(marker)}회")

# ── 7. seen_news 기록에서 8/2 21시 슬롯 확인 ───────────────────────
w("\n[7] seen_news.json — 8/2 21시 슬롯 combos")
try:
    seen = json.load(open("seen_news.json", encoding="utf-8"))
    for key in sorted(seen):
        if "08-02" in key:
            combos = (seen[key] or {}).get("combos") or []
            hits = [c for c in combos if any("농협" in str(x) for x in c)]
            w(f"    {key}: combos {len(combos)}건 / 농협 관련 {len(hits)}건 {hits}")
except Exception as e:
    w(f"    읽기 실패: {e}")

w("\n" + "=" * 64)
with open("diag_exposure.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
print("\n진단 결과 기록: diag_exposure.txt")
