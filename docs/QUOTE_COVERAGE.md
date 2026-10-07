# Quote symbols and coverage

`quote_prices.py` is shared by the daily loan report and the entity-price dedup
check. Instrument codes are strings, including alphanumeric Korean codes.

## Exchange source

Each process reads the public Korea Investment & Securities master archives:

- https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip
- https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip
- Format references: https://github.com/koreainvestment/open-trading-api/tree/main/stocks_info

These are credential-free static files, not paid quote APIs. No account, secret,
permission or workflow change is required. HTTPS certificate checks stay enabled.
The loader reads only each expected ZIP member, limits download/member size, and
validates fixed-width code/ISIN records. Six-character stock/ETF codes are retained;
only the documented Q-prefixed EN (ETN) records are normalized to six digits.
Other long fund/right codes are unsupported, never blindly truncated.

Both files must validate before any mapping is used. HTTP Last-Modified must be
present, timezone-aware, non-future, and at most seven calendar days old. The
seven-day tolerance accommodates closures; it is not a guarantee that a recent
listing or exchange transfer has already appeared. Success and failure are cached
for the current run/process only; the scheduled monitor starts a new process for
each run. There is no persistent stale mapping fallback or alternate-exchange
quote probing. If either source fails, domestic quotes remain unknown.

The master proves covered exchange membership only. Missing membership is labeled
“not in domestic master (unlisted/unsupported, etc.)”; it does not prove that an
instrument is unlisted everywhere. KONEX, OTC, unsupported products and recent
changes may also be absent. Yahoo quote availability is separately validated.

## Price and reporting policy

- Korean bars retain the existing same-KST-calendar-day policy. Weekends, holidays,
  pre-open runs with yesterday's bar and provider gaps are shown as unknown
- US daily bars use their New York session date. Before 09:30 New York or during a
  weekend the prior weekday is expected; otherwise today's session is expected
- There is no US exchange-holiday calendar in this minimal repair. Holiday cases
  that cannot meet the conservative session rule remain unknown, not healthy
- Missing, non-finite, zero/negative or stale prices and unverified timestamps never
  become zero-percent changes. No date is fabricated and no fast-info fallback is
  used to manufacture a valid dedup price
- Provider failures receive bounded categories. A surfaced 429/rate-limit exception
  is classified separately and is not retried. If yfinance returns an empty frame
  after internally swallowing an error, only “insufficient history” can be proven
- The report shows successful/total monitored instruments, unknown count/reasons,
  and successful/total instruments with risk customers, including zero-alert and
  all-failed runs. Counts sum to the original monitored universe
- Unknown instruments are excluded from numerical drop/crash counts, explicitly
  labeled unverified, and never described as no decline
- Cached report HTML, crash counts and coverage are restored together. No-alert
  coverage retains the existing no-result mail route and recipient policy

Alert thresholds, scoring, article filters, broker-notice handling and recipient
lists are unchanged. Both Korean exchanges use KRW price formatting.

## Verification

Run `bash run_tests.sh` and `python3 check_changes.py`. New tests use synthetic
master ZIPs, bars and loan records; network and mail are mocked. Symbol assertions
include KOSPI, KOSDAQ, alphabetic Korean codes, ETNs, US class tickers, unsupported
codes, quote failures, source freshness and report/cache/no-result behavior.

CI and offline tests establish implementation behavior, not live Yahoo coverage.
After an authorized merge, the next natural scheduled run must confirm source
availability, actual symbol requests, usable-price coverage, error categories,
rendered currency and unchanged mail behavior. No production workflow or mail
send should be manually triggered solely to validate this change.
