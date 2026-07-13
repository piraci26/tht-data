"""
Build a filtered universe based on current market cap threshold.

Fetches market caps via yfinance, caches results (refreshes if older than 20h),
and writes a filtered universe + Desktop watchlist file.

Usage:
    python3 build_universe_3_5b.py                 # default $3.5B threshold
    python3 build_universe_3_5b.py --threshold 5   # $5B threshold
    python3 build_universe_3_5b.py --force         # ignore cache, refetch all

Outputs (relative to this script's dir):
    docs/universe_filtered.json           # filtered ticker list
    docs/mcap_cache.json                  # {sym: {mcap_b: float, ts: iso}}
    ~/Desktop/watchlist_3_5b_FRESH.txt    # one ticker per line for TV upload
"""
import json
import time
import argparse
import os
import sys
import concurrent.futures
from datetime import datetime, timezone

try:
    import yfinance as yf
except ImportError:
    print("yfinance required. Install with: pip install yfinance")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE = os.path.join(HERE, 'universe.json')
CACHE = os.path.join(HERE, 'docs', 'mcap_cache.json')
FILTERED_OUT = os.path.join(HERE, 'docs', 'universe_filtered.json')
DESKTOP = os.path.expanduser('~/Desktop/watchlist_3_5b_FRESH.txt')

# How long cached mcap stays fresh
CACHE_TTL_HOURS = 20

def load_cache():
    if not os.path.exists(CACHE):
        return {}
    try:
        with open(CACHE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, 'w') as f:
        json.dump(cache, f, indent=2, sort_keys=True)

def is_fresh(entry):
    if not entry or 'ts' not in entry:
        return False
    try:
        ts = datetime.fromisoformat(entry['ts'].replace('Z', '+00:00'))
        age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        return age_hours < CACHE_TTL_HOURS
    except Exception:
        return False

def fetch_mcap(sym):
    """Fetch mcap in billions or None."""
    try:
        info = yf.Ticker(sym).fast_info
        mcap = None
        if hasattr(info, 'market_cap'):
            mcap = info.market_cap
        elif hasattr(info, 'get'):
            mcap = info.get('market_cap')
        if mcap and mcap > 0:
            return mcap / 1e9
    except Exception:
        pass
    return None

def build(threshold_b=3.5, force=False, workers=20):
    print(f"Building filtered universe @ ${threshold_b}B threshold")
    print(f"  Cache: {CACHE}")
    print(f"  Force refresh: {force}")

    with open(UNIVERSE) as f:
        universe = json.load(f)
    print(f"  Universe size: {len(universe)}")

    cache = {} if force else load_cache()

    # Determine which tickers need fresh fetch
    stale = [s for s in universe if not is_fresh(cache.get(s))]
    print(f"  Cached fresh: {len(universe) - len(stale)}")
    print(f"  Need fetch: {len(stale)}")

    if stale:
        start = time.time()
        hits = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(fetch_mcap, s): s for s in stale}
            for i, fut in enumerate(concurrent.futures.as_completed(futures)):
                sym = futures[fut]
                mcap = fut.result()
                if mcap is not None:
                    cache[sym] = {
                        'mcap_b': mcap,
                        'ts': datetime.now(timezone.utc).isoformat(),
                    }
                    hits += 1
                if (i + 1) % 200 == 0:
                    print(f"    {i+1}/{len(stale)} ({hits} hits, {time.time()-start:.0f}s)")
        print(f"  Fetched {hits}/{len(stale)} in {time.time()-start:.0f}s")
        save_cache(cache)

    # Filter
    filtered = sorted(
        [(s, cache[s]['mcap_b']) for s in universe
         if s in cache and cache[s].get('mcap_b', 0) >= threshold_b],
        key=lambda x: -x[1],
    )
    print(f"\n>= ${threshold_b}B: {len(filtered)}")
    if filtered:
        print(f"  Top 5: {[s for s,_ in filtered[:5]]}")
        print(f"  Bottom 5 (closest to threshold):")
        for s, m in filtered[-5:]:
            print(f"    {s}: ${m:.2f}B")

    # Write outputs
    os.makedirs(os.path.dirname(FILTERED_OUT), exist_ok=True)
    with open(FILTERED_OUT, 'w') as f:
        json.dump({
            'threshold_b': threshold_b,
            'count': len(filtered),
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'tickers': [s for s, _ in filtered],
        }, f, indent=2)
    print(f"\n  Wrote: {FILTERED_OUT}")

    with open(DESKTOP, 'w') as f:
        for s, _ in filtered:
            f.write(s + '\n')
    print(f"  Wrote: {DESKTOP}")

    dual_write_universe(filtered, threshold_b)

    return [s for s, _ in filtered]


def dual_write_universe(filtered, threshold_b):
    """Mirror the filtered universe into Supabase `universe` (symbol -> sector,
    industry, mcap). This is what backs the dashboard's sector / sub-sector
    filters — the flip/setup/extrema tables carry no sector column, so the
    browser joins against this one. No-op when SUPABASE_* env vars are unset.
    """
    try:
        import supabase_client as sb

        def _load(path):
            try:
                with open(os.path.join(HERE, path)) as f:
                    return json.load(f)
            except Exception:
                return {}

        names   = _load('universe_names.json')
        sectors = _load('universe_sectors.json')

        rows = []
        for sym, mcap_b in filtered:
            meta = sectors.get(sym) or {}
            rows.append({
                'symbol':      sym,
                'name':        names.get(sym) or sym,
                'mcap_b':      round(mcap_b, 2),
                'threshold_b': threshold_b,
                'sector':      meta.get('sector'),
                'industry':    meta.get('industry'),
            })
        # Full replace — symbols that fell below the threshold should disappear
        # rather than linger with a stale cap.
        sb.truncate('universe', where_filter='updated_at=not.is.null')
        n = sb.upsert('universe', rows, on_conflict='symbol')
        missing = sum(1 for r in rows if not r['sector'])
        print(f"  [supabase] universe: {n} rows ({missing} without a sector)")
    except Exception as e:
        print(f"  [supabase] universe write skipped: {e}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--threshold', type=float, default=3.5)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--workers', type=int, default=20)
    args = parser.parse_args()
    build(args.threshold, args.force, args.workers)
