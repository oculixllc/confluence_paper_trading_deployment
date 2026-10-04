#!/bin/bash
# Runs the Test 2 smoke test and, only on PASS, adds the Test 2 cron line. Idempotent and self-removing.
# Scheduled by a one-time cron entry (see RUNBOOK). Result is logged to enable_setups.log and posted to the dashboard.
DIR="${PT_DIR:-/home/ubuntu/paper-trading}"
cd "$DIR" || exit 1
exec >> enable_setups.log 2>&1
echo "=== $(date -u +%FT%TZ) enable_setups_after_smoke ==="

self_remove() { crontab -l | grep -v enable_setups_after_smoke | crontab -; }
report() {
  [ -n "$DASHBOARD_URL" ] && curl -s -m 8 -X POST -H "Content-Type: application/json" -H "Authorization: Bearer $DASHBOARD_TOKEN" \
    -d "{\"event\":\"smoke_test_result\",\"bot_id\":\"setups\",\"reason\":\"$1\"}" "$DASHBOARD_URL/api/event" > /dev/null
}

set -a; source .env; set +a

if crontab -l | grep -q run_setups.sh; then echo "Test 2 cron line already present; nothing to do"; self_remove; exit 0; fi
if [ -e enable_setups.failed ]; then echo "previous attempt FAILED; refusing to retry (remove enable_setups.failed to allow)"; self_remove; exit 1; fi

OANDA_ACCOUNT_ID=101-001-39749670-002 venv/bin/python3 smoke_test_setups.py
rc=$?
echo "smoke test exit code: $rc"
if [ $rc -eq 0 ]; then
  (crontab -l; echo '3,18,33,48 * * * * /home/ubuntu/paper-trading/run_setups.sh >> /home/ubuntu/paper-trading/cron_setups.log 2>&1') | crontab -
  self_remove
  echo "PASS: Test 2 cron line ADDED"
  report "smoke test PASS - Test 2 cron line added"
elif [ $rc -eq 2 ]; then
  echo "INCONCLUSIVE (market closed): will retry at the next scheduled attempt"
else
  touch enable_setups.failed
  self_remove
  echo "FAIL (exit $rc): Test 2 NOT enabled; see output above"
  report "smoke test FAILED (exit $rc) - Test 2 NOT enabled"
fi
