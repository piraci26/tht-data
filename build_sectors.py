"""One-shot: build universe_sectors.json by fetching sector + industry for every
ticker in universe.json. Idempotent — re-running merges new symbols, keeps old ones.

Run: python build_sectors.py
"""
import json, os, urllib.request, urllib.parse, time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
TICKERS = json.load(open(os.path.join(HERE, "universe.json")))
OUT = os.path.join(HERE, "universe_sectors.json")

try:
    existing = json.load(open(OUT))
except FileNotFoundError:
    existing = {}

def fetch_sector(sym):
    """stockanalysis.com profile API — returns sector + industry. No auth needed."""
    sym_q = sym.replace(".", "-").lower()
    url = f"https://stockanalysis.com/api/symbol/s/{urllib.parse.quote(sym_q)}/profile"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        profile = d.get("data", {}).get("profile", {})
        sec = profile.get("sector",   {}).get("value") if isinstance(profile.get("sector"), dict) else None
        ind = profile.get("industry", {}).get("value") if isinstance(profile.get("industry"), dict) else None
        if not sec and not ind: return sym, None
        return sym, {"sector": sec, "industry": ind}
    except Exception:
        return sym, None

missing = [s for s in TICKERS if s not in existing or not existing[s].get("sector")]
print(f"Universe: {len(TICKERS)}, existing: {len(existing)}, fetching: {len(missing)}")

t0 = time.time()
with ThreadPoolExecutor(max_workers=15) as ex:
    for i, f in enumerate(as_completed([ex.submit(fetch_sector, s) for s in missing]), 1):
        sym, data = f.result()
        if data:
            existing[sym] = data
        if i % 100 == 0:
            print(f"  {i}/{len(missing)} done ({round(time.time()-t0,1)}s)")
            # Save progress periodically
            with open(OUT, "w") as fh:
                json.dump(existing, fh, indent=2, sort_keys=True)

with open(OUT, "w") as fh:
    json.dump(existing, fh, indent=2, sort_keys=True)

# Quick stats
sectors = {}
for sym, meta in existing.items():
    sec = meta.get("sector")
    if sec: sectors[sec] = sectors.get(sec, 0) + 1
print(f"\nDone in {round(time.time()-t0,1)}s. Sectors:")
for s, n in sorted(sectors.items(), key=lambda x: -x[1]):
    print(f"  {n:4d} {s}")
