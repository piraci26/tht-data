#!/usr/bin/env python3
"""Pull all US-listed stocks with mcap > $3.5B from NASDAQ's public screener API.
Writes universe.json with ticker, name, mcap. Run weekly to refresh.
"""
import json, os, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(HERE, "universe.json")
NAMES_OUT = os.path.join(HERE, "universe_names.json")
MIN_MCAP = 3.5e9

URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&marketcap=mega%7Clarge%7Cmid"

def is_common(name):
    n = name.lower()
    bad = [" warrant", " right", " unit", " preferred", " etf", " trust", " note",
           " bond", " depositary", "%", "share class"]
    return not any(b in n for b in bad)

def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    rows = data["data"]["table"]["rows"]
    out = []
    names = {}
    for r in rows:
        sym = r["symbol"].strip()
        if not sym or "/" in sym or "^" in sym: continue
        mc_str = (r.get("marketCap") or "").replace(",", "").strip()
        try: mc = float(mc_str) if mc_str else 0
        except: mc = 0
        if mc < MIN_MCAP: continue
        name = r.get("name", "").replace(" Common Stock", "").replace(" Class A", "").replace(" Class B", "").strip()
        if not is_common(r.get("name", "")): continue
        out.append({"sym": sym, "name": name, "mcap": round(mc / 1e9)})
        names[sym] = name
    out.sort(key=lambda x: -x["mcap"])
    json.dump([r["sym"] for r in out], open(OUT, "w"))
    json.dump(names, open(NAMES_OUT, "w"), indent=1)
    print(f"Wrote {len(out)} tickers (mcap >= ${MIN_MCAP/1e9:.1f}B) to {OUT}")
    print("Top 5:", [(o['sym'], f"${o['mcap']}B") for o in out[:5]])
    print("Bottom 5:", [(o['sym'], f"${o['mcap']}B") for o in out[-5:]])

if __name__ == "__main__":
    main()
