#!/usr/bin/env python3
"""
LuxAlgo regime scan — captures per-ticker regime state on D, W, M for every
ticker in the universe (not just today's fires). The single canonical
CDP-driven scanner; replaces the older lux_scan_full / lux_scan runners.

Output: docs/regime_state.json — the input to setups_classify.py.

For each ticker × timeframe the recorded fields are:
  regime          'g' | 'r' | None      (whichever of last-green/last-red was more recent)
  dsg, dsr        floats                (Days Since Green / Red from LuxAlgo Meta helper)
  ts              float                 (Trend Strength from LuxAlgo Signals & Overlays)
  fired_g / fired_r           bools     (Bullish/Bearish fire on the current bar)
  fired_g_plus / fired_r_plus bools     (the "+" stronger-variant fires)
"""
import json, time, os, argparse, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from cdp_lib import (
    CDPClient, set_timeframe, set_symbol, is_loading, read_lux,
    load_universe, load_names, load_mcaps,
)
import supabase_client as sb  # no-op when SUPABASE_* env vars are unset


def _num(s):
    if s is None: return None
    try: return float(str(s).replace(",", ""))
    except (ValueError, TypeError): return None


def scan_one_tf(cdp, tickers, names, mcaps, tf, wait):
    print(f"\n>>> Timeframe {tf}")
    set_timeframe(cdp, tf)
    time.sleep(1.5)
    rows = {}
    started = time.time()
    for i, sym in enumerate(tickers):
        try:
            r = set_symbol(cdp, sym)
            if not r or not r.get("ok"):
                continue
            t0 = time.time()
            while time.time() - t0 < 3:
                if not is_loading(cdp):
                    break
                time.sleep(0.1)
            time.sleep(wait)

            v = read_lux(cdp)
            if not v or not v.get("found"):
                continue
            vals = v["values"]
            meta = v.get("meta") or {}

            dsg = _num(meta.get("Days Since Green"))
            dsr = _num(meta.get("Days Since Red"))
            ts  = _num(vals.get("Trend Strength"))
            b   = (vals.get("Bullish",  "0") or "0").startswith("1")
            bp  = (vals.get("Bullish+", "0") or "0").startswith("1")
            be  = (vals.get("Bearish",  "0") or "0").startswith("1")
            bep = (vals.get("Bearish+", "0") or "0").startswith("1")

            regime = None
            if dsg is not None and dsr is not None:
                if dsg < dsr:   regime = "g"
                elif dsr < dsg: regime = "r"

            rows[sym] = {
                "regime": regime,
                "dsg": dsg, "dsr": dsr, "ts": ts,
                "fired_g": b or bp, "fired_g_plus": bp,
                "fired_r": be or bep, "fired_r_plus": bep,
                "name": names.get(sym, ""),
                "mcap": mcaps.get(sym, 0),
            }
            if (i + 1) % 25 == 0:
                el = time.time() - started
                rate = (i + 1) / el
                left = (len(tickers) - i - 1) / rate
                print(f"  [{tf}] {i+1}/{len(tickers)}  elapsed {el:.0f}s ~{left:.0f}s left  captured={len(rows)}")
        except Exception as e:
            print(f"  {sym}: ERR {e}")
    print(f"  [{tf}] done: {len(rows)} captured in {time.time()-started:.0f}s")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=3.5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--wait", type=float, default=1.2)
    ap.add_argument("--tfs", default="D,W,M")
    args = ap.parse_args()

    tickers = load_universe(args.threshold)
    names = load_names()
    mcaps = load_mcaps()
    if args.limit:
        tickers = tickers[: args.limit]
    tfs = args.tfs.split(",")

    print(f"Regime scan: {len(tickers)} tickers @ ${args.threshold}B × {len(tfs)} TFs ({','.join(tfs)})")
    est = len(tickers) * (args.wait + 0.5) * len(tfs) / 60
    print(f"Estimated: ~{est:.1f} min")

    cdp = CDPClient()
    by_tf = {}
    try:
        for tf in tfs:
            by_tf[tf] = scan_one_tf(cdp, tickers, names, mcaps, tf, args.wait)
    finally:
        cdp.close()

    finished_at = datetime.now(timezone.utc).isoformat()
    out = {
        "updated_at": finished_at,
        "threshold_b": args.threshold,
        "tfs": tfs,
        "scanned": len(tickers),
        "by_tf": by_tf,
    }
    out_path = os.path.join(HERE, "docs", "regime_state.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved: {out_path}")

    # ── Supabase dual-write ─────────────────────────────────────────────
    # Flatten by_tf into one row per (symbol, tf). Upsert with the same
    # primary key so each run overwrites the prior state cleanly.
    rows = []
    for tf, by_sym in by_tf.items():
        for sym, r in by_sym.items():
            rows.append({
                "symbol": sym, "tf": tf,
                "regime": r.get("regime"),
                "dsg": r.get("dsg"), "dsr": r.get("dsr"), "ts": r.get("ts"),
                "fired_g":      bool(r.get("fired_g")),
                "fired_g_plus": bool(r.get("fired_g_plus")),
                "fired_r":      bool(r.get("fired_r")),
                "fired_r_plus": bool(r.get("fired_r_plus")),
                "name": r.get("name") or "",
                "mcap": r.get("mcap") or 0,
                "scanned_at": finished_at,
            })
    n = sb.upsert("regime_state", rows, on_conflict="symbol,tf")
    if n:
        print(f"Supabase: upserted {n} regime_state rows")
    sb.record_run("regime_scan", out.get("updated_at"), finished_at, True, n,
                  f"{len(tickers)} tickers × {len(tfs)} TFs")


if __name__ == "__main__":
    main()
