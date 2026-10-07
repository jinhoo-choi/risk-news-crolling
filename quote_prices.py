"""Source-backed quote symbols and fail-closed daily quote validation.

KIS's public master format: stocks_info/kis_{kospi,kosdaq}_code_mst.py in
https://github.com/koreainvestment/open-trading-api . No account/API key needed.
The master proves exchange membership, not Yahoo coverage or price freshness.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from io import BytesIO
import math
import re
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import requests

KST = timezone(timedelta(hours=9))
MASTER_BASE = "https://new.real.download.dws.co.kr/common/master/"
MASTER_MAX_AGE = timedelta(days=7)  # accommodates weekends/holiday closures
MAX_MASTER_BYTES = 10 * 1024 * 1024

STATUS_LABELS = {
    "market_unavailable": "상장시장 자료 조회불가",
    "not_listed": "국내 마스터 미등재(비상장·미지원 등)",
    "unsupported": "코드·상품 미지원",
    "ambiguous": "복수 코드·시장 충돌",
    "rate_limited": "조회 제한",
    "provider_error": "가격 제공자 오류",
    "provider_unavailable": "가격 조회 모듈 없음",
    "no_history": "가격 이력 부족",
    "stale_price": "당일 가격 없음",
    "invalid_timestamp": "가격 시각 확인불가",
    "invalid_price": "가격값 오류",
}


@dataclass(frozen=True)
class Symbol:
    ticker: str | None = None
    status: str = "ok"


@dataclass(frozen=True)
class Quote:
    status: str
    ticker: str | None = None
    change: float | None = None
    current: float | None = None


def parse_master(payload: bytes, market: str) -> dict[str, str]:
    """Read just the named member, preserving alphanumeric short codes.

    Prefix fields are ASCII fixed-width: short code 9 bytes, ISIN 12 bytes.
    Never extract paths, execute downloaded code, or truncate 9-digit codes.
    """
    filename = f"{market}_code.mst"
    with ZipFile(BytesIO(payload)) as archive:
        member = archive.getinfo(filename)
        if member.file_size > MAX_MASTER_BYTES:
            raise ValueError("oversized master")
        lines = archive.read(member).splitlines()
    if not lines:
        raise ValueError("empty master")
    result = {}
    for line in lines:
        if len(line) < {"kospi": 288, "kosdaq": 282}[market]:
            raise ValueError("invalid master record")
        code = line[:9].decode("ascii").strip()
        isin = line[9:21].decode("ascii")
        if not re.fullmatch(r"[A-Z0-9]{6,9}", code) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
            raise ValueError("invalid master prefix")
        # Official master uses Q + six-digit code specifically for ETNs (EN).
        # Funds/rights use other 9-character codes and must not be truncated.
        if re.fullmatch(r"Q[0-9]{6}", code) and line[61:63] == b"EN":
            code = code[1:]
        if re.fullmatch(r"[A-Z0-9]{6}", code):
            if code in result:
                raise ValueError("duplicate master code")
            result[code] = ".KS" if market == "kospi" else ".KQ"
    if not result:
        raise ValueError("no supported master codes")
    return result


@lru_cache(maxsize=1)
def get_market_catalog() -> dict[str, str] | None:
    """Two bounded public downloads once per process, including failures.

    No stale disk snapshot, guessed suffix, cross-exchange retry, or TLS bypass.
    A partial catalog is not used to declare instruments unlisted.
    """
    catalog = {}
    now = datetime.now(timezone.utc)
    try:
        for market in ("kospi", "kosdaq"):
            with requests.get(MASTER_BASE + f"{market}_code.mst.zip",
                              timeout=(5, 15), stream=True) as response:
                response.raise_for_status()
                modified = parsedate_to_datetime(response.headers["Last-Modified"])
                if modified.tzinfo is None or not timedelta(0) <= now - modified <= MASTER_MAX_AGE:
                    raise ValueError("unverified master freshness")
                payload = bytearray()
                for chunk in response.iter_content(65536):
                    payload.extend(chunk)
                    if len(payload) > MAX_MASTER_BYTES:
                        raise ValueError("oversized download")
            current = parse_master(bytes(payload), market)
            if catalog.keys() & current.keys():
                raise ValueError("conflicting exchanges")
            catalog.update(current)
    except Exception:
        return None
    return catalog


def resolve_symbol(rows: list[dict], fallback_ticker: str = "") -> Symbol:
    """Select only an unambiguous equity/loan code; bonds are never tickers."""
    candidates = set()
    for row in rows:
        kind = row.get("종목유형", "")
        if kind not in ("여신", "주식", "해외주식", "해외대출", "해외담보"):
            continue
        foreign = kind.startswith("해외") or row.get("시장") == "해외"
        code = str(row.get("종목코드") or "").strip().upper()
        if foreign:
            # Prefer the explicit valid code, then a ticker-shaped name, then
            # the caller's known foreign-name mapping (never a domestic guess).
            options = (code, str(row.get("종목명") or "").strip().upper(),
                       fallback_ticker.strip().upper())
            code = next((value for value in options
                         if re.fullmatch(r"[A-Z]{1,5}([./-][A-Z])?", value)), code)
            code = code.replace("/", "-").replace(".", "-")
        if not foreign and re.fullmatch(r"[0-9]{1,6}", code):
            code = code.zfill(6)
        candidates.add((foreign, code))
    if not candidates:
        # Only a known foreign mapping may fill a missing exposure. Never use it
        # to reinterpret a present domestic/bond row as an overseas instrument.
        if rows or not fallback_ticker:
            return Symbol(status="unsupported")
        candidates.add((True, fallback_ticker.strip().upper()))
    if len(candidates) != 1:
        return Symbol(status="ambiguous")
    foreign, code = next(iter(candidates))
    if foreign:
        if not re.fullmatch(r"[A-Z]{1,5}([./-][A-Z])?", code):
            return Symbol(status="unsupported")
        return Symbol(code.replace("/", "-").replace(".", "-"))
    if not re.fullmatch(r"[A-Z0-9]{6}", code):
        return Symbol(status="unsupported")
    catalog = get_market_catalog()
    if catalog is None:
        return Symbol(status="market_unavailable")
    suffix = catalog.get(code)
    return Symbol(code + suffix) if suffix else Symbol(status="not_listed")


def fetch_quote(symbol: Symbol, *, ticker_factory=None, now=None) -> Quote:
    """Missing/invalid data stays unknown; daily bars use their market timezone.

    Korean bars retain the existing same-KST-day policy. US daily bars use
    New York session labels, not the KST calendar date of a midnight bar.
    Before the US open/weekends, the previous weekday is expected. Holidays
    fail closed if this conservative rule cannot confirm a current session.
    """
    if symbol.status != "ok" or not symbol.ticker:
        return Quote(symbol.status)
    if ticker_factory is None:
        try:
            from yfinance import Ticker
            ticker_factory = Ticker
        except ImportError:
            return Quote("provider_unavailable", symbol.ticker)
    try:
        hist = ticker_factory(symbol.ticker).history(period="5d", interval="1d", auto_adjust=False)
    except Exception as exc:
        # Do not expose raw provider errors/URLs or retry a throttled request.
        status = "rate_limited" if (type(exc).__name__ == "YFRateLimitError" or
                                    getattr(getattr(exc, "response", None), "status_code", None) == 429) else "provider_error"
        return Quote(status, symbol.ticker)
    if hist is None or len(hist) < 2:
        return Quote("no_history", symbol.ticker)
    try:
        last = hist.index[-1]
        if last.tzinfo is None:
            return Quote("invalid_timestamp", symbol.ticker)
        clock = now or datetime.now(KST)
        if symbol.ticker.endswith((".KS", ".KQ")):
            expected = clock.astimezone(KST).date()
            last_day = last.tz_convert("Asia/Seoul").date()
        else:
            ny = clock.astimezone(ZoneInfo("America/New_York"))
            expected = ny.date()
            if ny.hour * 60 + ny.minute < 9 * 60 + 30:
                expected -= timedelta(days=1)
            while expected.weekday() >= 5:
                expected -= timedelta(days=1)
            last_day = last.tz_convert("America/New_York").date()
        if last_day != expected:
            return Quote("stale_price", symbol.ticker)
    except Exception:
        return Quote("invalid_timestamp", symbol.ticker)
    try:
        current, previous = float(hist["Close"].iloc[-1]), float(hist["Close"].iloc[-2])
        if not all(math.isfinite(v) and v > 0 for v in (current, previous)):
            raise ValueError("invalid price")
        change = round((current - previous) / previous * 100, 2)
        if not math.isfinite(change):
            raise ValueError("invalid change")
    except Exception:
        return Quote("invalid_price", symbol.ticker)
    return Quote("ok", symbol.ticker, change, current)


def format_price(current: float, ticker: str) -> str:
    if ticker.endswith((".KS", ".KQ")):
        return f"{int(current):,}원" if current >= 1000 else f"{current:.2f}원"
    return f"${current:,.2f}"
