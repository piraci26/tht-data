"""The major indices and their ETFs, as bars files (2026-09-12).

The scan universe is large-cap US equities, so SPY, SPX and the other
indices had no bars file and the app's chart, backtest and optimiser
answered "no feed" for them (owner: "why does ticker SPY and SPX not work
... please add the biggest ones"). These are not scanned — an index has no
market cap and no place in the screener — but their bars are kept fresh
with every run, under the names a trader types (TradingView's), fetched
from Yahoo under Yahoo's.

docs/markets.json is the served list, so the app can offer them by name.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))

# sym (what the app requests) -> (yahoo symbol, name, kind, exchange)
MARKETS = {
    "SPX":   ("^GSPC",     "S&P 500",               "index", "SP"),
    "NDX":   ("^NDX",      "Nasdaq 100",            "index", "NASDAQ"),
    "DJI":   ("^DJI",      "Dow Jones Industrial",  "index", "DJ"),
    "RUT":   ("^RUT",      "Russell 2000",          "index", "RUSSELL"),
    "IXIC":  ("^IXIC",     "Nasdaq Composite",      "index", "NASDAQ"),
    "VIX":   ("^VIX",      "CBOE Volatility Index", "index", "CBOE"),
    "SPY":   ("SPY",       "SPDR S&P 500 ETF",      "etf",   "AMEX"),
    "QQQ":   ("QQQ",       "Invesco QQQ Trust",     "etf",   "NASDAQ"),
    "IWM":   ("IWM",       "iShares Russell 2000",  "etf",   "AMEX"),
    "DIA":   ("DIA",       "SPDR Dow Jones ETF",    "etf",   "AMEX"),
    "VOO":   ("VOO",       "Vanguard S&P 500 ETF",  "etf",   "AMEX"),
    "VTI":   ("VTI",       "Vanguard Total Market", "etf",   "AMEX"),
    "UKX":   ("^FTSE",     "FTSE 100",              "index", "FTSE"),
    "DAX":   ("^GDAXI",    "DAX 40",                "index", "XETR"),
    "CAC":   ("^FCHI",     "CAC 40",                "index", "EURONEXT"),
    "SX5E":  ("^STOXX50E", "Euro Stoxx 50",         "index", "STOXX"),
    "NI225": ("^N225",     "Nikkei 225",            "index", "TSE"),
    "HSI":   ("^HSI",      "Hang Seng",             "index", "HKEX"),
    "NIFTY": ("^NSEI",     "Nifty 50",              "index", "NSE"),
    "TSX":   ("^GSPTSE",   "S&P/TSX Composite",     "index", "TSX"),
}

# what each timeframe's run fetches to stay fresh; the long tail comes from
# backfill_markets() once and survives the merge
FRESH = {"daily": ("1d", "1y"), "weekly": ("1wk", "2y"), "monthly": ("1mo", "10y")}
LONG = {"daily": ("1d", "5y"), "weekly": ("1wk", "10y"), "monthly": ("1mo", "max")}
DIRS = {"daily": "bars", "weekly": "bars_weekly", "monthly": "bars_monthly"}


def write_markets_json():
    rows = [{"sym": s, "name": n, "kind": k, "exchange": x} for s, (_, n, k, x) in MARKETS.items()]
    with open(os.path.join(HERE, "docs", "markets.json"), "w") as f:
        json.dump(rows, f, separators=(",", ":"))


def refresh_markets(timeframe="daily", ranges=None):
    """Merge fresh bars for every market into its file; returns (ok, fail)."""
    from scan import fetch_ohlc, merge_bars, BARS_CAP
    interval, rng = (ranges or FRESH)[timeframe]
    bars_dir = os.path.join(HERE, "docs", DIRS[timeframe])
    os.makedirs(bars_dir, exist_ok=True)
    ok = fail = 0
    for sym, (yahoo, _, _, _) in MARKETS.items():
        _, bars = fetch_ohlc(yahoo, interval, rng)
        if bars:
            merge_bars(os.path.join(bars_dir, f"{sym}.json"), bars, BARS_CAP[timeframe])
            ok += 1
        else:
            fail += 1
    write_markets_json()
    return ok, fail


def backfill_markets():
    for tf in ("daily", "weekly", "monthly"):
        ok, fail = refresh_markets(tf, LONG)
        print(f"markets {tf}: ok {ok}, fail {fail}", flush=True)


if __name__ == "__main__":
    backfill_markets()
