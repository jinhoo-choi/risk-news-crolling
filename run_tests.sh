#!/usr/bin/env bash
# 수정 전후 필수 검증 — 하나라도 실패하면 커밋하지 않는다.
# 사용: bash run_tests.sh
set -u
cd "$(dirname "$0")"

FAILED=0
echo "════════════════════════════════════════════════════════════"
echo " 리스크봇 검증 스위트"
echo "════════════════════════════════════════════════════════════"

python3 -m py_compile naver_news_monitor.py manual_send.py convert_exposure.py 2>/dev/null \
  && echo "  OK   컴파일" || { echo "  FAIL 컴파일"; FAILED=1; }

for t in test_variants test_send_decision test_regrade test_scoring test_html test_smoke; do
  if python3 "$t.py" >/tmp/_t.log 2>&1; then
    echo "  OK   $t"
  else
    echo "  FAIL $t"
    tail -12 /tmp/_t.log | sed 's/^/       /'
    FAILED=1
  fi
done

echo "────────────────────────────────────────────────────────────"
python3 test_variants.py 2>/dev/null | grep -E '^\[변형\]|^\[원본\]' | sed 's/^/  /'
echo "════════════════════════════════════════════════════════════"
if [ "$FAILED" -eq 0 ]; then
  echo " ✅ 전체 통과 — 커밋 가능"
else
  echo " ❌ 실패 있음 — 커밋 금지"
fi
exit $FAILED
