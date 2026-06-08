#!/bin/zsh
# Nightly close run — drives TradingView Desktop via CDP to refresh
# regime_state + setups for the full $3.5B universe (D, W, M), then
# pushes results to Supabase.
#
# Triggered by ~/Library/LaunchAgents/com.kuba.regime-close.plist at
# 21:00 local time, Mon–Fri.
#
# Self-heals when TradingView isn't running or has lost its CDP port
# (the common failure mode: macOS `open --args` ignores Electron args
#  when TV is already running, so a stale TV instance must be quit and
#  the binary launched directly).

set -uo pipefail

LOG_DIR="$HOME/tht-data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/regime-close.log"

exec >> "$LOG" 2>&1
echo
echo "============================================="
echo "regime-close started $(date -u +%FT%TZ) (local $(date '+%F %T %Z'))"

# ─── Notification helper ─────────────────────────────────────────────────
# Shows a desktop notification AND writes a sentinel file so we can spot
# failures from a glance. The sentinel file's mtime tells you when the
# last failure was.
NOTIFY_TITLE="THT Dual Scan"
notify() {
  local subtitle="$1"; local body="$2"
  /usr/bin/osascript -e "display notification \"${body}\" with title \"${NOTIFY_TITLE}\" subtitle \"${subtitle}\"" >/dev/null 2>&1 || true
  echo "  [notify] ${subtitle} — ${body}"
}

# ─── Env ─────────────────────────────────────────────────────────────────
if [ -f "$HOME/tht-data/.env" ]; then
  set -a; source "$HOME/tht-data/.env"; set +a
else
  notify "ABORT" "~/tht-data/.env not found"
  exit 78
fi

cd "$HOME/tht-data" || exit 1

# ─── Ensure TradingView is alive on CDP ──────────────────────────────────
# Three-tier escalation:
#   1. If CDP responds, proceed.
#   2. If not, quit any existing TV and relaunch via the binary directly
#      with the debug flag. Wait up to 90s for port to come up.
#   3. If still down, notify + abort (don't corrupt the data).
ensure_cdp() {
  if curl -sf --max-time 3 "http://localhost:9222/json/version" >/dev/null; then
    echo "  CDP up: proceeding"
    return 0
  fi

  echo "  CDP down — attempting self-heal: quitting TV, relaunching with --remote-debugging-port=9222"
  /usr/bin/osascript -e 'quit app "TradingView"' >/dev/null 2>&1 || true

  # Wait for TV to fully shut down (max 15s)
  local i=0
  while pgrep -f "/Applications/TradingView.app/Contents/MacOS/TradingView$" >/dev/null && [ $i -lt 15 ]; do
    sleep 1; i=$((i+1))
  done

  # Launch the binary directly — `open --args` is unreliable for Electron
  # apps because the App's existing instance may swallow the args.
  nohup /Applications/TradingView.app/Contents/MacOS/TradingView --remote-debugging-port=9222 \
    > /tmp/tv-autorelaunch.log 2>&1 &
  disown
  echo "  TV relaunch PID $!, waiting up to 90s for CDP…"

  # Per feedback: don't hammer CDP during boot. Wait 25s before first probe,
  # then poll every 5s.
  sleep 25
  i=0
  while [ $i -lt 13 ]; do
    if curl -sf --max-time 3 "http://localhost:9222/json/version" >/dev/null; then
      echo "  CDP up after $((25 + i*5))s"
      sleep 8  # let TV's chart finish rendering before the scan iterates
      return 0
    fi
    sleep 5; i=$((i+1))
  done

  notify "ABORT" "CDP never came up after TV relaunch. Manual intervention needed."
  return 1
}

if ! ensure_cdp; then
  echo "ABORT: TradingView CDP not responding on :9222 even after relaunch attempt."
  echo "  Open TradingView Desktop with LuxAlgo Signals & Overlays + LuxAlgo Meta on the active chart, then re-trigger."
  exit 78
fi

# ─── Scan ────────────────────────────────────────────────────────────────
PYTHON="$HOME/.homebrew/Caskroom/miniconda/base/bin/python3"

caffeinate -is "$PYTHON" regime_scan.py --threshold 3.5 --tfs D,W,M --wait 1.2
STATUS_REGIME=$?

# Always run the classifier even if the scan partially failed — it'll work
# off whatever made it into docs/regime_state.json.
"$PYTHON" setups_classify.py
STATUS_CLASSIFY=$?

# ─── Result ──────────────────────────────────────────────────────────────
echo "regime-close finished $(date -u +%FT%TZ)  regime=${STATUS_REGIME} classify=${STATUS_CLASSIFY}"

if [ $STATUS_REGIME -ne 0 ] || [ $STATUS_CLASSIFY -ne 0 ]; then
  notify "FAILED" "regime=${STATUS_REGIME} classify=${STATUS_CLASSIFY}. Check ${LOG}."
else
  notify "OK" "$(date '+%H:%M') scan + classify complete"
fi

exit $(( STATUS_REGIME == 0 ? STATUS_CLASSIFY : STATUS_REGIME ))
