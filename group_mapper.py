"""
group_mapper.py — DART API 기반 그룹 계열사 매핑
exposure_data.csv의 상장사 종목코드 → DART 타법인출자현황·최대주주현황 조회
→ 동일 그룹 클러스터링 → group_map.json 저장

실행: python group_mapper.py
출력: group_map.json  {종목명: [계열사명, ...], ...}
주기: news_monitor.yml의 group-mapper job에서 호출 (기존 cron-job.org 트리거 그대로)
"""

import os, re, json, sys, time, csv, zipfile, io
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import requests
from collections import defaultdict

# ── 설정 ──────────────────────────────────────────────────────────────────────
DART_API_KEY   = os.environ.get("DART_API_KEY", "")
EXPOSURE_FILE  = os.environ.get("EXPOSURE_FILE", "exposure_data.csv")
OUTPUT_FILE    = "group_map.json"
CORP_CODE_FILE = "dart_corp_codes.json"   # corpCode.xml 파싱 결과 캐시

SLEEP_SEC      = 0.15   # API 호출 간 딜레이 (초)
MIN_STAKE_PCT  = 20.0   # 지분율 임계치 — 이 이상이면 계열사로 인정

# ETF·리츠·펀드 등 사업보고서 없는 종목 제외 패턴
ETF_RE = re.compile(
    r'TIGER|KODEX|KINDEX|ARIRANG|ACE|SOL|HANARO|KOSEF|TREX|KBSTAR|'
    r'ETF|리츠|인프라|REIT|스팩|SPAC|기업인수목적'
)
STOCK_CODE_RE = re.compile(r'^\d{6}$')


# ── 1. corpCode.xml 다운로드 → 종목코드 ↔ corp_code 매핑 ─────────────────────
def load_corp_codes() -> dict:
    """종목코드(6자리) → {corp_code, corp_name} 매핑 딕셔너리 반환
    당일 캐시 파일 있으면 재사용 (API 호출 절약)
    """
    # 캐시 확인 (당일 생성 파일이면 재사용)
    if os.path.exists(CORP_CODE_FILE):
        try:
            mtime = os.path.getmtime(CORP_CODE_FILE)
            import time as _t
            if _t.time() - mtime < 86400:  # 24시간 이내
                with open(CORP_CODE_FILE, encoding="utf-8") as f:
                    cached = json.load(f)
                print(f"  corpCode 캐시 로드: {len(cached)}개")
                return cached
        except Exception:
            pass

    if not DART_API_KEY:
        print("  [경고] DART_API_KEY 없음 — corpCode 로드 불가")
        return {}

    try:
        res = requests.get(
            f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={DART_API_KEY}",
            timeout=30
        )
        res.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(res.content))
        xml_data = z.read("CORPCODE.xml")
        root = ET.fromstring(xml_data)

        code_map = {}
        for item in root.findall("list"):
            stock_code = item.findtext("stock_code", "").strip()
            corp_code  = item.findtext("corp_code", "").strip()
            corp_name  = item.findtext("corp_name", "").strip()
            if stock_code and STOCK_CODE_RE.match(stock_code):
                code_map[stock_code] = {"corp_code": corp_code, "corp_name": corp_name}

        # 캐시 저장
        with open(CORP_CODE_FILE, "w", encoding="utf-8") as f:
            json.dump(code_map, f, ensure_ascii=False)

        print(f"  corpCode 다운로드 완료: {len(code_map)}개")
        return code_map

    except Exception as e:
        print(f"  [오류] corpCode 로드 실패: {e}")
        return {}


# ── 2. exposure_data.csv에서 DART 조회 대상 추출 ─────────────────────────────
MAX_DART_TARGETS = int(os.environ.get("DART_MAX_TARGETS", "300"))   # 기본 300, yml에서 9999로 주입 시 전체 조회
MAX_TOTAL_SEC    = int(os.environ.get("DART_MAX_TOTAL_SEC", "300")) # 기본 5분, 주간 yml에서 1500(25분)
def load_target_stocks() -> list:
    """exposure_data.csv → DART 조회 대상 종목 리스트 [(종목명, 종목코드), ...]
    - 6자리 숫자 종목코드만 (채권·해외주식 제외)
    - ETF·리츠·스팩 제외
    - 잔고 합산 기준 상위 MAX_DART_TARGETS개만 (실행 시간 제한)
    """
    if not os.path.exists(EXPOSURE_FILE):
        print(f"  [경고] {EXPOSURE_FILE} 없음")
        return []

    bal_map = {}  # {종목코드: (종목명, 잔고합산)}
    try:
        with open(EXPOSURE_FILE, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                유형 = row.get("종목유형", "")
                if 유형 not in ("주식", "여신"):
                    continue
                name = row.get("종목명", "").strip()
                code = str(row.get("종목코드", "")).strip()
                if not STOCK_CODE_RE.match(code):
                    continue
                if ETF_RE.search(name):
                    continue
                try:
                    bal = float(str(row.get("잔고(억)", "0") or "0").replace(",", ""))
                except ValueError:
                    bal = 0.0
                if code not in bal_map:
                    bal_map[code] = (name, bal)
                else:
                    bal_map[code] = (bal_map[code][0], bal_map[code][1] + bal)
    except Exception as e:
        print(f"  [오류] exposure_data 로드 실패: {e}")

    # 잔고 내림차순 정렬 후 상위 N개
    sorted_targets = sorted(bal_map.items(), key=lambda x: -x[1][1])
    targets = [(v[0], code) for code, v in sorted_targets[:MAX_DART_TARGETS]]

    print(f"  DART 조회 대상: {len(targets)}개 종목 (전체 {len(bal_map)}개 중 잔고 상위)")
    return targets


# ── 3. DART 타법인출자현황 조회 ───────────────────────────────────────────────
CACHE_FILE      = "dart_relation_cache.json"   # 빈 응답(무출자) 기업 캐시
CACHE_EXPIRY_D  = 28                            # 4주 후 재확인 (분기보고서 주기 고려)

def load_relation_cache() -> dict:
    """{corp_code: 'YYYY-MM-DD'(마지막 무출자 확인일)} — 만료분 제외 로드"""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    cutoff = datetime.now() - timedelta(days=CACHE_EXPIRY_D)
    out = {}
    for cc, dstr in raw.items():
        try:
            if datetime.strptime(str(dstr), "%Y-%m-%d") >= cutoff:
                out[cc] = str(dstr)
        except Exception:
            continue
    return out


def save_relation_cache(cache: dict):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"  [경고] 캐시 저장 실패: {e}")


def _latest_bsns_year() -> str:
    """최신 제출완료 사업보고서 연도 — 12월 결산 제출기한(익년 3월말) 반영.
    4월 이후: 전년도 보고서 제출완료 / 1~3월: 전전년도가 최신"""
    now = datetime.now()
    return str(now.year - 1) if now.month >= 4 else str(now.year - 2)


def fetch_investee(corp_code: str, api_key: str) -> list:
    """타법인출자현황 API → [(투자대상법인명, 지분율), ...]
    최신 제출완료 사업보고서 단일 조회 (동적 연도 계산)
    응답 필드명 샘플 로깅으로 실제 구조 파악
    """
    NAME_FIELDS  = ["inv_prm", "inv_nm", "corp_name", "invstmnt_prm", "cmpny_nm"]
    RATIO_FIELDS = ["trmend_blce_qota_rt", "bsis_blce_qota_rt",  # 실제 DART 필드명 (로그 확인)
                    "hold_ratio", "frst_acnt_d", "invstmnt_prm_stcqt", "qota_rt",
                    "stkqy_qota_rt", "pssrp_stock_qota_rt", "pssrp_stcqt"]

    _logged = getattr(fetch_investee, "_logged", False)

    for bsns_year in [_latest_bsns_year()]:
        try:
            res = requests.get(
                "https://opendart.fss.or.kr/api/otrCprInvstmntSttus.json",
                params={
                    "crtfc_key": api_key,
                    "corp_code": corp_code,
                    "bsns_year": bsns_year,
                    "reprt_code": "11011",
                },
                timeout=5
            )
            if res.status_code != 200:
                continue
            data = res.json()
            if data.get("status") != "000":
                continue
            items = data.get("list", [])
            if not items:
                continue
            # 최초 1회 실제 필드명 로깅 (진단 완료 후 제거 가능)
            if not _logged:
                print(f"  [DART 필드 샘플] {list(items[0].keys())}")
                print(f"  [DART 값 샘플] {items[0]}")
                fetch_investee._logged = True
            result = []
            for item in items:
                inv_name = ""
                for f in NAME_FIELDS:
                    v = str(item.get(f, "") or "").strip()
                    if v:
                        inv_name = v
                        break
                if not inv_name:
                    continue
                pct = 0.0
                for f in RATIO_FIELDS:
                    val = item.get(f, "")
                    try:
                        parsed = float(str(val).replace(",", "").replace("%", "").strip())
                        if parsed > 0:
                            pct = parsed
                            break
                    except (ValueError, TypeError):
                        continue
                if pct >= MIN_STAKE_PCT:
                    result.append((inv_name, pct))
            if result:
                return result
        except Exception:
            continue
    return []



# ── 5. 그래프 기반 그룹 클러스터링 ───────────────────────────────────────────
def cluster_groups(
    name_to_code: dict,       # {종목명: 종목코드}
    investee_map: dict,       # {종목명: [(투자대상명, 지분율), ...]}
) -> dict:
    """
    엣지 구성: A가 B에 MIN_STAKE_PCT% 이상 출자 → A-B 엣지
    Union-Find로 connected component → 그룹 클러스터
    (최대주주 엣지는 히트율 0.2%로 제거 — 2026.07)
    """
    # Union-Find
    parent = {name: name for name in name_to_code}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    all_names = set(name_to_code.keys())

    # 법인명 정규화 — (주)/㈜/주식회사 제거 + 영문 그룹표기 → 한글 통일
    _SUFFIX_RE = re.compile(r'[\(（]주[\)）]|㈜|주식회사|\s+')
    _EN_KO_ALIAS = [  # 긴 표기 우선 치환 (S-OIL을 SK보다 먼저 등)
        ("POSCO", "포스코"), ("S-OIL", "에스오일"), ("SOIL", "에스오일"),
        ("HMM", "에이치엠엠"), ("DGB", "디지비"), ("BNK", "비엔케이"),
        ("SPC", "에스피씨"), ("OCI", "오씨아이"), ("SDI", "에스디아이"),
        ("SDS", "에스디에스"), ("SK", "에스케이"),
        ("LG", "엘지"), ("CJ", "씨제이"), ("GS", "지에스"),
        ("DB", "디비"), ("KT", "케이티"), ("HD", "에이치디"),
        ("LS", "엘에스"), ("LX", "엘엑스"), ("DL", "디엘"),
        ("HL", "에이치엘"), ("NH", "엔에이치"), ("KB", "케이비"),
        ("JB", "제이비"), ("JW", "제이더블유"),
    ]

    def _norm(name: str) -> str:
        s = _SUFFIX_RE.sub('', name).strip().upper()
        for en, ko in _EN_KO_ALIAS:
            if en in s:
                s = s.replace(en, ko)
        return s

    # 정규화된 이름 → 원래 이름 매핑
    norm_to_names = {}
    for n in all_names:
        norm = _norm(n)
        if norm not in norm_to_names:
            norm_to_names[norm] = []
        norm_to_names[norm].append(n)

    # 엣지 1: 타법인출자 관계
    for investor, investees in investee_map.items():
        if investor not in all_names:
            continue
        for inv_name, pct in investees:
            inv_norm = _norm(inv_name)
            if len(inv_norm) < 3:
                continue  # 너무 짧은 이름 — 오탐 위험 스킵
            # 1) 정규화 완전일치
            if inv_norm in norm_to_names:
                for target_name in norm_to_names[inv_norm]:
                    union(investor, target_name)
            else:
                # 2) 정규화 후 부분포함 — 더 짧은 쪽이 더 긴 쪽에 완전 포함
                for norm_target, target_names in norm_to_names.items():
                    if len(norm_target) < 3:
                        continue
                    shorter = inv_norm if len(inv_norm) <= len(norm_target) else norm_target
                    longer  = norm_target if len(inv_norm) <= len(norm_target) else inv_norm
                    if shorter in longer and len(shorter) / len(longer) >= 0.8:
                        for target_name in target_names:
                            union(investor, target_name)
                        break


    # 클러스터 추출 — 2개 이상 종목인 그룹만
    clusters = defaultdict(list)
    for name in all_names:
        clusters[find(name)].append(name)

    # group_map: {종목명: [계열사명, ...]} — 자기 자신 포함, 2개 이상 그룹만
    group_map = {}
    for root_name, members in clusters.items():
        if len(members) >= 2:
            for member in members:
                group_map[member] = [m for m in members if m != member]

    return group_map


# ── 6. 메인 ───────────────────────────────────────────────────────────────────
def main():
    import time as _t
    t_start = _t.time()

    print(f"[group_mapper] 시작")

    if not DART_API_KEY:
        print("  [오류] DART_API_KEY 환경변수 없음 — 종료")
        return

    # 1. corpCode 매핑 로드
    print("  [1/4] corpCode 매핑 로드 중...")
    corp_code_map = load_corp_codes()  # {종목코드: {corp_code, corp_name}}
    if not corp_code_map:
        print("  [오류] corpCode 매핑 실패 — 종료")
        return

    # 2. 조회 대상 추출
    print("  [2/4] exposure_data 로드 중...")
    targets = load_target_stocks()  # [(종목명, 종목코드), ...]
    if not targets:
        print("  [오류] 조회 대상 없음 — 종료")
        return

    # corp_code 매핑 — 종목코드로 corp_code 찾기
    name_to_code  = {}  # {종목명: 종목코드}
    code_to_corp  = {}  # {종목코드: corp_code}
    name_to_corp  = {}  # {종목명: corp_code}
    for name, stock_code in targets:
        if stock_code in corp_code_map:
            corp_code = corp_code_map[stock_code]["corp_code"]
            name_to_code[name]  = stock_code
            code_to_corp[stock_code] = corp_code
            name_to_corp[name]  = corp_code

    print(f"  corp_code 매핑 완료: {len(name_to_corp)}개")

    # 3. DART API 호출 — 타법인출자 + 최대주주
    print("  [3/4] DART API 조회 중...")
    investee_map = {}  # {종목명: [(투자대상명, 지분율)]}

    no_rel_cache = load_relation_cache()   # 무출자 기업 스킵 (커서 효과: 기조회분 자동 통과)
    today_str    = datetime.now().strftime("%Y-%m-%d")
    skipped = 0

    total = len(name_to_corp)
    for i, (name, corp_code) in enumerate(name_to_corp.items()):
        # 전체 타임아웃 체크
        if _t.time() - t_start > MAX_TOTAL_SEC:
            print(f"  타임아웃 — {i}/{total}개 처리 후 중단 (캐시 스킵 {skipped}개 별도)")
            break

        if corp_code in no_rel_cache:
            skipped += 1
            continue   # API 미호출 — 28일 내 무출자 확인분

        investees = fetch_investee(corp_code, DART_API_KEY)

        if investees:
            investee_map[name] = investees
        else:
            no_rel_cache[corp_code] = today_str

        time.sleep(SLEEP_SEC)

        if (i + 1) % 100 == 0:
            elapsed = _t.time() - t_start
            print(f"    {i+1}/{total}개 처리 ({elapsed:.0f}초 경과)")

    save_relation_cache(no_rel_cache)
    print(f"  타법인출자 데이터: {len(investee_map)}개 / 캐시 스킵: {skipped}개 / 무출자 캐시: {len(no_rel_cache)}개")

    # 4. 클러스터링 → group_map.json 저장
    print("  [4/4] 그룹 클러스터링 중...")
    group_map = cluster_groups(name_to_code, investee_map)
    print(f"  그룹 클러스터: {len(set(tuple(sorted(v)) for v in group_map.values()))}개 그룹 / {len(group_map)}개 종목")

    # 결과 검증 — 빈 결과면 기존 파일 유지 (폴백)
    existing = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

    if not group_map:
        print("  [경고] group_map 비어있음 — DART API 응답 데이터 확인 필요")
        print(f"    investee_map 건수: {len(investee_map)}")
        if investee_map:
            sample = list(investee_map.items())[:2]
            print(f"    investee_map 샘플: {sample}")
        if existing:
            print(f"  [폴백] 기존 group_map.json 유지 ({len(existing)}개 항목) — 빈 결과로 덮어쓰지 않음")
        else:
            print("  [폴백] 기존 파일 없음 — 빈 결과 저장")
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        # 실패 exit code → yml re-run 트리거용
        sys.exit(1)

    if existing == group_map:
        print(f"  변경 없음 — {OUTPUT_FILE} 저장 스킵")
    else:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(group_map, f, ensure_ascii=False, indent=2)
        print(f"  저장 완료: {OUTPUT_FILE} ({len(group_map)}개 항목)")

    elapsed_total = _t.time() - t_start
    print(f"[group_mapper] 완료 ({elapsed_total:.0f}초)")


if __name__ == "__main__":
    main()
