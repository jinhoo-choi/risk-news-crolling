# -*- coding: utf-8 -*-
"""exposure_data.csv 변환 스크립트 (20컬럼 뱅/영 채널 스키마)

사용법:
  python3 convert_exposure.py <통합.xlsx>                          # 이미 20컬럼(뱅/영)으로 병합된 단일 파일 — 형식 검증 후 그대로 변환
  python3 convert_exposure.py <뱅키스.xlsx> <영업점.xlsx>          # 12컬럼 채널 2개 → left-join 병합
  python3 convert_exposure.py <뱅키스.xlsx>                        # 뱅키스 단독 12컬럼 (영업점 컬럼 공란)

입력 자동 판별:
  - 컬럼이 20컬럼 스키마(뱅*/영* 8항목씩)와 일치 → 단일 파일 경로(이미 병합됨, 그대로 정제)
  - 컬럼이 12컬럼 SRC_COLS와 일치 → 기존 조인 경로(뱅키스[+영업점] 별도 파일)

12컬럼 입력 엑셀(두 채널 동일 구조, 시트 'Data'):
  기준일, 종목명, 종목코드, 종목유형, 잔고(억), 고객수, 리스크종목,
  리스크고객수, 리스크잔고(억), 최고리스크잔고, 최고리스크고객, 유지담보비율

출력(20컬럼, utf-8-sig, LF):
  기준일, 종목명, 종목코드, 종목유형,
  뱅잔고, 뱅고객수, 뱅리스크종목, 뱅리스크고객수, 뱅리스크잔고,
  뱅최고리스크잔고, 뱅최고리스크고객, 뱅유지담보비율,
  영잔고, 영고객수, 영리스크종목, 영리스크고객수, 영리스크잔고,
  영최고리스크잔고, 영최고리스크고객, 영유지담보비율

변환 규칙:
  - 조인 키(12컬럼 2파일 입력시): (종목코드, 종목유형) — 뱅키스 기준 left-join,
    뱅키스에 없는 영업점 전용 종목은 뒤에 append (뱅 컬럼 공란)
  - 유지담보비율: ×100 반올림 2자리(:g), 결측 공란
  - 종목코드: 순수 숫자 6자리 미만만 zero-pad (해외 알파벳 티커 보존)
  - 빈 종목명 행 스킵
"""
import sys
import pandas as pd

SRC_COLS = ["기준일", "종목명", "종목코드", "종목유형", "잔고(억)", "고객수",
            "리스크종목", "리스크고객수", "리스크잔고(억)", "최고리스크잔고",
            "최고리스크고객", "유지담보비율"]
KEY = ["종목코드", "종목유형"]
CHANNEL_ITEMS = ["잔고", "고객수", "리스크종목", "리스크고객수", "리스크잔고",
                 "최고리스크잔고", "최고리스크고객", "유지담보비율"]
OUT_COLS = (["기준일", "종목명", "종목코드", "종목유형"]
            + [f"뱅{c}" for c in CHANNEL_ITEMS]
            + [f"영{c}" for c in CHANNEL_ITEMS])
DST = "exposure_data.csv"


def _pad_code(c):
    c = str(c).strip()
    return c.zfill(6) if c.isdigit() and len(c) < 6 else c


def _fmt_ratio(v):
    if pd.isna(v):
        return ""
    return f"{round(float(v) * 100, 2):g}"


def _fmt_int(v):
    return "" if pd.isna(v) else str(int(float(v)))


def _fmt_str(v):
    return "" if pd.isna(v) else str(v).strip()


def load_merged(path: str) -> pd.DataFrame:
    """이미 20컬럼(뱅/영 채널)으로 병합돼 전달된 단일 파일 — 형식만 정제"""
    df = pd.read_excel(path, sheet_name="Data")
    assert list(df.columns) == OUT_COLS, f"{path} 컬럼 불일치: {list(df.columns)}"
    df = df[df["종목명"].notna() & (df["종목명"].astype(str).str.strip() != "")].copy()
    df["종목코드"] = df["종목코드"].apply(_pad_code)
    out = df[["기준일", "종목명", "종목코드", "종목유형"]].copy()
    for pre in ("뱅", "영"):
        for item in CHANNEL_ITEMS:
            col = f"{pre}{item}"
            if item == "유지담보비율":
                out[col] = df[col].apply(_fmt_ratio)
            elif item in ("리스크종목", "최고리스크고객"):
                out[col] = df[col].apply(_fmt_str)
            else:
                out[col] = df[col].apply(_fmt_int)
    return out


def load_channel(path: str, prefix: str) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="Data")
    assert list(df.columns) == SRC_COLS, f"{path} 컬럼 불일치: {list(df.columns)}"
    df = df[df["종목명"].notna() & (df["종목명"].astype(str).str.strip() != "")].copy()
    df["종목코드"] = df["종목코드"].apply(_pad_code)
    out = df[["기준일", "종목명", "종목코드", "종목유형"]].copy()
    out[f"{prefix}잔고"]           = df["잔고(억)"].apply(_fmt_int)
    out[f"{prefix}고객수"]         = df["고객수"].apply(_fmt_int)
    out[f"{prefix}리스크종목"]     = df["리스크종목"].apply(_fmt_str)
    out[f"{prefix}리스크고객수"]   = df["리스크고객수"].apply(_fmt_int)
    out[f"{prefix}리스크잔고"]     = df["리스크잔고(억)"].apply(_fmt_int)
    out[f"{prefix}최고리스크잔고"] = df["최고리스크잔고"].apply(_fmt_int)
    out[f"{prefix}최고리스크고객"] = df["최고리스크고객"].apply(_fmt_str)
    out[f"{prefix}유지담보비율"]   = df["유지담보비율"].apply(_fmt_ratio)
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    # 입력 컬럼으로 형식 자동 판별
    _probe = pd.read_excel(sys.argv[1], sheet_name="Data", nrows=0)
    _cols = list(_probe.columns)

    if _cols == OUT_COLS:
        # 이미 20컬럼으로 병합된 단일 파일 — 실제 데이터 전달 방식과 일치(가장 흔한 경로)
        if len(sys.argv) >= 3:
            print("[경고] 20컬럼 병합 파일은 단일 인자만 받습니다 — 두번째 인자는 무시됩니다.")
        merged = load_merged(sys.argv[1])
    else:
        bank = load_channel(sys.argv[1], "뱅")
        if len(sys.argv) >= 3:
            br = load_channel(sys.argv[2], "영")
            merged = bank.merge(br.drop(columns=["기준일", "종목명"]), on=KEY, how="left")
            # 영업점 전용 종목 append (뱅 컬럼 공란)
            only_br = br[~br.set_index(KEY).index.isin(bank.set_index(KEY).index)].copy()
            merged = pd.concat([merged, only_br], ignore_index=True)
        else:
            merged = bank

    for c in OUT_COLS:
        if c not in merged.columns:
            merged[c] = ""
    merged = merged[OUT_COLS].fillna("")
    merged.to_csv(DST, index=False, encoding="utf-8-sig", lineterminator="\n")

    # 검증 리포트
    print(f"변환 완료: {len(merged)}행 → {DST}")
    print("종목유형별:", merged["종목유형"].value_counts().to_dict())
    print("기준일:", sorted(merged["기준일"].unique().tolist()))
    for pre in ("뱅", "영"):
        ratio = pd.to_numeric(merged[f"{pre}유지담보비율"].replace("", pd.NA), errors="coerce").dropna()
        if len(ratio):
            print(f"{pre}담보비율 범위: {ratio.min()} ~ {ratio.max()} ({len(ratio)}건)")
    dom = merged[merged["종목유형"].isin(["여신", "주식", "채권"])]
    bad = dom[dom["종목코드"].astype(str).str.len() != 6]
    print("국내 종목코드 6자리 아닌 행:", len(bad))
    dup = merged.duplicated(subset=KEY).sum()
    print("중복(종목코드+유형):", dup)


if __name__ == "__main__":
    main()
