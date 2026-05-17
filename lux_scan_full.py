"""
LuxAlgo multi-timeframe scan — daily, weekly, monthly. Captures both green and
red signals (Bullish, Bullish+, Bearish, Bearish+).

Drives TradingView Desktop via raw CDP WebSocket. Same approach as the MCP.

Outputs:
  docs/lux_results.json — one file with all timeframes and both signal types:
  {
    "updated_at": "...",
    "threshold_b": 50.0,
    "scanned": N,
    "timeframes": {
      "D": { "greens": [...], "reds": [...] },
      "W": { "greens": [...], "reds": [...] },
      "M": { "greens": [...], "reds": [...] }
    }
  }

Each entry: { sym, name, mcap, bullish, bullish_plus, bearish, bearish_plus,
              trend_strength, is_plus }
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
        sys.exit(f"universe_filtered @ ${d.get('threshold_b')}B, expected ${expected_threshold}B — rerun build_universe_3_5b.py --threshold {expected_threshold}")
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
        resp = urllib.request.urlopen(f"{CDP_HTTP}/json").read()
        pages = json.loads(resp)
        chart = None
        for p in pages:
            if p.get('type') == 'page' and 'tradingview.com/chart/' in p.get('url', ''):
                chart = p
                break
        if chart is None:
            sys.exit("No TradingView chart page found in CDP")
        ws_url = chart['webSocketDebuggerUrl']
        self.ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)
        print(f"Connected: {chart.get('title', '?')[:60]}")

    def evaluate(self, expression, timeout=10):
        self.msg_id += 1
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps({
            'id': self.msg_id,
            'method': 'Runtime.evaluate',
            'params': {'expression': expression, 'returnByValue': True},
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
          {CHART_API}.setSymbol('{symbol}', {{}});
          return {{ ok: true }};
        }} catch(e) {{ return {{ ok: false, error: String(e) }}; }}
      }})()
    """)

def set_timeframe(cdp, tf):
    return cdp.evaluate(f"""
      (function() {{
        try {{
          {CHART_API}.setResolution('{tf}', {{}});
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
    # Reads BOTH LuxAlgo Signals & Overlays (Bullish/Bearish fires + Trend Strength)
    # AND the local "LuxAlgo Meta" helper (Days Since Green / Days Since Red).
    return cdp.evaluate(f"""
      (function() {{
        try {{
          var chart = {CHART_API}._chartWidget;
          var sources = chart.model().model().dataSources();
          var luxVals = null, metaVals = null;
          for (var si = 0; si < sources.length; si++) {{
            var s = sources[si];
            if (!s.metaInfo) continue;
            var meta = s.metaInfo();
            var name = meta.description || meta.shortDescription || '';
            if (typeof name !== 'string') continue;
            var dwv = s.dataWindowView();
            if (!dwv) continue;
            var items = dwv.items();
            var values = {{}};
            for (var i = 0; i < items.length; i++) {{
              var it = items[i];
              if (it._title && it._value && it._value !== '∅') values[it._title] = it._value;
            }}
            if (name.indexOf('LuxAlgo Meta') !== -1) metaVals = values;
            else if (name.indexOf('LuxAlgo') !== -1 && name.indexOf('Signals') !== -1) luxVals = values;
          }}
          if (!luxVals) return {{ found: false }};
          return {{ found: true, values: luxVals, meta: metaVals || {{}} }};
        }} catch(e) {{ return {{ error: String(e) }}; }}
      }})()
    """)

def scan_one_tf(cdp, tickers, names, mcaps, tf, wait):
    """Scan all tickers at a given timeframe, return (greens, reds, scanned_count)."""
    print(f"\n>>> Switching to timeframe: {tf}")
    set_timeframe(cdp, tf)
    time.sleep(1.5)  # let TF change settle

    greens = []
    reds = []
    scanned = 0
    started = time.time()

    for i, sym in enumerate(tickers):
        try:
            r = set_symbol(cdp, sym)
            if not r or not r.get('ok'):
                continue

            # Wait for loading to clear
            wait_start = time.time()
            while time.time() - wait_start < 3:
                if not is_loading(cdp):
                    break
                time.sleep(0.1)
            time.sleep(wait)

            vals_resp = read_lux(cdp)
            if not vals_resp or not vals_resp.get('found'):
                continue
            vals = vals_resp['values']
            meta_vals = vals_resp.get('meta') or {}
            bull = vals.get('Bullish', '0')
            bull_plus = vals.get('Bullish+', '0')
            bear = vals.get('Bearish', '0')
            bear_plus = vals.get('Bearish+', '0')

            bull_fire = bull.startswith('1') or bull_plus.startswith('1')
            bull_plus_fire = bull_plus.startswith('1')
            bear_fire = bear.startswith('1') or bear_plus.startswith('1')
            bear_plus_fire = bear_plus.startswith('1')

            def _to_int(s):
                try:
                    return int(float(s))
                except Exception:
                    return None

            base = {
                'sym': sym,
                'name': names.get(sym, sym),
                'mcap': mcaps.get(sym, 0),
                'trend_strength': vals.get('Trend Strength', ''),
                # Days since the OPPOSITE signal last fired — i.e. duration of the
                # regime that just ended. None if LuxAlgo Meta isn't on the chart.
                'days_since_green': _to_int(meta_vals.get('Days Since Green')),
                'days_since_red':   _to_int(meta_vals.get('Days Since Red')),
            }
            scanned += 1
            if bull_fire:
                # For a fresh green, the relevant duration is how long the previous red regime lasted.
                greens.append({**base, 'is_plus': bull_plus_fire, 'prior_regime_days': base['days_since_red']})
            if bear_fire:
                reds.append({**base, 'is_plus': bear_plus_fire, 'prior_regime_days': base['days_since_green']})

            if (i + 1) % 20 == 0:
                elapsed = time.time() - started
                rate = (i + 1) / elapsed
                rem = (len(tickers) - i - 1) / rate
                print(f"  [{tf}] {i+1}/{len(tickers)} | G={len(greens)} R={len(reds)} | {elapsed:.0f}s, ~{rem:.0f}s left")
        except Exception as e:
            print(f"  {sym}: ERR {e}")

    greens.sort(key=lambda r: -r['mcap'])
    reds.sort(key=lambda r: -r['mcap'])
    print(f"  [{tf}] DONE: scanned={scanned}, greens={len(greens)}, reds={len(reds)} in {time.time()-started:.0f}s")
    return greens, reds, scanned

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--threshold', type=float, default=50.0)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--wait', type=float, default=1.2)
    ap.add_argument('--tfs', default='D,W,M', help='Comma-separated timeframes')
    args = ap.parse_args()

    tickers = load_universe(args.threshold)
    names = load_names()
    mcaps = load_mcaps()
    if args.limit:
        tickers = tickers[:args.limit]
    tfs = args.tfs.split(',')

    print(f"Scanning {len(tickers)} tickers @ ${args.threshold}B × {len(tfs)} timeframes ({','.join(tfs)})")
    total_est = len(tickers) * (args.wait + 0.5) * len(tfs) / 60
    print(f"Estimated runtime: ~{total_est:.0f} min")

    cdp = CDPClient()
    results = {}
    total_started = time.time()

    try:
        for tf in tfs:
            g, r, scanned = scan_one_tf(cdp, tickers, names, mcaps, tf, args.wait)
            results[tf] = {'greens': g, 'reds': r, 'scanned': scanned}
    finally:
        cdp.close()

    out = {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'threshold_b': args.threshold,
        'tickers_in_scope': len(tickers),
        'timeframes': results,
    }
    out_path = os.path.join(HERE, 'docs', 'lux_results.json')
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2)

    print(f"\n=========================================")
    print(f"DONE in {time.time()-total_started:.0f}s ({(time.time()-total_started)/60:.1f} min)")
    for tf in tfs:
        r = results.get(tf, {})
        print(f"  {tf}: {r.get('scanned', 0)} scanned | {len(r.get('greens', []))} greens | {len(r.get('reds', []))} reds")
    print(f"Saved: {out_path}")

if __name__ == '__main__':
    main()
