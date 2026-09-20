#!/usr/bin/env bash
# AATM canonical demo sequence.
# Runs the real engine against local mock adapters. No network, no credentials.
set -u

# Pick a working Python interpreter that actually has AATM's deps installed.
# Honors $PYTHON first, then tries common interpreter names (including
# python.exe for Git-Bash / WSL on Windows) and verifies pydantic imports.
has_deps() { "$1" -c "import pydantic, yaml, jsonschema, jinja2" >/dev/null 2>&1; }
pick_python() {
  if [ -n "${PYTHON:-}" ] && command -v "$PYTHON" >/dev/null 2>&1 && has_deps "$PYTHON"; then
    echo "$PYTHON"; return
  fi
  for c in python python3 python.exe py; do
    if command -v "$c" >/dev/null 2>&1 && has_deps "$c"; then echo "$c"; return; fi
  done
  # Fall back to the first interpreter that at least exists.
  for c in python python3 python.exe py; do
    if command -v "$c" >/dev/null 2>&1; then echo "$c"; return; fi
  done
  echo "python"
}
PY="$(pick_python)"
WF="workflows/travel_booking.yaml"
LINE="============================================================"

hr() { echo ""; echo "$LINE"; echo "  $1"; echo "$LINE"; }

# Extract "RUN_ID: <id>" from captured output.
extract_run_id() { echo "$1" | grep -Eo 'RUN_ID: [^ ]+' | awk '{print $2}' | tr -d '\r'; }

hr "AATM DEMO - Agent Action Transaction Manager"
echo "Runtime safety/recovery layer for AI-agent tool calls."
echo "Everything below runs locally against mock adapters."

# 0) Plan
hr "0. PLAN the workflow (classification + pivot detection)"
$PY main.py plan "$WF"

# A) Happy path
hr "A. HAPPY PATH (all 7 steps succeed)"
OUT_A=$($PY main.py run "$WF" --fast --report --no-color)
echo "$OUT_A"
RID_A=$(extract_run_id "$OUT_A")

# B) Failure BEFORE pivot (car unavailable at step 3)
hr "B. FAILURE BEFORE PIVOT (car unavailable -> compensate hotel, flight)"
OUT_B=$($PY main.py run "$WF" --inject injections/step3_failure.yaml --fast --report --no-color)
echo "$OUT_B"
RID_B=$(extract_run_id "$OUT_B")

# C) Failure AFTER pivot (timeout at confirm -> refund + cancellations)
hr "C. FAILURE AFTER PIVOT (forward recovery: refund is a NEW transaction)"
OUT_C=$($PY main.py run "$WF" --inject injections/post_pivot_timeout.yaml --fast --report --no-color)
echo "$OUT_C"
RID_C=$(extract_run_id "$OUT_C")

# D) Crash DURING payment, then recover
hr "D. CRASH DURING PAYMENT (crash after charge dispatched)"
OUT_D=$($PY main.py run "$WF" --inject injections/crash_mid_payment.yaml --fast --no-color)
echo "$OUT_D"
RID_D=$(extract_run_id "$OUT_D")

hr "D2. RESTART + RECONCILE (query by intent_id, no duplicate charge)"
$PY main.py recover --run-id "$RID_D"

# E) Approval denied at the pivot
hr "E. APPROVAL DENIED AT PIVOT (no charge; prior reversible work compensated)"
OUT_E=$($PY main.py run "$WF" --deny-pivot --fast --no-color)
echo "$OUT_E"

# Verify audit chains for the reported runs.
hr "F. TAMPER-EVIDENT AUDIT VERIFICATION"
for rid in "$RID_A" "$RID_B" "$RID_C"; do
  if [ -n "$rid" ]; then
    echo "-- run $rid"
    $PY main.py verify-audit --run-id "$rid"
  fi
done

hr "DEMO COMPLETE"
echo "HTML reports were written to output/reports/ for runs A, B and C."
echo "Open them in a browser to see the reliability/evidence report."
[ -n "$RID_A" ] && echo "  A (happy):        output/reports/$RID_A.report.html"
[ -n "$RID_B" ] && echo "  B (pre-pivot):    output/reports/$RID_B.report.html"
[ -n "$RID_C" ] && echo "  C (post-pivot):   output/reports/$RID_C.report.html"
echo ""
echo "AATM is an engineering prototype: no zero-risk guarantees, no regulatory"
echo "certification, and no true rollback of irreversible actions."
