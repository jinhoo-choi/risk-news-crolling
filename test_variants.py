"""과적합 검증 — 오탐/정탐 기사를 유사 형태 다른 단어로 변형해 재검수"""
import sys,types,pandas as pd,os,json,io,contextlib
fake=types.ModuleType("yfinance")
class T:
    def __init__(s,t): pass
    def history(s,**k): return pd.DataFrame()
fake.Ticker=T; sys.modules["yfinance"]=fake
for k in ["EMAIL_SENDER","EMAIL_PASSWORD","EMAIL_RECEIVER","ANTHROPIC_API_KEY","NAVER_CLIENT_ID","NAVER_CLIENT_SECRET"]:
    os.environ.setdefault(k,"x@t.com" if "EMAIL" in k else "x")
import importlib.util
spec=importlib.util.spec_from_file_location("nm","naver_news_monitor.py")
nm=importlib.util.module_from_spec(spec)
try: spec.loader.exec_module(nm)
except SystemExit: pass

def check(title, desc=""):
    return nm.is_hard_excluded(title, desc, "")

# ── 오탐 변형: 종목명·수치·표현을 바꾸되 구조는 동일 → 여전히 차단돼야 ──
FP_VARIANTS = [
 # (원본 유형, 변형 제목, desc)
 ("호재성 상폐/공개매수","LG㈜, LG헬로비전 상장폐지·매각…소액주주 주식 15% 프리미엄가 공개매수",""),
 ("호재성 상폐/잔여지분","카카오, 잔여 지분 현금 매입 완료…코스닥 상장폐지",""),
 ("완전자회사 편입","신한지주, 제주은행 완전 자회사 편입 마무리",""),
 ("주식교환 자진상폐","포스코홀딩스, 포스코엠텍 주식교환일 확정…상장폐지 수순",""),
 ("기술적 거래정지","거래소 \"대한제강, 8월 3일부터 주권매매거래정지\"","정지 사유는 액면분할에 따른 전자등록 변경이다."),
 ("기술적 거래정지2","OO케미칼, 무상증자 신주권 변경상장까지 매매거래 정지",""),
 ("연예 파생","박서준, 회생 신청한 소속사 관련 인스타 해명…누리꾼 갑론을박",""),
 ("연예 가십","정우성, 파산 위기 제작사 드라마 촬영 강행…팬들 뭇매",""),
 ("응원매수(공백변형)","한성기업 상한가, '상장폐지 저지' 응원 매수 행렬",""),
 ("돈쭐(공백변형)","위기 기업 돈 쭐 내주자…소액주주 매수 확산",""),
 ("브래킷 코너물","[증시 돋보기] 7월 5주차 - 셀트리온·한미약품外",""),
 ("브래킷 말미","LG화학 실적 부진 지속…투자자 고심 [애널리스트 노트]",""),
 ("경쟁사 호실적","대신증권, 전 부문 성장에 3분기 순익 900억…ROE 상승",""),
 ("인가 호재","화려한 STO 인가, 그러나 남은 과제…미래에셋 주주가치 쟁점",""),
 ("마케팅 이벤트","OO증권 해외주식 수수료 할인 이벤트 진행",""),
 ("호실적 최대","교보증권 사상 최고 실적 경신",""),
]

# ── 정탐 변형: 구조 동일, 종목·수치만 변경 → 통과해야(차단되면 미탐) ──
TP_VARIANTS = [
 ("상폐 가처분","대유에이텍, 상장폐지 효력정지 가처분 신청",""),
 ("감사의견 거절","OO바이오, 감사의견 거절로 주권매매거래정지","감사의견 거절에 따른 상장폐지 사유 발생"),
 ("부도 거래정지","XX건설 부도 발생…채권 전자등록 변경 절차 착수",""),
 ("횡령 공시","대창단조, 횡령·배임 혐의 발생 공시",""),
 ("당사 장애","한국투자증권 HTS 접속 지연, 매매 30분 중단",""),
 ("당사 제재","한국투자증권 불완전판매 과태료 3억 제재",""),
 ("회생 신청","YY리츠 300억 채무불이행…법원에 회생 신청",""),
 ("반대매매 확정","반대매매 사상 최대…하루 2500억 강제청산 발생",""),
 ("실적 쇼크","마이크론 3분기 실적 예상치 -20% 쇼크…가이던스 하향",""),
 ("신용등급 강등","한신평, ZZ건설 신용등급 A→BBB 하향…부정적 검토",""),
 ("차환 실패","△△개발 ABCP 차환 실패 우려…만기 2주 앞두고 미매각",""),
 ("적대적 M&A","경영권 분쟁 심화…적대적 공개매수 방어 나선 □□기업",""),
 ("호재 무산(신규)","◇◇전자 대규모 수주 취소…유동성 위기 확산",""),
 ("호재 무산2","◆◆사 MOU 파기로 사업 중단…자금난 심화",""),
 ("소비자 불만","○○증권 이벤트 경품 미지급 논란…고객 불만 폭주",""),
 ("관리종목 지정","▲▲테크 관리종목 지정…자본잠식 50% 초과",""),
]

print("="*80)
print("[A] 오탐 변형 16건 — 차단돼야 정상 (통과 시 과적합)")
print("="*80)
fail_a=[]
for kind,t,d in FP_VARIANTS:
    g,r=check(t,d)
    if not g: fail_a.append((kind,t))
    print(f"  {'✅차단' if g else '❌통과'} | {kind:16} | {str(r)[:22]:22} | {t[:40]}")

print("\n"+"="*80)
print("[B] 정탐 변형 16건 — 통과해야 정상 (차단 시 미탐)")
print("="*80)
fail_b=[]
for kind,t,d in TP_VARIANTS:
    g,r=check(t,d)
    if g: fail_b.append((kind,t,r))
    print(f"  {'❌차단' if g else '✅통과'} | {kind:16} | {str(r)[:22]:22} | {t[:40]}")

print("\n"+"="*80)
print(f"결과: 오탐변형 {len(FP_VARIANTS)-len(fail_a)}/{len(FP_VARIANTS)} 차단  |  "
      f"정탐변형 {len(TP_VARIANTS)-len(fail_b)}/{len(TP_VARIANTS)} 통과")
print("="*80)
if fail_a:
    print("\n[과적합 — 변형 시 놓침]")
    for k,t in fail_a: print(f"  · {k}: {t}")
if fail_b:
    print("\n[★미탐 — 변형 시 오차단]")
    for k,t,r in fail_b: print(f"  · {k}: {t}\n    사유: {r}")
