"""One-time bars history backfill (2026-08-29).

Extends every universe ticker's cached OHLC files with long history, merged
through scan.merge_bars so subsequent scans keep accumulating on top:
  daily   fetch 5y  -> capped 780 (~3y)
  weekly  fetch 10y -> capped 520 (~10y)
  monthly fetch max -> capped 480 (~40y)
Also fills the coverage gaps (tickers that never went "clickable" on a
timeframe and so never got a bars file there at all).
"""
import json, os, random, time
from concurrent.futures import ThreadPoolExecutor, as_completed

from scan import fetch_ohlc, merge_bars, BARS_CAP

HERE = os.path.dirname(os.path.abspath(__file__))

# (dirname, interval, range, cap, done_threshold) — a file at or above the
# threshold is considered backfilled and skipped on re-runs, so the script
# is safely resumable after Yahoo rate-limit storms. Young listings whose
# full history is naturally short get refetched harmlessly (merge is
# idempotent).
JOBS = [
    ("bars", "1d", "5y", BARS_CAP["daily"], 700),
    ("bars_weekly", "1wk", "10y", BARS_CAP["weekly"], 400),
    ("bars_monthly", "1mo", "max", BARS_CAP["monthly"], 100),
]

def bar_count(path):
    try:
        with open(path) as f:
            return len(json.load(f))
    except Exception:
        return 0

def fetch_slow(sym, interval, rng):
    time.sleep(random.uniform(0.05, 0.4))
    return fetch_ohlc(sym, interval, rng)

def universe():
    syms = set()
    with open(os.path.join(HERE, "docs", "universe_filtered.json")) as f:
        syms |= set(json.load(f)["tickers"])
    for d, _, _, _, _ in JOBS:
        p = os.path.join(HERE, "docs", d)
        if os.path.isdir(p):
            syms |= {fn[:-5] for fn in os.listdir(p) if fn.endswith(".json")}
    return sorted(syms)

def main():
    syms = universe()
    print(f"backfilling {len(syms)} symbols x {len(JOBS)} timeframes")
    for dirname, interval, rng, cap, done_at in JOBS:
        bars_dir = os.path.join(HERE, "docs", dirname)
        os.makedirs(bars_dir, exist_ok=True)
        todo = [s for s in syms if bar_count(os.path.join(bars_dir, f"{s}.json")) < done_at]
        print(f"{dirname}: {len(todo)} to fetch ({len(syms) - len(todo)} already done)", flush=True)
        done = fail = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(fetch_slow, s, interval, rng) for s in todo]
            for f in as_completed(futs):
                sym, bars = f.result()
                if bars:
                    merge_bars(os.path.join(bars_dir, f"{sym}.json"), bars, cap)
                    done += 1
                else:
                    fail += 1
                if (done + fail) % 200 == 0:
                    print(f"  {dirname}: {done + fail}/{len(todo)} (ok {done}, fail {fail})", flush=True)
        print(f"{dirname}: ok {done}, fail {fail}")

if __name__ == "__main__":
    main()
