#!/bin/zsh
# Nightly close run — drives TradingView Desktop via CDP to refresh
# regime_state + setups for the full $3.5B universe (D, W, M), then
# pushes results to Supabase.
#
# Triggered by ~/Library/LaunchAgents/com.kuba.regime-close.plist at
# 21:00 local time, Mon–Fri.

set -uo pipefail

LOG_DIR="$HOME/tht-data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/regime-close.log"

exec >> "$LOG" 2>&1
echo
echo "============================================="
echo "regime-close started $(date -u +%FT%TZ) (local $(date '+%F %T %Z'))"

# Env (Supabase creds + the macOS CA bundle fix for node, harmless for python).
if [ -f "$HOME/tht-data/.env" ]; then
  set -a; source "$HOME/tht-data/.env"; set +a
else
  echo "ABORT: ~/tht-data/.env not found. Run SUPABASE_SETUP.md step 4."
  exit 78
fi

cd "$HOME/tht-data" || exit 1

# Is TradingView Desktop alive on CDP?
if ! curl -sf --max-time 3 "http://localhost:9222/json/version" >/dev/null; then
  echo "ABORT: TradingView CDP not responding on :9222."
  echo "  Open TradingView Desktop with LuxAlgo Signals & Overlays + LuxAlgo Meta on the active chart, then re-trigger."
  exit 78
fi

# Keep the Mac awake for the duration of the scan (~4 hours).
PYTHON="$HOME/.homebrew/Caskroom/miniconda/base/bin/python3"

caffeinate -is "$PYTHON" regime_scan.py --threshold 3.5 --tfs D,W,M --wait 1.2
STATUS_REGIME=$?

# Always run the classifier even if the scan partially failed — it'll work
# off whatever made it into docs/regime_state.json.
"$PYTHON" setups_classify.py
STATUS_CLASSIFY=$?

echo "regime-close finished $(date -u +%FT%TZ)  regime=${STATUS_REGIME} classify=${STATUS_CLASSIFY}"
exit $(( STATUS_REGIME == 0 ? STATUS_CLASSIFY : STATUS_REGIME ))
