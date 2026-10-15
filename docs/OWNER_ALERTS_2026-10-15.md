# Executive report / owner diagnostic separation

Executive reports no longer include deferred-review banners/counts, price-fetch coverage warnings or unconfirmed model labels. Verified risk findings, real market/customer risk warnings and price-drop alerts remain intact.

Operational warnings and runtime/SMTP failures use the existing fixed owner constant from `risk_owner_verification.py`; executive receiver/CC and configurable no-result receiver settings are never diagnostic fallbacks. Price diagnostics are flushed after the subscription SMTP receipt guard is restored. Deferred review alerts likewise stay outside delivery receipts. Owner-only empty reports can retain review status; no duplicate review alert is sent after confirmed owner status delivery.

Unverified article bodies hold the run before report delivery/history registration rather than merely removing caveats from title-only analysis. Pending classification review records and existing accepted/uncertain delivery protections remain unchanged. No job retry, live SMTP, paid inference, secret/schedule change, merge or deployment was performed.

Validation: `bash run_tests.sh`, `python check_changes.py`, and each of the eight risk subscription CI test scripts independently. New regressions exercise owner-only SMTP envelopes/headers, warning flush deduplication, mixed verified/deferred report separation, unchanged delivery receipts/history, and unverified-body hold.
