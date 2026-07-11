#!/bin/zsh
# TV keepalive watchdog — keeps TradingView Desktop always running and
# healthy on CDP port 9222 so the nightly regime scan never has to launch
# TV cold (the July 3/6/7 failure mode: fresh TV boots into a white
# non-chart state and the scan aborts with exit 87).
#
# Triggered by ~/Library/LaunchAgents/com.kuba.tv-keepalive.plist every
# 5 minutes + at login.
#
# Health = all four tiers pass:
#   1. TV process alive
#   2. CDP /json/version responds
#   3. /json lists a tradingview.com/chart/ page
#   4. Runtime.evaluate over the chart page's websocket returns a value
#      (catches the zombie state where CDP answers but the chart doesn't)
#
# Never touches TV while a scan is running — the scan wrapper owns TV then.

set -uo pipefail

LOG_DIR="$HOME/tht-data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/tv-keepalive.log"
STATE="$LOG_DIR/tv-keepalive.state"   # holds: <consecutive_failures> <last_relaunch_epoch>

exec >> "$LOG" 2>&1

# Keep the log from growing forever.
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 5000 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

ts() { date '+%F %T'; }
PYTHON="$HOME/.homebrew/Caskroom/miniconda/base/bin/python3"
TV_BIN="/Applications/TradingView.app/Contents/MacOS/TradingView"

notify() {
  local subtitle="$1"; local body="$2"
  /usr/bin/osascript -e "display notification \"${body}\" with title \"TV Keepalive\" subtitle \"${subtitle}\"" >/dev/null 2>&1 || true
  echo "$(ts)  [notify] ${subtitle} — ${body}"
}

read_state() {
  FAILS=0; LAST_RELAUNCH=0
  if [ -f "$STATE" ]; then
    read -r FAILS LAST_RELAUNCH < "$STATE" 2>/dev/null || true
    FAILS=${FAILS:-0}; LAST_RELAUNCH=${LAST_RELAUNCH:-0}
  fi
}
write_state() { echo "$1 $2" > "$STATE"; }

# ─── Skip if a scan owns TV right now ────────────────────────────────────
if pgrep -f "regime_scan.py" >/dev/null 2>&1 || pgrep -f "run_regime_close.sh" >/dev/null 2>&1; then
  echo "$(ts)  scan in progress — skipping (wrapper owns TV)"
  exit 0
fi

# ─── Health check ────────────────────────────────────────────────────────
health() {
  pgrep -f "$TV_BIN" >/dev/null 2>&1 || { REASON="TV process not running"; return 1; }
  curl -sf --max-time 3 "http://localhost:9222/json/version" >/dev/null || { REASON="CDP not responding"; return 1; }
  curl -s --max-time 3 "http://localhost:9222/json" 2>/dev/null | grep -q "tradingview.com/chart/" \
    || { REASON="no chart page (white/login/splash state)"; return 1; }
  # Tier 4: does the chart actually execute JS, or is it a zombie?
  "$PYTHON" - <<'PY' >/dev/null 2>&1 || { REASON="chart page unresponsive to Runtime.evaluate (zombie)"; return 1; }
import json, sys, urllib.request, websocket
pages = json.loads(urllib.request.urlopen("http://localhost:9222/json", timeout=5).read())
chart = next(p for p in pages if p.get('type') == 'page' and 'tradingview.com/chart/' in p.get('url', ''))
ws = websocket.create_connection(chart['webSocketDebuggerUrl'], timeout=8, suppress_origin=True)
ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "2+2", "returnByValue": True}}))
msg = json.loads(ws.recv())
ws.close()
sys.exit(0 if msg.get('result', {}).get('result', {}).get('value') == 4 else 1)
PY
  return 0
}

REASON=""
if health; then
  read_state
  [ "$FAILS" -gt 0 ] && echo "$(ts)  healthy again (was failing)"
  echo "$(ts)  healthy"
  write_state 0 "$LAST_RELAUNCH"
  exit 0
fi

echo "$(ts)  UNHEALTHY: $REASON"
read_state

# Relaunch cooldown: never bounce TV more than once per 10 minutes, so we
# don't fight a slow boot or hammer CDP (per prior feedback).
NOW=$(date +%s)
if [ $(( NOW - LAST_RELAUNCH )) -lt 600 ]; then
  echo "$(ts)  last relaunch $(( NOW - LAST_RELAUNCH ))s ago (<10min) — waiting, not bouncing again"
  exit 0
fi

# ─── Recover ─────────────────────────────────────────────────────────────
FAILS=$(( FAILS + 1 ))
write_state "$FAILS" "$NOW"
echo "$(ts)  recovery attempt (consecutive failure #$FAILS): quitting TV…"

/usr/bin/osascript -e 'quit app "TradingView"' >/dev/null 2>&1 || true
i=0
while pgrep -f "$TV_BIN" >/dev/null 2>&1 && [ $i -lt 15 ]; do sleep 1; i=$((i+1)); done
pkill -9 -f "$TV_BIN" 2>/dev/null || true
sleep 3

# On the 2nd+ consecutive failure, clear Electron's GPU cache before
# relaunching — the leading suspect for the recurring white-screen boot.
# Safe: Electron rebuilds these caches on next launch.
if [ "$FAILS" -ge 2 ]; then
  echo "$(ts)  clearing GPU caches (failure #$FAILS)"
  rm -rf "$HOME/Library/Application Support/TradingView/GPUCache" \
         "$HOME/Library/Application Support/TradingView/DawnGraphiteCache" \
         "$HOME/Library/Application Support/TradingView/DawnWebGPUCache" 2>/dev/null || true
fi

# Launch the binary directly — `open --args` is unreliable for Electron.
nohup "$TV_BIN" --remote-debugging-port=9222 > /tmp/tv-keepalive-launch.log 2>&1 &
disown
echo "$(ts)  relaunched TV (PID $!), waiting 25s before first probe…"
sleep 25

# Poll CDP up to a further 60s.
i=0
while [ $i -lt 12 ]; do
  curl -sf --max-time 3 "http://localhost:9222/json/version" >/dev/null && break
  sleep 5; i=$((i+1))
done

# Poll for the chart page up to 90s.
j=0
while [ $j -lt 30 ]; do
  if curl -s --max-time 3 "http://localhost:9222/json" 2>/dev/null | grep -q "tradingview.com/chart/"; then
    sleep 5  # buffer for indicators to compute
    if health; then
      echo "$(ts)  RECOVERED after $(( 25 + i*5 + j*3 + 5 ))s"
      notify "RECOVERED" "TV was down ($REASON), relaunched and healthy again"
      write_state 0 "$NOW"
      exit 0
    fi
  fi
  sleep 3; j=$((j+1))
done

echo "$(ts)  RECOVERY FAILED — TV still unhealthy after relaunch ($REASON)"
if [ "$FAILS" -ge 3 ]; then
  notify "MANUAL ATTENTION" "TV won't come up healthy after $FAILS attempts. Likely login/dialog. Open TV manually."
else
  notify "TV DOWN" "Relaunch #$FAILS didn't produce a healthy chart. Will retry in ~10min."
fi
exit 1
