"""Offline price/market regressions using synthetic records and histories only.

Run: python test_quote_prices.py
No repository exposure/customer files, external requests, SMTP, or paid APIs are
used. Network and mail entry points are blocked before importing the monitor.
"""
import builtins
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import importlib.util
from io import BytesIO, StringIO
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance

import quote_prices as prices

NOW = datetime(2026, 10, 7, 15, 30, tzinfo=prices.KST)
CATALOG = {
    "005930": ".KS", "196170": ".KQ", "0126Z0": ".KS",
    "0001A0": ".KQ", "0177R0": ".KS", "580044": ".KS",
}
_SYNTHETIC_ENV = {
    "EMAIL_SENDER": "offline-sender@example.invalid",
    "EMAIL_PASSWORD": "offline-placeholder",
    "EMAIL_RECEIVER": "offline-recipient@example.invalid",
    "ANTHROPIC_API_KEY": "offline-placeholder",
    "NAVER_CLIENT_ID": "offline-placeholder",
    "NAVER_CLIENT_SECRET": "offline-placeholder",
}


def master_record(code, market="kospi", *, isin="KR7005930003", group="ST"):
    """Minimal fixed-width record: 9-byte code, 12-byte ISIN, 40-byte name."""
    size = {"kospi": 288, "kosdaq": 282}[market]
    record = bytearray(b" " * size)
    record[:9] = code.ljust(9).encode("ascii")
    record[9:21] = isin.encode("ascii")
    record[21:61] = b"SYNTHETIC FIXTURE".ljust(40)
    record[61:63] = group.encode("ascii")
    return bytes(record)


def master_zip(market, records, *, filename=None, extras=None):
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, content in (extras or {}).items():
            archive.writestr(name, content)
        archive.writestr(filename or f"{market}_code.mst", b"\n".join(records))
    return output.getvalue()


def http_response(payload, *, modified=None, status=200, headers=True):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.headers = ({"Last-Modified": modified or format_datetime(
        (NOW - timedelta(days=1)).astimezone(timezone.utc), usegmt=True)} if headers else {})
    response.iter_content.return_value = [payload]
    response.raise_for_status.return_value = None
    if status != 200:
        error_response = requests.Response()
        error_response.status_code = status
        response.raise_for_status.side_effect = requests.HTTPError(response=error_response)
    return response


def history(previous=100, current=90, *, stamp=None):
    last = pd.Timestamp(stamp or NOW)
    return pd.DataFrame({"Close": [previous, current]},
                        index=pd.DatetimeIndex([last - pd.Timedelta(days=1), last]))


def exposure_row(name="시험가", code="005930", *, kind="여신", risk=1,
                 balance=10, risk_balance=3, market="국내", flag="Y"):
    return {"종목명": name, "종목코드": code, "종목유형": kind,
            "시장": market, "리스크종목": flag, "잔고(억)": balance,
            "고객수": 2, "리스크고객수": risk, "리스크잔고(억)": risk_balance,
            "기준일": "2026-10-07"}


def exposure(*rows):
    result = {}
    for row in rows:
        result.setdefault(row["종목명"], []).append(row)
    return result


def blocked_io():
    """Return scoped guards; callers assert they were never invoked."""
    stack = ExitStack()
    guards = []
    real_open = builtins.open
    sensitive_files = {"exposure_data.csv", "seen_news.json", "run_stats.jsonl",
                       "group_map.json", "ticker_map.json", "dart_corp_codes.json",
                       "dart_relation_cache.json", "known_cases.json"}
    forbidden_read = Mock(side_effect=AssertionError("Repository data read in synthetic test"))
    def guarded_open(file, *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)) and os.path.basename(os.fsdecode(file)) in sensitive_files:
            return forbidden_read(file, *args, **kwargs)
        return real_open(file, *args, **kwargs)
    stack.enter_context(patch("builtins.open", side_effect=guarded_open))
    guards.append(forbidden_read)
    for target in ("requests.sessions.Session.request", "requests.post",
                   "smtplib.SMTP", "smtplib.SMTP_SSL", "socket.create_connection",
                   "socket.socket.connect", "yfinance.Ticker"):
        guard = stack.enter_context(patch(target, side_effect=AssertionError(
            f"Unexpected external I/O in offline test: {target}")))
        guards.append(guard)
    return stack, guards


monitor = None


def setUpModule():
    global monitor
    stack, guards = blocked_io()
    original_exists = os.path.exists
    hidden = {"group_map.json", "ticker_map.json"}
    # Import a private module instance, so synthetic environment/maps cannot
    # replace the module used by other suites under unittest discovery.
    path = Path(__file__).with_name("naver_news_monitor.py")
    spec = importlib.util.spec_from_file_location("_offline_quote_monitor", path)
    monitor = importlib.util.module_from_spec(spec)
    with stack, patch.dict(os.environ, _SYNTHETIC_ENV), patch(
        "os.path.exists", side_effect=lambda p: False if os.path.basename(p) in hidden
        else original_exists(p)
    ), redirect_stdout(StringIO()):
        spec.loader.exec_module(monitor)
    for guard in guards:
        guard.assert_not_called()


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self.io_stack, self.io_guards = blocked_io()
        self.addCleanup(self.io_stack.close)
        self.clock = self.io_stack.enter_context(patch.object(prices, "datetime", wraps=datetime))
        self.clock.now.return_value = NOW
        prices.get_market_catalog.cache_clear()
        monitor.clear_price_alert_cache()
        self.output = StringIO()
        self.io_stack.enter_context(redirect_stdout(self.output))
        # Fail even when production catches a blocked network error internally.
        self.addCleanup(self.assert_no_external_io)
        self.addCleanup(prices.get_market_catalog.cache_clear)
        self.addCleanup(monitor.clear_price_alert_cache)

    def assert_no_external_io(self):
        for guard in self.io_guards:
            guard.assert_not_called()

    def assert_unknown(self, quote, status):
        self.assertEqual(quote.status, status)
        self.assertIsNone(quote.change)
        self.assertIsNone(quote.current)


class MasterParserTests(OfflineTestCase):
    def test_fixed_width_codes_and_isins_preserve_alphanumeric_codes(self):
        for market, codes, suffix in [
            ("kospi", ["005930", "0126Z0", "0177R0"], ".KS"),
            ("kosdaq", ["196170", "0001A0"], ".KQ"),
        ]:
            with self.subTest(market=market):
                payload = master_zip(market, [master_record(code, market) for code in codes])
                self.assertEqual(prices.parse_master(payload, market),
                                 {code: suffix for code in codes})

    def test_expected_member_only_and_no_path_extraction(self):
        payload = master_zip("kospi", [master_record("005930")], extras={
            "../unexpected.txt": b"not a master", "other.mst": b"malformed"})
        self.assertEqual(prices.parse_master(payload, "kospi"), {"005930": ".KS"})
        with self.assertRaises(KeyError):
            prices.parse_master(master_zip("kospi", [master_record("005930")],
                                           filename="wrong.mst"), "kospi")

    def test_truncated_records_and_wrong_prefixes_rejected(self):
        bad = [master_record("005930")[:287], b"005930".ljust(300),
               b"\xff" + master_record("005930")[1:],
               master_record("005930", isin="!NVALID00000"),
               master_record("bad!00")]
        for record in bad:
            with self.subTest(record=record[:21]):
                with self.assertRaises((ValueError, UnicodeDecodeError)):
                    prices.parse_master(master_zip("kospi", [record]), "kospi")
        with self.assertRaises(ValueError):
            prices.parse_master(master_zip("kosdaq", [master_record("196170", "kosdaq")[:281]]),
                                "kosdaq")

    def test_foreign_dr_isin_prefix_is_valid(self):
        payload = master_zip("kosdaq", [master_record("196170", "kosdaq", isin="US1234567890")])
        self.assertEqual(prices.parse_master(payload, "kosdaq"), {"196170": ".KQ"})

    def test_verified_q_etn_group_maps_six_digit_code(self):
        payload = master_zip("kospi", [master_record("Q580044", group="EN")])
        catalog = prices.parse_master(payload, "kospi")
        self.assertEqual(catalog, {"580044": ".KS"})
        with patch.object(prices, "get_market_catalog", return_value=catalog):
            self.assertEqual(prices.resolve_symbol([exposure_row(code="580044")]),
                             prices.Symbol("580044.KS"))

    def test_other_q_groups_and_long_fund_right_codes_are_not_truncated(self):
        payload = master_zip("kospi", [master_record("005930"),
            master_record("Q580044", group="ST"), master_record("F12345678"),
            master_record("J12345678")])
        self.assertEqual(prices.parse_master(payload, "kospi"), {"005930": ".KS"})

    def test_duplicate_and_empty_masters_rejected(self):
        for records in ([], [master_record("005930")] * 2,
                        [master_record("Q580044", group="EN"), master_record("580044")],
                        [master_record("F12345678")]):
            with self.subTest(count=len(records)):
                with self.assertRaises(ValueError):
                    prices.parse_master(master_zip("kospi", records), "kospi")
        with self.assertRaises(BadZipFile):
            prices.parse_master(b"not a zip", "kospi")

    def test_uncompressed_member_limit_checked(self):
        with patch.object(prices, "MAX_MASTER_BYTES", 287), self.assertRaises(ValueError):
            prices.parse_master(master_zip("kospi", [master_record("005930")]), "kospi")


class MarketCatalogTests(OfflineTestCase):
    def responses(self):
        return [http_response(master_zip("kospi", [master_record("005930")])),
                http_response(master_zip("kosdaq", [master_record("196170", "kosdaq")]))]

    def test_both_markets_fresh_bounded_and_cached(self):
        responses = self.responses()
        with patch.object(prices.requests, "get", side_effect=responses) as get:
            self.assertEqual(prices.get_market_catalog(), {"005930": ".KS", "196170": ".KQ"})
            self.assertEqual(prices.get_market_catalog(), {"005930": ".KS", "196170": ".KQ"})
        self.assertEqual(get.call_args_list, [
            call(prices.MASTER_BASE + "kospi_code.mst.zip", timeout=(5, 15), stream=True),
            call(prices.MASTER_BASE + "kosdaq_code.mst.zip", timeout=(5, 15), stream=True)])
        for response in responses:
            response.raise_for_status.assert_called_once_with()
            response.iter_content.assert_called_once_with(65536)
            response.__exit__.assert_called_once()

    def test_stale_missing_future_invalid_or_naive_freshness_fails_closed(self):
        cases = [format_datetime(NOW - timedelta(days=8)),
                 format_datetime(NOW + timedelta(seconds=1)),
                 "not an HTTP date", "Wed, 07 Oct 2026 06:30:00", None]
        for modified in cases:
            with self.subTest(modified=modified):
                prices.get_market_catalog.cache_clear()
                response = http_response(master_zip("kospi", [master_record("005930")]),
                                         modified=modified, headers=modified is not None)
                with patch.object(prices.requests, "get", return_value=response) as get:
                    self.assertIsNone(prices.get_market_catalog())
                    self.assertIsNone(prices.get_market_catalog())
                get.assert_called_once()
                response.iter_content.assert_not_called()

    def test_freshness_age_boundary_is_inclusive(self):
        responses = self.responses()
        for response in responses:
            response.headers["Last-Modified"] = format_datetime(NOW - prices.MASTER_MAX_AGE)
        with patch.object(prices.requests, "get", side_effect=responses):
            self.assertEqual(prices.get_market_catalog(), {"005930": ".KS", "196170": ".KQ"})

    def test_partial_second_market_failure_discards_entire_catalog_and_caches(self):
        with patch.object(prices.requests, "get", side_effect=[self.responses()[0],
                requests.Timeout("synthetic timeout")]) as get:
            self.assertIsNone(prices.get_market_catalog())
            self.assertIsNone(prices.get_market_catalog())
        self.assertEqual(get.call_count, 2)

    def test_exchange_conflict_discards_entire_catalog(self):
        responses = [self.responses()[0], http_response(master_zip("kosdaq", [
            master_record("005930", "kosdaq")]))]
        with patch.object(prices.requests, "get", side_effect=responses) as get:
            self.assertIsNone(prices.get_market_catalog())
        self.assertEqual(get.call_count, 2)

    def test_http_errors_bad_archives_and_download_limits_do_not_retry(self):
        for response in (http_response(b"irrelevant", status=429),
                         http_response(b"irrelevant", status=503),
                         http_response(b"not a zip")):
            prices.get_market_catalog.cache_clear()
            with patch.object(prices.requests, "get", return_value=response) as get:
                self.assertIsNone(prices.get_market_catalog())
                self.assertIsNone(prices.get_market_catalog())
            get.assert_called_once()
        prices.get_market_catalog.cache_clear()
        response = http_response(b"123456789")
        with patch.object(prices, "MAX_MASTER_BYTES", 8), \
             patch.object(prices.requests, "get", return_value=response) as get:
            self.assertIsNone(prices.get_market_catalog())
        get.assert_called_once()


class SymbolResolutionTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.catalog = self.io_stack.enter_context(patch.object(
            prices, "get_market_catalog", return_value=CATALOG))

    def test_exact_kospi_kosdaq_and_mixed_symbols(self):
        for code, suffix in CATALOG.items():
            with self.subTest(code=code):
                self.assertEqual(prices.resolve_symbol([exposure_row(code=code)]),
                                 prices.Symbol(code + suffix))
        self.assertEqual(prices.resolve_symbol([exposure_row(code=5930)]), prices.Symbol("005930.KS"))
        self.assertEqual(prices.resolve_symbol([exposure_row(code="0126z0")]), prices.Symbol("0126Z0.KS"))

    def test_foreign_class_share_symbols_normalized_without_domestic_lookup(self):
        for code in ("BRK/B", "BRK.B", "BRK-B"):
            self.assertEqual(prices.resolve_symbol([exposure_row(code=code, kind="해외주식")]),
                             prices.Symbol("BRK-B"))
        self.assertEqual(prices.resolve_symbol([exposure_row(code="BRK/B", market="해외")]),
                         prices.Symbol("BRK-B"))
        self.catalog.assert_not_called()

    def test_foreign_names_use_only_valid_name_or_verified_fallback(self):
        self.assertEqual(prices.resolve_symbol([
            exposure_row(name="가상한글", code="", kind="해외대출")], "AAPL"), prices.Symbol("AAPL"))
        self.assertEqual(prices.resolve_symbol([
            exposure_row(name="BRK/B", code="invalid code", kind="해외담보")]), prices.Symbol("BRK-B"))
        self.assertEqual(prices.resolve_symbol([
            exposure_row(name="가상한글", code="invalid code", kind="해외주식")]),
            prices.Symbol(status="unsupported"))
        self.assertEqual(prices.resolve_symbol([
            exposure_row(name="BRK/B", code="AAPL", kind="해외주식")], "MSFT"), prices.Symbol("AAPL"))
        self.catalog.assert_not_called()

    def test_equivalent_foreign_class_share_notations_are_not_ambiguous(self):
        rows = [exposure_row(code=code, kind="해외주식") for code in ("BRK/B", "BRK.B", "BRK-B")]
        self.assertEqual(prices.resolve_symbol(rows), prices.Symbol("BRK-B"))
        self.catalog.assert_not_called()

    def test_missing_domestic_listing_never_guesses_suffix_or_foreign_fallback(self):
        self.assertEqual(prices.resolve_symbol([exposure_row(code="285240")], "AAPL"),
                         prices.Symbol(status="not_listed"))
        self.assertEqual(prices.resolve_symbol([exposure_row(code="BRK/B")], "BRK/B"),
                         prices.Symbol(status="unsupported"))

    def test_bonds_and_invalid_codes_never_become_tickers(self):
        for row in [exposure_row(code="005930", kind="채권"),
                    exposure_row(code="KR7005930003", kind="채권"),
                    *[exposure_row(code=code) for code in
                      ("", None, "0059300", "F12345678", "005930.KS", "5930.0", "../AAPL")]]:
            with self.subTest(row=row):
                self.assertEqual(prices.resolve_symbol([row], "AAPL"), prices.Symbol(status="unsupported"))
        self.catalog.assert_not_called()

    def test_ambiguous_rows_do_not_select_first_code(self):
        for rows in ([exposure_row(code="005930"), exposure_row(code="196170")],
                     [exposure_row(code="005930"), exposure_row(code="005930", market="해외")],
                     [exposure_row(code="005930"), exposure_row(code="")]):
            self.assertEqual(prices.resolve_symbol(rows), prices.Symbol(status="ambiguous"))
        self.catalog.assert_not_called()

    def test_repeated_same_code_and_bond_companion_are_unambiguous(self):
        rows = [exposure_row(), exposure_row(kind="주식"), exposure_row(code="KR7005930003", kind="채권")]
        self.assertEqual(prices.resolve_symbol(rows), prices.Symbol("005930.KS"))

    def test_catalog_unavailable_remains_unknown(self):
        self.catalog.return_value = None
        self.assertEqual(prices.resolve_symbol([exposure_row()]), prices.Symbol(status="market_unavailable"))

    def test_foreign_fallback_only_when_exposure_missing(self):
        self.assertEqual(prices.resolve_symbol([], "BRK/B"), prices.Symbol("BRK-B"))
        self.assertEqual(prices.resolve_symbol([], "invalid symbol"), prices.Symbol(status="unsupported"))
        self.assertEqual(prices.resolve_symbol([], ""), prices.Symbol(status="unsupported"))
        self.assertEqual(prices.resolve_symbol([exposure_row(code="", kind="채권")], "BRK/B"),
                         prices.Symbol(status="unsupported"))


class QuoteValidationTests(OfflineTestCase):
    def fetch(self, frame, *, ticker="196170.KQ", now=NOW):
        factory = Mock(return_value=SimpleNamespace(history=Mock(return_value=frame)))
        quote = prices.fetch_quote(prices.Symbol(ticker), ticker_factory=factory, now=now)
        factory.assert_called_once_with(ticker)
        factory.return_value.history.assert_called_once_with(period="5d", interval="1d", auto_adjust=False)
        return quote

    def test_today_valid_price_and_change(self):
        self.assertEqual(self.fetch(history(100, 90)), prices.Quote("ok", "196170.KQ", -10.0, 90.0))
        self.assertEqual(self.fetch(history(100, 100)).change, 0.0)
        self.assertEqual(self.fetch(history(100, 105)).change, 5.0)

    def test_domestic_bar_converts_other_timezone_to_same_kst_day(self):
        stamp = datetime(2026, 10, 6, 23, 30, tzinfo=timezone(timedelta(hours=-4)))
        self.assertEqual(self.fetch(history(stamp=stamp)).status, "ok")

    def test_us_previous_close_is_fresh_at_korean_report_hours(self):
        frame = history(stamp=pd.Timestamp("2026-10-06", tz="America/New_York"))
        for hour in (7, 14, 21):
            with self.subTest(kst_hour=hour):
                quote = self.fetch(frame, ticker="BRK-B", now=NOW.replace(hour=hour))
                self.assertEqual(quote, prices.Quote("ok", "BRK-B", -10.0, 90.0))

    def test_us_open_boundary_selects_previous_or_current_session(self):
        for minute, session, status in [(29, "2026-10-06", "ok"),
                                        (29, "2026-10-07", "stale_price"),
                                        (30, "2026-10-06", "stale_price"),
                                        (30, "2026-10-07", "ok")]:
            with self.subTest(minute=minute, session=session):
                clock = datetime(2026, 10, 7, 9, minute, tzinfo=ZoneInfo("America/New_York"))
                quote = self.fetch(history(stamp=pd.Timestamp(session, tz="America/New_York")),
                                   ticker="BRK-B", now=clock)
                self.assertEqual(quote.status, status)
                if status != "ok":
                    self.assert_unknown(quote, status)

    def test_us_weekend_and_monday_preopen_use_friday(self):
        friday = history(stamp=pd.Timestamp("2026-10-09", tz="America/New_York"))
        for day, hour, minute in ((10, 8, 0), (10, 12, 0), (11, 12, 0), (12, 9, 29)):
            with self.subTest(day=day, hour=hour):
                clock = datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))
                self.assertEqual(self.fetch(friday, ticker="BRK-B", now=clock).status, "ok")
        clock = datetime(2026, 10, 12, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        self.assert_unknown(self.fetch(friday, ticker="BRK-B", now=clock), "stale_price")

    def test_us_old_sessions_and_unconfirmed_holiday_gap_remain_unknown(self):
        old = history(stamp=pd.Timestamp("2026-10-05", tz="America/New_York"))
        self.assert_unknown(self.fetch(old, ticker="BRK-B", now=NOW), "stale_price")
        friday = history(stamp=pd.Timestamp("2026-09-04", tz="America/New_York"))
        holiday = datetime(2026, 9, 7, 12, tzinfo=ZoneInfo("America/New_York"))
        self.assert_unknown(self.fetch(friday, ticker="BRK-B", now=holiday), "stale_price")

    def test_missing_empty_or_one_bar_history_is_unknown(self):
        for frame in (None, pd.DataFrame(), history().iloc[:1]):
            self.assert_unknown(self.fetch(frame), "no_history")

    def test_stale_and_future_day_bars_are_unknown(self):
        for delta in (-1, 1):
            self.assert_unknown(self.fetch(history(stamp=NOW + timedelta(days=delta))), "stale_price")

    def test_naive_unparseable_and_nat_timestamps_are_unknown(self):
        frames = [history(stamp=NOW.replace(tzinfo=None)),
                  pd.DataFrame({"Close": [100, 90]}, index=["yesterday", "today"]),
                  pd.DataFrame({"Close": [100, 90]}, index=[pd.NaT, pd.NaT])]
        for frame in frames:
            self.assert_unknown(self.fetch(frame), "invalid_timestamp")

    def test_zero_negative_nan_inf_and_nonnumeric_prices_are_unknown(self):
        for value in (0, -1, float("nan"), float("inf"), float("-inf"), "invalid", None):
            for previous, current in ((value, 90), (100, value)):
                with self.subTest(previous=previous, current=current):
                    self.assert_unknown(self.fetch(history(previous, current)), "invalid_price")
        self.assert_unknown(self.fetch(history().rename(columns={"Close": "Open"})), "invalid_price")

    def test_overflowed_change_is_unknown(self):
        self.assert_unknown(self.fetch(history(1e-308, 1e308)), "invalid_price")

    def test_rate_limit_and_provider_errors_are_unknown_without_retries(self):
        response = requests.Response()
        response.status_code = 429
        rate_limit_type = type("YFRateLimitError", (Exception,), {})
        for error, status in [(rate_limit_type("synthetic"), "rate_limited"),
                              (requests.HTTPError(response=response), "rate_limited"),
                              (RuntimeError("synthetic private provider detail"), "provider_error")]:
            factory = Mock(return_value=SimpleNamespace(history=Mock(side_effect=error)))
            quote = prices.fetch_quote(prices.Symbol("005930.KS"), ticker_factory=factory, now=NOW)
            self.assert_unknown(quote, status)
            factory.assert_called_once_with("005930.KS")
            factory.return_value.history.assert_called_once()
            self.assertNotIn("synthetic", repr(quote))

    def test_factory_error_and_missing_dependency_are_unknown(self):
        factory = Mock(side_effect=RuntimeError("synthetic error"))
        self.assert_unknown(prices.fetch_quote(prices.Symbol("005930.KS"),
                            ticker_factory=factory, now=NOW), "provider_error")
        factory.assert_called_once()
        with patch.dict(sys.modules, {"yfinance": None}):
            self.assert_unknown(prices.fetch_quote(prices.Symbol("005930.KS")), "provider_unavailable")

    def test_unresolved_symbols_never_contact_provider(self):
        factory = Mock()
        for status in ("unsupported", "ambiguous", "not_listed", "market_unavailable"):
            self.assert_unknown(prices.fetch_quote(prices.Symbol(status=status), ticker_factory=factory), status)
        factory.assert_not_called()

    def test_both_domestic_suffixes_use_krw_and_foreign_uses_dollars(self):
        for ticker in ("005930.KS", "196170.KQ"):
            self.assertEqual(prices.format_price(12345, ticker), "12,345원")
            self.assertEqual(prices.format_price(999.5, ticker), "999.50원")
        self.assertEqual(prices.format_price(1234.5, "BRK-B"), "$1,234.50")


class MonitorPriceIntegrationTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.io_stack.enter_context(patch.object(prices, "get_market_catalog", return_value=CATALOG))
        self.io_stack.enter_context(patch.object(monitor, "NAME_TO_TICKER", {}))
        self.io_stack.enter_context(patch.object(monitor, "TICKER_MAP_RUNTIME", {}))
        self.clients = {}

    def mock_provider(self, frames):
        def ticker(name):
            frame = frames[name]
            client = SimpleNamespace(history=Mock(side_effect=frame) if isinstance(frame, Exception)
                                     else Mock(return_value=frame))
            self.clients.setdefault(name, []).append(client)
            return client
        return self.io_stack.enter_context(patch.object(yfinance, "Ticker", side_effect=ticker))

    def test_both_consumers_use_shared_resolver_and_quote_fetch(self):
        self.assertIs(monitor.resolve_symbol, prices.resolve_symbol)
        self.assertIs(monitor.fetch_quote, prices.fetch_quote)
        rows = [exposure_row(name=f"시험{chr(0xAC00 + i)}", code=code)
                for i, code in enumerate(CATALOG)]
        rows.append(exposure_row(name="해외시험", code="BRK/B", market="해외"))
        data = exposure(*rows)
        symbols = [code + suffix for code, suffix in CATALOG.items()] + ["BRK-B"]
        frames = {symbol: history(20000, 18000) for symbol in symbols}
        frames["BRK-B"] = history(20000, 18000, stamp=pd.Timestamp("2026-10-06 16:00", tz="America/New_York"))
        provider = self.mock_provider(frames)
        with patch.object(monitor, "resolve_symbol", wraps=prices.resolve_symbol) as resolve, \
             patch.object(monitor, "fetch_quote", wraps=prices.fetch_quote) as fetch:
            for row in rows:
                with self.subTest(code=row["종목코드"]):
                    self.assertEqual(monitor.get_entity_price_drop(row["종목명"], data), -10.0)
            html = monitor.build_price_alert_section(data, "2026-10-07")
        self.assertEqual(resolve.call_count, 2 * len(rows))
        self.assertEqual(fetch.call_count, 2 * len(rows))
        self.assertCountEqual([args.args[0] for args in provider.call_args_list], symbols * 2)
        self.assertIn("18,000원", html)
        self.assertNotIn("$18,000.00", html)  # top three fixture balances are domestic
        self.assertEqual(monitor.build_price_alert_section.last_coverage["priced"], len(rows))

    def test_foreign_name_mapping_and_bare_class_ticker_keep_dedup_coverage(self):
        frame = history(stamp=pd.Timestamp("2026-10-06", tz="America/New_York"))
        provider = self.mock_provider({"AAPL": frame, "BRK-B": frame})
        with patch.object(monitor, "NAME_TO_TICKER", {"가상한글": "AAPL"}):
            data = exposure(exposure_row(name="가상한글", code="", kind="해외대출"))
            self.assertEqual(monitor.get_entity_price_drop("가상한글", data), -10.0)
        self.assertEqual(monitor.get_entity_price_drop("BRK/B", {}), -10.0)
        self.assertEqual(provider.call_args_list, [call("AAPL"), call("BRK-B")])

    def test_coverage_accounts_for_every_target_and_risk_subset(self):
        data = exposure(exposure_row("시험가", "005930", risk_balance=7),
                        exposure_row("시험나", "196170", risk=0),
                        exposure_row("시험다", "285240"),
                        exposure_row("시험라", "invalid"),
                        exposure_row("시험마", "0001A0"),
                        exposure_row("시험바", "0126Z0"),
                        exposure_row("시험바", "0177R0"),
                        exposure_row("제외가", "005930", kind="채권"),
                        exposure_row("제외나", "196170", flag="N"))
        provider = self.mock_provider({"005930.KS": history(100, 90),
                                      "196170.KQ": history(100, 105),
                                      "0001A0.KQ": RuntimeError("synthetic")})
        html = monitor.build_price_alert_section(data)
        coverage = monitor.build_price_alert_section.last_coverage
        self.assertEqual(coverage, {"total": 6, "priced": 2, "missing": 4,
            "risk_total": 5, "risk_priced": 1,
            "failures": {"not_listed": 1, "unsupported": 1, "provider_error": 1, "ambiguous": 1}})
        self.assertEqual(coverage["priced"] + sum(coverage["failures"].values()), coverage["total"])
        self.assertEqual(coverage["missing"], sum(coverage["failures"].values()))
        self.assertIn("당일 가격 확인 2/6종목", html)
        self.assertIn("가격 확인 1/5종목", html)
        self.assertIn("미확인 상태", html)
        for status in coverage["failures"]:
            if status == "not_listed":  # (2026-10-11) 비상장은 상시 상태라 사유에서 제외
                self.assertNotIn(prices.STATUS_LABELS[status], html)
                continue
            self.assertIn(prices.STATUS_LABELS[status], html)
        self.assertEqual(monitor.build_price_alert_section.last_alerted_count, 1)
        self.assertEqual(monitor.build_price_alert_section.last_alerted_rbal, 7)
        self.assertCountEqual([args.args[0] for args in provider.call_args_list],
                              ["005930.KS", "196170.KQ", "0001A0.KQ"])
        self.assertNotIn("시험가", self.output.getvalue())
        self.assertNotIn("synthetic", self.output.getvalue())

    def test_no_decline_still_displays_coverage_without_price_alerts(self):
        provider = self.mock_provider({"196170.KQ": history(100, 105)})
        html = monitor.build_price_alert_section(exposure(exposure_row(code="196170")))
        # (2026-10-11) 조회 장애가 없으면 유의사항 박스를 숨긴다(사용자 결정).
        self.assertNotIn("가격 감시 범위", html)
        self.assertEqual(monitor.build_price_alert_section.last_coverage["priced"], 1)
        self.assertNotIn("여신잔고 리스크 현황", html)
        self.assertEqual(monitor.build_price_alert_section.last_alerted_count, 0)
        self.assertEqual(monitor.build_price_alert_section.last_alerted_rbal, 0)
        provider.assert_called_once_with("196170.KQ")

    def test_all_failures_remain_visible_and_cannot_trigger_market_crash(self):
        provider = self.mock_provider({"005930.KS": RuntimeError("synthetic")})
        data = exposure(exposure_row("시험가", "285240", risk_balance=10000),
                        exposure_row("시험나", "005930", risk_balance=10000))
        html = monitor.build_price_alert_section(data)
        coverage = monitor.build_price_alert_section.last_coverage
        self.assertEqual((coverage["total"], coverage["priced"], coverage["missing"]), (2, 0, 2))
        self.assertEqual((coverage["risk_total"], coverage["risk_priced"]), (2, 0))
        self.assertIn("당일 가격 확인 0/2종목", html)
        self.assertIn("시장급락 판정에 포함되지 않습니다", html)
        self.assertEqual(monitor.build_price_alert_section.last_alerted_count, 0)
        self.assertEqual(monitor.build_price_alert_section.last_alerted_rbal, 0)
        provider.assert_called_once_with("005930.KS")

    def test_weekend_all_stale_plus_one_unlisted_hides_notice(self):
        # (2026-10-11) 주말·휴장 회차: 전 종목 당일 가격 없음 + 비상장 1 → 유의사항 미노출
        self.mock_provider({"005930.KS": history(stamp=NOW - timedelta(days=1))})
        data = exposure(exposure_row("시험가", "005930"), exposure_row("시험나", "285240"))
        html = monitor.build_price_alert_section(data)
        self.assertEqual(monitor.build_price_alert_section.last_coverage["failures"],
                         {"stale_price": 1, "not_listed": 1})
        self.assertNotIn("가격 감시 범위", html)

    def test_decline_without_risk_customers_still_displays_coverage(self):
        self.mock_provider({"005930.KS": history(100, 70)})
        html = monitor.build_price_alert_section(exposure(exposure_row(risk=0)))
        # (2026-10-11) 조회 장애 없으면 유의사항 박스 미노출
        self.assertNotIn("가격 감시 범위", html)
        self.assertEqual(monitor.build_price_alert_section.last_coverage["priced"], 1)
        self.assertNotIn("여신잔고 리스크 현황", html)
        self.assertEqual(monitor.build_price_alert_section.last_alerted_count, 0)
        self.assertEqual(monitor.build_price_alert_section.last_alerted_rbal, 0)

    def test_cache_restores_all_aggregates_without_extra_provider_calls(self):
        provider = self.mock_provider({"196170.KQ": history(20000, 18000)})
        data = exposure(exposure_row(code="196170", risk_balance=7))
        first = monitor.build_price_alert_section(data, "2026-10-07")
        expected_coverage = dict(monitor.build_price_alert_section.last_coverage)
        monitor.build_price_alert_section.last_coverage = {"corrupted": True}
        monitor.build_price_alert_section.last_alerted_count = -1
        monitor.build_price_alert_section.last_alerted_rbal = -1
        self.assertEqual(monitor.build_price_alert_section(data, "2026-10-07"), first)
        self.assertEqual(monitor.build_price_alert_section.last_coverage, expected_coverage)
        self.assertEqual(monitor.build_price_alert_section.last_alerted_count, 1)
        self.assertEqual(monitor.build_price_alert_section.last_alerted_rbal, 7)
        provider.assert_called_once_with("196170.KQ")
        monitor.clear_price_alert_cache()
        self.assertEqual(monitor.build_price_alert_section.last_coverage, {})
        self.assertEqual(monitor.build_price_alert_section.last_alerted_count, 0)
        self.assertEqual(monitor.build_price_alert_section.last_alerted_rbal, 0)
        self.assertEqual(monitor.build_price_alert_section(data, "2026-10-07"), first)
        self.assertEqual(provider.call_count, 2)

    def test_unknown_prices_stay_none_in_dedup_and_missing_in_report(self):
        frames = [(history(stamp=NOW - timedelta(days=1)), "stale_price"),
                  (history(stamp=NOW.replace(tzinfo=None)), "invalid_timestamp"),
                  (history(current=0), "invalid_price"),
                  (pd.DataFrame(), "no_history"),
                  (RuntimeError("synthetic"), "provider_error")]
        rate_limit_type = type("YFRateLimitError", (Exception,), {})
        frames.append((rate_limit_type("synthetic"), "rate_limited"))
        for frame, status in frames:
            with self.subTest(status=status):
                monitor.clear_price_alert_cache()
                data = exposure(exposure_row())
                with patch.object(yfinance, "Ticker", return_value=SimpleNamespace(
                    history=Mock(side_effect=frame) if isinstance(frame, Exception)
                    else Mock(return_value=frame))) as provider:
                    self.assertIsNone(monitor.get_entity_price_drop("시험가", data))
                    html = monitor.build_price_alert_section(data)
                self.assertEqual(provider.call_count, 2)
                self.assertEqual(monitor.build_price_alert_section.last_coverage["failures"], {status: 1})
                if status == "stale_price":
                    # (2026-10-11) 전 종목 당일 가격 없음 = 주말·휴장·개장 전 → 박스 미노출
                    self.assertNotIn("가격 감시 범위", html)
                else:
                    self.assertIn("당일 가격 확인 0/1종목", html)
                self.assertEqual(monitor.build_price_alert_section.last_alerted_count, 0)

    def test_unlisted_bond_ambiguous_and_unavailable_dedup_do_not_fetch(self):
        for rows in ([exposure_row(code="285240")], [exposure_row(kind="채권")],
                     [exposure_row(code="005930"), exposure_row(code="196170")]):
            self.assertIsNone(monitor.get_entity_price_drop("시험가", exposure(*rows)))
        with patch.object(prices, "get_market_catalog", return_value=None):
            self.assertIsNone(monitor.get_entity_price_drop("시험가", exposure(exposure_row())))
            html = monitor.build_price_alert_section(exposure(exposure_row()))
            self.assertIn(prices.STATUS_LABELS["market_unavailable"], html)
            self.assertEqual(monitor.build_price_alert_section.last_coverage["failures"],
                             {"market_unavailable": 1})

    def test_empty_or_ineligible_exposure_has_no_price_targets(self):
        for data in ({}, exposure(exposure_row(kind="채권")), exposure(exposure_row(flag="N"))):
            monitor.clear_price_alert_cache()
            self.assertEqual(monitor.build_price_alert_section(data), "")
            self.assertEqual(monitor.build_price_alert_section.last_coverage, {})
            self.assertEqual(monitor.build_price_alert_section.last_alerted_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
