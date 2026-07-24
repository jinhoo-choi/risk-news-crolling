"""2차 검증 judgment 판정 품질 진단 — 이메일 발송 없음, 읽기 전용.

그간의 오탐 이력(regression_set.json)과 현재 수집 기사를 실제 2차 검증
프롬프트에 통과시켜, ①핵심사건 ②손실주체 ③확정여부 판정이 어떻게
나오는지 수집한다. 체크리스트(1번안) 설계 근거 데이터 확보 목적.
"""
import json, requests, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from naver_news_monitor import (ANTHROPIC_KEY, CLAUDE_MODEL,
                                 load_exposure_data, find_exposure)

_EXP = load_exposure_data()

def _f0(v):
    try: return float(str(v).replace(",", "").strip() or 0)
    except (ValueError, TypeError): return 0.0

def _exposure_text(entity):
    lines = []
    for r in (find_exposure(entity, _EXP) if entity else [])[:6]:
        b = _f0(r.get("뱅잔고")) + _f0(r.get("영잔고"))
        c = _f0(r.get("뱅고객수")) + _f0(r.get("영고객수"))
        if b or c:
            lines.append(f"  - {r.get('종목명','')} ({r.get('종목유형','')}): {b:,.0f}억, {c:,.0f}명")
    if lines:
        return "당사 보유 현황(뱅키스+영업점 합산):\n" + "\n".join(lines)
    if entity:
        return f"당사 보유 현황: '{entity}' 관련 주식·채권·여신 보유 없음 (익스포저 0)"
    return "당사 보유 현황: 대상 종목 특정 불가(시장 전체 이슈 가능성)"

def judge(title, body="", entity=""):
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
- 리스크 O (당사 보유 해외종목 급락): 당사 해외주식 잔고가 있으면 -8%↑ 급락·실적쇼크는 리스크 O
- 리스크 O (ETF 투자유의종목 지정): 당사 보유 ETF의 투자유의·상폐사유 발생은 리스크 O

제목: {title}
본문(앞부분): {body_preview}

{_exposure_text(entity)}

먼저 아래 4가지를 순서대로 판단한 뒤 결론을 내리세요.
① 핵심사건: 이 기사가 보도하는 사건 한 가지를 15자 이내로
② 손실주체: "주주" / "회사" / "채권자" / "없음"
   ★ 공개매수 프리미엄·완전자회사 편입·주식분할처럼 주주가 보상을 받거나
     아무 영향이 없으면 "없음"이며 risk는 반드시 false입니다.
③ 당사연관: "직접" / "당사이슈" / "무관"
   ★ 익스포저 0이고 당사 자체 이슈도 아니면 "무관", risk는 false입니다.
   ★ 경쟁사의 전산장애·민원은 "무관". 단 대규모 손실·부도·상폐처럼 주가 급락을
     유발하는 사건이고 당사 보유 잔고가 있으면 "직접"입니다.
   ★ 시장 전체 시황으로 특정 종목 미지목이면 "무관"입니다.
④ 확정여부: "확정" / "가능성" / "무관"

JSON만 출력:
{{"judgment": {{"핵심사건": "...", "손실주체": "...", "당사연관": "...", "확정여부": "..."}}, "risk": true}}
또는
{{"judgment": {{"핵심사건": "...", "손실주체": "...", "당사연관": "...", "확정여부": "..."}}, "risk": false, "reason": "한 줄 이유"}}"""
    try:
        res = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=30)
        if res.status_code != 200:
            return {"error": f"HTTP {res.status_code}"}
        raw = res.json().get("content", [{}])[0].get("text", "").strip()
        # 마크다운 코드펜스 제거 후 파싱 (```json ... ``` 형태 대응)
        cleaned = raw
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except Exception as pe:
            return {"error": f"parse: {pe}", "_raw": raw[:200]}
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
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(judge, x['title'], x.get('desc',''), x.get('entity','')): x for x in items}
        for fu in as_completed(futs):
            x = futs[fu]
            results.append((x, fu.result()))
            time.sleep(0.3)
    wrong = []
    errs = []
    for x, r in results:
        if "error" in r:
            errs.append((x, r))
            print(f"  ERR  {x['title'][:44]} — {r['error'][:50]}")
            if r.get("_raw"):
                print(f"       원문: {r['_raw'][:120]}")
            continue
        risk = r.get("risk", True)
        j = r.get("judgment", {}) or {}
        ok = (risk == expect_risk)
        if not ok: wrong.append((x, r))
        mark = "OK  " if ok else "★오판"
        print(f"  {mark} risk={str(risk):5} | 사건:{str(j.get('핵심사건',''))[:16]:16} "
              f"손실:{str(j.get('손실주체','')):4} 당사연관:{str(j.get('당사연관','')):5} | {x['title'][:34]}")
    ok_n = len(results) - len(wrong) - len(errs)
    print(f"\n  정확도: {ok_n}/{len(results)} (오판 {len(wrong)}건, 응답오류 {len(errs)}건)")
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
          f"손실주체={j.get('손실주체')} / 당사연관={j.get('당사연관')} / 확정={j.get('확정여부')}")
    if r.get('reason'): print(f"    이유: {r.get('reason')}")
