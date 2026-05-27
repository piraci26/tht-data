"""
TradingView Desktop CDP primitives — the byte-identical scan logic that
used to live inside lux_scan_full.py. Extracted unchanged so the canonical
scanner (regime_scan.py) is the single CDP runner and lux_scan_full.py
can go away.

Provides:
  CHART_API constant — the JS expression that resolves the active chart widget.
  load_universe(threshold) / load_names() / load_mcaps() — universe IO.
  CDPClient — connects to chrome://inspect on :9222, sends Runtime.evaluate
              messages over the raw WebSocket, returns the JS return values.
  set_symbol(cdp, sym) / set_timeframe(cdp, tf) — chart navigation.
  is_loading(cdp) — whether TradingView's loading spinner is visible.
  read_lux(cdp) — reads the LuxAlgo Signals & Overlays + LuxAlgo Meta
                  data windows for the currently-loaded chart.

NOTHING in this module differs from the prior lux_scan_full.py code paths.
Per-ticker work, timing, retries, and the JS source it executes are all
preserved. If we ever break scan results, look for diffs against the
2026-05-27 baseline.
"""
import json, os, sys
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

    def evaluate(self, expression, timeout=10, _retried=False):
        """Run a JS expression on the chart. Auto-reconnects on broken pipe /
        closed-socket errors so a single websocket drop doesn't abort the
        whole ~4h scan."""
        try:
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
        except (BrokenPipeError, ConnectionResetError, websocket.WebSocketConnectionClosedException, OSError) as e:
            if _retried:
                raise
            print(f"  [cdp] websocket dropped ({type(e).__name__}: {e}); reconnecting…")
            try:
                if self.ws: self.ws.close()
            except Exception:
                pass
            self._connect()
            return self.evaluate(expression, timeout=timeout, _retried=True)

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
