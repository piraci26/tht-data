"""
LuxAlgo daily green-flip scan — talks to TradingView Desktop CDP directly via
raw WebSocket (Runtime.evaluate). Avoids Playwright's connect_over_cdp issues
with the Electron app.

For each ticker in the filtered universe:
  1. chart.setSymbol(sym, {}) via JS
  2. Wait for chart loading spinner to clear
  3. Read LuxAlgo Signals & Overlays data window values
  4. If Bullish == 1 or Bullish+ == 1, mark as green

Output: docs/lux_green_daily.json
"""
import json, time, os, argparse, sys
from datetime import datetime, timezone
import urllib.request
import websocket  # websocket-client

HERE = os.path.dirname(os.path.abspath(__file__))
FILTERED = os.path.join(HERE, 'docs', 'universe_filtered.json')
MCAP_CACHE = os.path.join(HERE, 'docs', 'mcap_cache.json')
NAMES = os.path.join(HERE, 'universe_names.json')
CDP_HTTP = "http://localhost:9222"

CHART_API = "window.TradingViewApi._activeChartWidgetWV.value()"

def load_universe(expected_threshold):
    if not os.path.exists(FILTERED):
        sys.exit(f"Missing {FILTERED}")
    with open(FILTERED) as f:
        d = json.load(f)
    if d.get('threshold_b') != expected_threshold:
        sys.exit(f"universe_filtered @ ${d.get('threshold_b')}B, expected ${expected_threshold}B")
    return d['tickers']

def load_names():
    return json.load(open(NAMES)) if os.path.exists(NAMES) else {}

def load_mcaps():
    if not os.path.exists(MCAP_CACHE):
        return {}
    with open(MCAP_CACHE) as f:
        return {k: v.get('mcap_b', 0) for k, v in json.load(f).items()}

class CDPClient:
    def __init__(self):
        self.ws = None
        self.msg_id = 0
        self._connect()

    def _connect(self):
        # Find the TradingView chart page
        resp = urllib.request.urlopen(f"{CDP_HTTP}/json").read()
        pages = json.loads(resp)
        chart = None
        # Prefer the actual chart page (URL contains tradingview.com/chart/)
        for p in pages:
            if p.get('type') != 'page':
                continue
            url = p.get('url', '')
            if 'tradingview.com/chart/' in url:
                chart = p
                break
        if chart is None:
            sys.exit("Could not find TradingView chart page in CDP targets. Is the chart open?")
        ws_url = chart['webSocketDebuggerUrl']
        print(f"Connecting to {ws_url}")
        # Chrome's CDP rejects WS connections unless Origin is in the allowlist.
        # Bypass by suppressing/clearing the Origin header.
        self.ws = websocket.create_connection(
            ws_url,
            timeout=15,
            suppress_origin=True,
        )
        print(f"Connected to: {chart.get('title', '?')} | {chart.get('url', '?')[:80]}")

    def evaluate(self, expression, await_promise=False, timeout=10):
        self.msg_id += 1
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps({
            'id': self.msg_id,
            'method': 'Runtime.evaluate',
            'params': {
                'expression': expression,
                'returnByValue': True,
                'awaitPromise': await_promise,
            },
        }))
        while True:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get('id') == self.msg_id:
                if 'error' in msg:
                    raise Exception(f"CDP error: {msg['error']}")
                return msg['result']['result'].get('value')

    def close(self):
        if self.ws:
            self.ws.close()

def set_symbol(cdp, symbol):
    return cdp.evaluate(f"""
      (function() {{
        try {{
          var chart = {CHART_API};
          chart.setSymbol('{symbol}', {{}});
          return {{ ok: true }};
        }} catch(e) {{ return {{ ok: false, error: String(e) }}; }}
      }})()
    """)

def is_loading(cdp):
    return cdp.evaluate("""
      (function() {
        var s = document.querySelector('[class*="loader"]') || document.querySelector('[class*="loading"]');
        return !!(s && s.offsetParent !== null);
      })()
    """)

def read_lux(cdp):
    return cdp.evaluate(f"""
      (function() {{
        try {{
          var chart = {CHART_API}._chartWidget;
          var sources = chart.model().model().dataSources();
          for (var si = 0; si < sources.length; si++) {{
            var s = sources[si];
            if (!s.metaInfo) continue;
            var meta = s.metaInfo();
            var name = meta.description || meta.shortDescription || '';
            if (typeof name !== 'string') continue;
            if (name.indexOf('LuxAlgo') === -1 || name.indexOf('Signals') === -1) continue;
            var dwv = s.dataWindowView();
            if (!dwv) continue;
            var items = dwv.items();
            var values = {{}};
            for (var i = 0; i < items.length; i++) {{
              var it = items[i];
              if (it._title && it._value && it._value !== '∅') values[it._title] = it._value;
            }}
            return {{ found: true, values: values, name: name }};
          }}
          return {{ found: false }};
        }} catch(e) {{ return {{ error: String(e) }}; }}
      }})()
    """)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--threshold', type=float, default=100.0)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--wait', type=float, default=1.2)
    args = ap.parse_args()

    tickers = load_universe(args.threshold)
    names = load_names()
    mcaps = load_mcaps()
    if args.limit:
        tickers = tickers[:args.limit]

    print(f"Scanning {len(tickers)} tickers @ ${args.threshold}B threshold (daily)")
    print(f"Per-ticker wait: {args.wait}s → est ~{len(tickers)*(args.wait+0.5)/60:.1f} min")

    cdp = CDPClient()
    greens = []
    all_readings = []
    started = time.time()

    try:
        for i, sym in enumerate(tickers):
            try:
                r = set_symbol(cdp, sym)
                if not r or not r.get('ok'):
                    print(f"  {sym}: setSymbol failed {r}")
                    continue
                # Wait for chart load
                wait_start = time.time()
                while time.time() - wait_start < 3:
                    if not is_loading(cdp):
                        break
                    time.sleep(0.1)
                time.sleep(args.wait)

                vals_resp = read_lux(cdp)
                if not vals_resp or not vals_resp.get('found'):
                    err = vals_resp.get('error') if vals_resp else 'no response'
                    print(f"  {sym}: lux read failed ({err})")
                    continue
                vals = vals_resp['values']
                bull = vals.get('Bullish', '0')
                bull_plus = vals.get('Bullish+', '0')
                is_plus = bull_plus.startswith('1')
                is_green = is_plus or bull.startswith('1')

                reading = {
                    'sym': sym,
                    'name': names.get(sym, sym),
                    'mcap': mcaps.get(sym, 0),
                    'bullish': bull,
                    'bullish_plus': bull_plus,
                    'trend_strength': vals.get('Trend Strength', ''),
                    'is_green': is_green,
                    'is_plus': is_plus,
                }
                all_readings.append(reading)
                if is_green:
                    greens.append(reading)
                    tag = "▲+" if is_plus else "▲"
                    print(f"  {sym}: {tag}  TS={vals.get('Trend Strength', '?')}")

                if (i + 1) % 10 == 0:
                    elapsed = time.time() - started
                    rate = (i + 1) / elapsed
                    rem = (len(tickers) - i - 1) / rate
                    print(f"  [progress] {i+1}/{len(tickers)} | {len(greens)} greens | {elapsed:.0f}s elapsed, ~{rem:.0f}s left")
            except Exception as e:
                print(f"  {sym}: ERR {e}")
    finally:
        cdp.close()

    greens.sort(key=lambda r: -r['mcap'])

    out = {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'timeframe': 'daily',
        'threshold_b': args.threshold,
        'scanned': len(all_readings),
        'green_count': len(greens),
        'greens': greens,
    }
    out_path = os.path.join(HERE, 'docs', 'lux_green_daily.json')
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2)

    print(f"\nDONE in {time.time()-started:.0f}s")
    print(f"Scanned: {len(all_readings)} | Greens: {len(greens)}")
    print(f"Saved: {out_path}")
    print(f"\nGreen tickers (sorted by mcap):")
    for g in greens:
        tag = "▲+" if g['is_plus'] else "▲"
        print(f"  {g['sym']:6s} {tag}  ${g['mcap']:>8.1f}B  TS={g['trend_strength']:>6s}  {g['name'][:50]}")

if __name__ == '__main__':
    main()
