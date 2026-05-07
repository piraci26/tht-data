#!/usr/bin/env python3
"""One-time pull of shares outstanding for all S&P 500 tickers from stockanalysis.com.
Run weekly to refresh. Caches to shares_outstanding.json."""
import json, os, urllib.request, time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
TICKERS = json.load(open(os.path.join(HERE, "universe.json")))
OUT = os.path.join(HERE, "shares_outstanding.json")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def parse_num(s):
    if not s or s == "n/a": return None
    s = s.strip().replace(",", "")
    mult = 1
    if s.endswith("T"): mult, s = 1e12, s[:-1]
    elif s.endswith("B"): mult, s = 1e9, s[:-1]
    elif s.endswith("M"): mult, s = 1e6, s[:-1]
    elif s.endswith("K"): mult, s = 1e3, s[:-1]
    try: return float(s) * mult
    except: return None

def fetch_one(sym):
    url = f"https://api.stockanalysis.com/api/symbol/s/{sym.lower().replace('.','-')}/overview"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        d = data.get("data", {})
        shares = parse_num(d.get("sharesOut"))
        mcap = parse_num(d.get("marketCap"))
        return sym, shares, mcap
    except Exception as e:
        return sym, None, None

def main():
    print(f"Fetching shares outstanding for {len(TICKERS)} tickers...")
    out = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(fetch_one, s): s for s in TICKERS}
        for i, fut in enumerate(as_completed(futs), 1):
            sym, shares, mcap = fut.result()
            if shares is not None:
                out[sym] = {"shares": shares, "mcap_ref": mcap}
            if i % 50 == 0:
                print(f"  {i}/{len(TICKERS)}  ({time.time()-t0:.1f}s)")
    print(f"Got {len(out)}/{len(TICKERS)} in {time.time()-t0:.1f}s")
    json.dump(out, open(OUT, "w"), indent=1)
    print(f"Wrote {OUT}")

if __name__ == "__main__":
    main()
