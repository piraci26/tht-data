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


class TVCrashedError(RuntimeError):
    """Raised when the CDP reconnect can't find a TradingView chart page.
    This means TradingView Desktop itself died (not just the websocket).
    Callers catch this to checkpoint progress and exit with a special code
    so the wrapper can relaunch TV and resume the scan."""


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
        try:
            resp = urllib.request.urlopen(f"{CDP_HTTP}/json", timeout=5).read()
        except (urllib.error.URLError, OSError) as e:
            raise TVCrashedError(f"CDP unreachable on {CDP_HTTP}: {e}")
        pages = json.loads(resp)
        chart = None
        for p in pages:
            if p.get('type') == 'page' and 'tradingview.com/chart/' in p.get('url', ''):
                chart = p
                break
        if chart is None:
            raise TVCrashedError(
                "No TradingView chart page found in CDP "
                f"(saw {len(pages)} pages but none on tradingview.com/chart/)"
            )
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
            # _connect() now raises TVCrashedError if TV itself is gone — we
            # let that propagate so the scan can checkpoint + exit.
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


def enable_symbol_sync(cdp, enable_symbol=True, enable_interval=False):
    """Toggle TradingView's per-layout symbol sync. With symbol sync ON,
    a single setSymbol() call cascades to every pane in the layout. With
    interval sync OFF, each pane keeps its own timeframe."""
    return cdp.evaluate(f"""
      (function() {{
        try {{
          window.TradingViewApi._symbolSync.setValue({"true" if enable_symbol else "false"});
          window.TradingViewApi._intervalSync.setValue({"true" if enable_interval else "false"});
          return {{
            symbol_sync: window.TradingViewApi._symbolSync.value(),
            interval_sync: window.TradingViewApi._intervalSync.value(),
          }};
        }} catch(e) {{ return {{ error: String(e) }}; }}
      }})()
    """)


def enumerate_panes(cdp):
    """Return [{pane, symbol, resolution}] for every chart pane in the
    current layout. Used to verify the TATA 4-pane setup is loaded."""
    return cdp.evaluate("""
      (function() {
        try {
          var defs = window.TradingViewApi._chartWidgetCollection._chartWidgetsDefs;
          var out = [];
          for (var i = 0; i < defs.length; i++) {
            var w = defs[i].chartWidget;
            if (!w) { out.push({pane:i, err:'no widget'}); continue; }
            var ms = w.model().model().mainSeries();
            out.push({ pane: i, symbol: ms.symbol(), resolution: ms.interval() });
          }
          return out;
        } catch(e) { return { error: String(e) }; }
      })()
    """)


def read_lux_all_panes(cdp):
    """Read LuxAlgo Signals & Overlays + LuxAlgo Meta data from every pane in
    a single round-trip. Returns a list of pane-records:

        [
          {pane: 0, resolution: '1D', found: True, values: {...}, meta: {...}},
          {pane: 1, resolution: '1W', ...},
          ...
        ]

    Used by the 4-pane regime scan: one chart_set_symbol changes all panes
    (when symbol sync is enabled), then one read_lux_all_panes call returns
    every timeframe's regime data.
    """
    return cdp.evaluate("""
      (function() {
        try {
          var defs = window.TradingViewApi._chartWidgetCollection._chartWidgetsDefs;
          var out = [];
          for (var i = 0; i < defs.length; i++) {
            var widget = defs[i].chartWidget;
            if (!widget) { out.push({ pane: i, found: false, err: 'no widget' }); continue; }
            var sources = widget.model().model().dataSources();
            var luxVals = null, metaVals = null;
            for (var si = 0; si < sources.length; si++) {
              var s = sources[si];
              if (!s.metaInfo) continue;
              var meta = s.metaInfo();
              var name = meta.description || meta.shortDescription || '';
              if (typeof name !== 'string') continue;
              var dwv = s.dataWindowView();
              if (!dwv) continue;
              var items = dwv.items();
              var values = {};
              for (var j = 0; j < items.length; j++) {
                var it = items[j];
                if (it._title && it._value && it._value !== '∅') values[it._title] = it._value;
              }
              if (name.indexOf('LuxAlgo Meta') !== -1) metaVals = values;
              else if (name.indexOf('LuxAlgo') !== -1 && name.indexOf('Signals') !== -1) luxVals = values;
            }
            var res = null, sym = null;
            try { res = widget.model().model().mainSeries().interval(); } catch(e) {}
            try { sym = widget.model().model().mainSeries().symbol(); } catch(e) {}
            out.push({
              pane: i, resolution: res, symbol: sym,
              found: !!luxVals,
              values: luxVals || {},
              meta: metaVals || {},
            });
          }
          return out;
        } catch(e) { return { error: String(e) }; }
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
