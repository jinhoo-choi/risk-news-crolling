"""
ticker_mapper.py — 해외주식 티커 → 한글명 자동 매핑
exposure_data.csv에서 해외주식 티커 추출 → yfinance로 한글명 조회 → ticker_map.json 저장
GitHub Actions에서 주기적으로 실행 (CSV 업로드 후)
"""

import csv, json, os, re, time, sys
from pathlib import Path

# ── 설정 ──────────────────────────────────────────
EXPOSURE_FILE  = os.environ.get("EXPOSURE_FILE", "exposure_data.csv")
OUTPUT_FILE    = "ticker_map.json"
MIN_BAL        = 100  # 100억 이상 종목만 매핑 (소요시간 단축)
BATCH_SIZE     = 10  # yfinance 배치 크기
SLEEP_SEC      = 0.3  # 배치 간 딜레이
TICKER_RE      = re.compile(r'^[A-Z]{1,5}(\.[A-Z])?$')

# ── 기본 매핑 (yfinance 실패 시 fallback) ─────────
FALLBACK_MAP = {
    "TSLA":"테슬라",      "NVDA":"엔비디아",    "GOOGL":"알파벳",
    "GOOG":"알파벳",      "AAPL":"애플",         "MSFT":"마이크로소프트",
    "META":"메타",         "AMZN":"아마존",       "AVGO":"브로드컴",
    "MU":"마이크론",       "INTC":"인텔",         "AMD":"AMD",
    "QCOM":"퀄컴",         "ARM":"ARM홀딩스",     "ASML":"ASML",
    "TSM":"TSMC",          "PLTR":"팔란티어",     "IONQ":"아이온큐",
    "QQQ":"나스닥100 ETF", "TQQQ":"나스닥3배 ETF","VOO":"뱅가드S&P500 ETF",
    "SPY":"SPDR S&P500 ETF","SOXL":"반도체레버리지ETF","SOXS":"반도체인버스ETF",
    "SCHD":"슈왑배당ETF",  "QQQM":"나스닥100 ETF(M)","QLD":"나스닥2배ETF",
    "SNDK":"샌디스크",     "TSLL":"테슬라2배ETF", "JEPQ":"JPモルガン나스닥EIM",
    "IREN":"아이렌",       "SPYM":"SPDR S&P500 ETF(M)","RKLB":"로켓랩",
    "SGOV":"단기국채ETF",  "JEPI":"JPモルガン프리미엄EIM","JOBY":"조비에비에이션",
    "O":"리얼티인컴",      "SOXX":"반도체ETF",    "UNH":"유나이티드헬스",
    "KO":"코카콜라",       "ORCL":"오라클",       "MRVL":"마벨테크놀로지",
    "SMH":"반도체ETF(VanEck)","ASHR":"중국A주ETF","RGTI":"리게티컴퓨팅",
    "SMR":"뉴스케일파워",  "GLD":"금ETF",         "SLV":"은ETF",
    "TLT":"장기국채ETF",   "NFLX":"넷플릭스",     "PFE":"화이자",
    "TMF":"장기국채3배ETF","ARM":"ARM홀딩스",     "NVDL":"엔비디아2배ETF",
    "PLUG":"플러그파워",   "LLY":"일라이릴리",    "NKE":"나이키",
    "QCOM":"퀄컴",         "MSTR":"마이크로스트래티지","IBM":"IBM",
    "CPNG":"쿠팡",         "T":"AT&T",             "VRT":"버티브홀딩스",
    "JNJ":"존슨앤존슨",    "COIN":"코인베이스",    "SBUX":"스타벅스",
    "DELL":"델테크놀로지", "DIS":"디즈니",         "XOM":"엑슨모빌",
    "RIVN":"리비안",       "AGNC":"AGNC인베스트먼트","COST":"코스트코",
    "AMAT":"어플라이드머티리얼즈","LRCX":"램리서치","QBTS":"D-Wave퀀텀",
    "RBLX":"로블록스",     "NVO":"노보노디스크",   "BA":"보잉",
    "JPM":"JP모건",        "V":"비자",             "MA":"마스터카드",
    "GS":"골드만삭스",     "BAC":"뱅크오브아메리카","MS":"모건스탠리",
    "WMT":"월마트",        "UBER":"우버",          "ABNB":"에어비앤비",
    "SHOP":"쇼피파이",     "PYPL":"페이팔",        "SPOT":"스포티파이",
    "NIO":"니오",          "XPEV":"샤오펑",        "BABA":"알리바바",
    "MRNA":"모더나",       "ABBV":"애브비",         "AMGN":"암젠",
    "GILD":"길리어드",     "CRM":"세일즈포스",      "ADBE":"어도비",
    "NOW":"서비스나우",    "SNOW":"스노우플레이크", "DDOG":"데이터도그",
    "CRWD":"크라우드스트라이크","PANW":"팔로알토네트웍스",
    "HOOD":"로빈후드",     "SOFI":"소파이",         "LCID":"루시드모터스",
    "GME":"게임스탑",      "AMC":"AMC엔터테인먼트",
    "IVV":"아이쉐어즈S&P500ETF","IWM":"러셀2000ETF",
    "VTI":"뱅가드미국주식ETF","ARKK":"ARK이노베이션ETF",
    "SQQQ":"나스닥인버스3배ETF","SPXL":"S&P500레버리지3배ETF",
    "TXN":"텍사스인스트루먼트","KLAC":"KLA",
}


def load_tickers_from_csv(fpath: str) -> dict:
    """CSV에서 해외주식 티커 추출 → {ticker: 잔고합산} 반환"""
    tickers = {}
    if not os.path.exists(fpath):
        print(f"  [WARN] CSV 없음: {fpath}")
        return tickers
    try:
        with open(fpath, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                유형 = row.get("종목유형","")
                if 유형 not in ("해외주식","해외담보"):
                    continue
                name = row.get("종목명","").strip()
                if not TICKER_RE.match(name):
                    continue
                try:
                    bal = float(str(row.get("잔고(억)","0")).replace(",",""))
                except:
                    bal = 0
                tickers[name] = tickers.get(name, 0) + bal
    except Exception as e:
        print(f"  [ERROR] CSV 로드 실패: {e}")
    return tickers


MAX_TOTAL_SEC  = 300  # 전체 yfinance 조회 최대 5분

def fetch_names_yfinance(tickers: list) -> dict:
    """yfinance로 티커 → 영문 종목명 조회 (최대 MAX_TOTAL_SEC초)"""
    try:
        import yfinance as yf
    except ImportError:
        print("  [WARN] yfinance 미설치")
        return {}

    result = {}
    t_start = time.time()
    for i in range(0, len(tickers), BATCH_SIZE):
        # 전체 타임아웃 체크
        if time.time() - t_start > MAX_TOTAL_SEC:
            print(f"  [WARN] yfinance 타임아웃 — {i}개 처리 후 중단")
            break
        batch = tickers[i:i+BATCH_SIZE]
        try:
            data = yf.Tickers(" ".join(batch))
            for t in batch:
                try:
                    info = data.tickers[t].info
                    name = info.get("longName") or info.get("shortName","")
                    if name:
                        result[t] = name
                except Exception:
                    pass
            time.sleep(SLEEP_SEC)
        except Exception as e:
            print(f"  [WARN] yfinance 배치 실패: {e}")
            time.sleep(1)
    return result


def main():
    print(f"[ticker_mapper] 시작")

    # 1. 기존 ticker_map.json 로드 (누적 업데이트)
    existing = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            existing = json.load(open(OUTPUT_FILE, encoding="utf-8"))
            print(f"  기존 매핑 로드: {len(existing)}개")
        except:
            pass

    # 2. CSV에서 티커 추출
    tickers_bal = load_tickers_from_csv(EXPOSURE_FILE)
    tickers_all = sorted(tickers_bal.keys(), key=lambda t: -tickers_bal[t])
    print(f"  CSV 티커 추출: {len(tickers_all)}개")

    # 3. 미등록 티커만 yfinance 조회
    missing = [t for t in tickers_all
               if t not in existing and t not in FALLBACK_MAP]
    print(f"  신규 조회 필요: {len(missing)}개")

    yf_result = {}
    if missing:
        print(f"  yfinance 조회 중...")
        yf_result = fetch_names_yfinance(missing)
        print(f"  yfinance 성공: {len(yf_result)}개")

    # 4. 최종 매핑 통합 (우선순위: 기존 > yfinance > fallback)
    final_map = dict(FALLBACK_MAP)   # fallback 베이스
    final_map.update(yf_result)       # yfinance 덮어쓰기
    final_map.update(existing)        # 기존 매핑 최우선

    # 5. CSV에 있는 티커만 남기고 잔고 기준 정렬
    output = {t: final_map[t] for t in tickers_all if t in final_map}
    # yfinance·fallback 모두 없는 티커 → 티커 그대로 보관
    for t in tickers_all:
        if t not in output:
            output[t] = t  # 미매핑 → 티커 그대로

    # 잔고 기준 정렬
    output_sorted = dict(sorted(output.items(), key=lambda x: -tickers_bal.get(x[0], 0)))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output_sorted, f, ensure_ascii=False, indent=2)

    mapped = sum(1 for v in output_sorted.values() if v and not TICKER_RE.match(v))
    print(f"  저장 완료: {len(output_sorted)}개 (한글명 매핑: {mapped}개)")
    print(f"[ticker_mapper] 완료 → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
