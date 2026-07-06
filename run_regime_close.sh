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
      echo "  CDP up after $((25 + i*5))s — now waiting for the chart page to render"
      break
    fi
    sleep 5; i=$((i+1))
  done

  if ! curl -sf --max-time 3 "http://localhost:9222/json/version" >/dev/null; then
    notify "ABORT" "CDP never came up after TV relaunch. Manual intervention needed."
    return 1
  fi

  # CDP is up but that only means Electron is talking. TV might still be on
  # the splash screen / welcome / login dialog — the chart page (URL contains
  # tradingview.com/chart/) doesn't exist yet. Fri 7/03 and Mon 7/06 both
  # failed because the scan started while TV was still on a non-chart page.
  # Poll for a chart page for up to 90s before giving up.
  local j=0
  while [ $j -lt 30 ]; do
    if curl -s --max-time 3 "http://localhost:9222/json" 2>/dev/null | grep -q "tradingview.com/chart/"; then
      echo "  chart page rendered after $((j*3))s of post-CDP wait"
      sleep 5  # small buffer for LuxAlgo indicators to compute
      return 0
    fi
    sleep 3; j=$((j+1))
  done

  notify "ABORT" "CDP up but chart page never rendered (TV stuck on non-chart screen)."
  echo "  ABORT: TV came up but is on a non-chart screen — likely needs manual login / dialog dismissal"
  return 1
}

if ! ensure_cdp; then
  echo "ABORT: TradingView CDP not responding on :9222 even after relaunch attempt."
  echo "  Open TradingView Desktop with LuxAlgo Signals & Overlays + LuxAlgo Meta on the active chart, then re-trigger."
  exit 78
fi

# ─── Scan with TV-crash retry loop ───────────────────────────────────────
# TradingView Desktop occasionally crashes after a few thousand symbol
# switches (June 10 + June 12 both died around the 25-min mark). regime_scan
# now writes a checkpoint every 100 tickers and exits with code 87 if TV
# dies. We catch that, restart TV via ensure_cdp, and resume — up to 3 attempts.
PYTHON="$HOME/.homebrew/Caskroom/miniconda/base/bin/python3"
EXIT_TV_CRASHED=87
MAX_ATTEMPTS=3
RESUME_FLAG=""
STATUS_REGIME=1

for attempt in 1 2 3; do
  echo "  [attempt $attempt/$MAX_ATTEMPTS] regime_scan ${RESUME_FLAG}"
  set +e
  caffeinate -is "$PYTHON" regime_scan.py --threshold 3.5 --tfs D,W,M --wait 1.2 --multi-pane $RESUME_FLAG
  STATUS_REGIME=$?
  set -e

  if [ $STATUS_REGIME -eq 0 ]; then
    echo "  scan completed cleanly on attempt $attempt"
    break
  fi

  if [ $STATUS_REGIME -eq $EXIT_TV_CRASHED ] && [ $attempt -lt $MAX_ATTEMPTS ]; then
    notify "TV CRASHED" "attempt $attempt died, restarting TradingView and resuming"
    echo "  TV crashed (exit 87) — restarting TV and resuming on attempt $((attempt+1))"
    # Force a TV restart by killing it, ensure_cdp will relaunch with the flag.
    /usr/bin/osascript -e 'quit app "TradingView"' >/dev/null 2>&1 || true
    sleep 6
    pkill -9 -f "/Applications/TradingView.app/Contents/MacOS/TradingView" 2>/dev/null || true
    sleep 4
    if ! ensure_cdp; then
      echo "  could not bring TV back up — giving up"
      break
    fi
    RESUME_FLAG="--resume"
    continue
  fi

  echo "  scan failed with exit $STATUS_REGIME (not retriable) — giving up"
  break
done

# Classifier freshness gate: only run if regime_state.json was actually
# modified by THIS wrapper invocation. If the scan died at startup and
# regime_state.json is from a prior run, classifying it would silently
# re-push stale numbers to Supabase as if they were today's close (this
# was the June 18 + June 19 failure mode — both pushed June 17's data to
# the dashboard for two days running).
REGIME_FILE="$HOME/tht-data/docs/regime_state.json"
STATUS_CLASSIFY=0
if [ -f "$REGIME_FILE" ]; then
  # File mtime in seconds since epoch, compared to wrapper start.
  REGIME_MTIME=$(stat -f "%m" "$REGIME_FILE")
  # Wrapper start time wasn't captured as epoch — use NOW minus a generous
  # window. If the file was touched in the last 4 hours, count it as fresh.
  NOW=$(date -u +%s)
  AGE=$(( NOW - REGIME_MTIME ))
  if [ $AGE -lt 14400 ]; then
    echo "  regime_state.json fresh (${AGE}s old) → running classifier"
    "$PYTHON" setups_classify.py
    STATUS_CLASSIFY=$?
  else
    echo "  regime_state.json STALE (${AGE}s old, >4h) → SKIPPING classifier to avoid pushing stale data"
    notify "STALE DATA" "regime_state.json is ${AGE}s old. Classifier skipped."
    STATUS_CLASSIFY=2
  fi
else
  echo "  no regime_state.json — classifier skipped"
  STATUS_CLASSIFY=3
fi

# ─── Result ──────────────────────────────────────────────────────────────
echo "regime-close finished $(date -u +%FT%TZ)  regime=${STATUS_REGIME} classify=${STATUS_CLASSIFY}"

if [ $STATUS_REGIME -ne 0 ] || [ $STATUS_CLASSIFY -ne 0 ]; then
  notify "FAILED" "regime=${STATUS_REGIME} classify=${STATUS_CLASSIFY}. Check ${LOG}."
else
  notify "OK" "$(date '+%H:%M') scan + classify complete"
fi

exit $(( STATUS_REGIME == 0 ? STATUS_CLASSIFY : STATUS_REGIME ))
