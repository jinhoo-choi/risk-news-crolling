"""2차 검증 judgment 판정 품질 진단 — 이메일 발송 없음, 읽기 전용.

그간의 오탐 이력(regression_set.json)과 현재 수집 기사를 실제 2차 검증
프롬프트에 통과시켜, ①핵심사건 ②손실주체 ③확정여부 판정이 어떻게
나오는지 수집한다. 체크리스트(1번안) 설계 근거 데이터 확보 목적.
"""
import json, requests, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from naver_news_monitor import ANTHROPIC_KEY, CLAUDE_MODEL

def judge(title, body=""):
    """2차 검증과 동일한 프롬프트로 판정 (본문 없으면 제목만)"""
    body_preview = (body or "(본문 없음 — 제목만으로 판단)")[:1500]
    prompt = f"""당신은 한국투자증권 개인고객그룹 리스크 담당자입니다.

【당신의 역할 — 2차 정밀 검수】
1차 필터(Gemini)는 제목·요약만 보고 넓게 걸러낸 결과입니다. 당신은 그 통과분
전건을 **본문까지 읽고** 최종 판정하는 마지막 관문입니다.

판단 기준:
- 리스크 O: 상장폐지·파산·부도·기업회생 확정, 당사 채권·PF 손실 가능, 반대매매 급증, MTS 장애, 금감원 제재
- 리스크 X: 연예·방송 인물 에피소드, 인터뷰·인물 기사, 산업 트렌드 분석, 시황 브리핑, 이미 알려진 사건의 단순 경과 보도
- 리스크 X (기술적 거래정지): 주식분할·액면분할·병합·무상증자·전자등록 변경 등 절차상 사유
- 리스크 X (연예·인물 파생): 기업 회생·파산이 배경일 뿐 내용은 인물 SNS·발언 논란
- 리스크 X (호재성 상장폐지): 공개매수 프리미엄·주식교환 완전자회사 편입 등 주주 보상
- 리스크 X (파생 기사): 이미 회생 진행 중 기업의 영업·인사·계약 영향 기사
- 리스크 X (투자경고·과열): 테마주 급등에 따른 투자경고·거래정지

제목: {title}
본문(앞부분): {body_preview}

먼저 아래 3가지를 순서대로 판단한 뒤 결론을 내리세요.
① 핵심사건: 이 기사가 보도하는 사건 한 가지를 15자 이내로
② 손실주체: 이 사건으로 손실을 보는 쪽 — "주주" / "회사" / "채권자" / "없음"
   ★ 공개매수 프리미엄·완전자회사 편입·주식분할처럼 주주가 보상을 받거나
     아무 영향이 없으면 "없음"입니다. 이 경우 risk는 반드시 false입니다.
③ 확정여부: "확정" / "가능성" / "무관"

JSON만 출력:
{{"judgment": {{"핵심사건": "...", "손실주체": "...", "확정여부": "..."}}, "risk": true}}
또는
{{"judgment": {{"핵심사건": "...", "손실주체": "...", "확정여부": "..."}}, "risk": false, "reason": "한 줄 이유"}}"""
    try:
        res = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=30)
        if res.status_code != 200:
            return {"error": f"HTTP {res.status_code}"}
        raw = res.json().get("content", [{}])[0].get("text", "").strip()
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e)[:60]}

d = json.load(open('regression_set.json', encoding='utf-8'))
fp = d['false_positive_history']   # risk=false 여야 정답
tp = d['true_positive_history']    # risk=true 여야 정답

def run(items, expect_risk, label):
    print("=" * 78)
    print(f"[{label}] {len(items)}건 — 정답: risk={expect_risk}")
    print("=" * 78)
    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(judge, x['title'], x.get('desc','')): x for x in items}
        for fu in as_completed(futs):
            x = futs[fu]
            results.append((x, fu.result()))
    wrong = []
    for x, r in results:
        if "error" in r:
            print(f"  ERR  {x['title'][:52]} — {r['error']}"); continue
        risk = r.get("risk", True)
        j = r.get("judgment", {}) or {}
        ok = (risk == expect_risk)
        if not ok: wrong.append((x, r))
        mark = "OK  " if ok else "★오판"
        print(f"  {mark} risk={str(risk):5} | 사건:{str(j.get('핵심사건',''))[:16]:16} "
              f"손실주체:{str(j.get('손실주체','')):4} 확정:{str(j.get('확정여부','')):4} | {x['title'][:38]}")
    print(f"\n  정확도: {len(results)-len(wrong)}/{len(results)}")
    return wrong

w1 = run(fp, False, "오탐 이력 — 걸러내야 함")
print()
w2 = run(tp, True, "정탐 이력 — 통과시켜야 함")

print("\n" + "=" * 78)
print("[오판 상세 — 체크리스트 설계 근거]")
print("=" * 78)
for x, r in w1 + w2:
    j = r.get("judgment", {}) or {}
    print(f"\n  · {x['title']}")
    print(f"    category: {x.get('category', x.get('grade',''))}")
    print(f"    판정: risk={r.get('risk')} / 사건={j.get('핵심사건')} / "
          f"손실주체={j.get('손실주체')} / 확정={j.get('확정여부')}")
    if r.get('reason'): print(f"    이유: {r.get('reason')}")
