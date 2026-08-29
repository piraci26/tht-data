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
    read_lux_all_panes, enumerate_panes, enable_symbol_sync, wait_for_api,
    load_universe, load_names, load_mcaps,
    TVCrashedError,
)
import supabase_client as sb  # no-op when SUPABASE_* env vars are unset

# Exit code returned when TradingView Desktop dies mid-scan. The wrapper
# script catches this and restarts TV, then re-runs with --resume.
EXIT_TV_CRASHED = 87
CHECKPOINT_PATH = os.path.join(HERE, "docs", "regime_state_checkpoint.json")
CHECKPOINT_EVERY = 100  # tickers


# TradingView returns resolutions like '1D', '1W', '1M', '720'. Map them to
# the scan's TF labels.
_RESOLUTION_TO_TF = {
    "1D": "D", "D": "D",
    "1W": "W", "W": "W",
    "1M": "M", "M": "M",
}


def _num(s):
    if s is None: return None
    try: return float(str(s).replace(",", ""))
    except (ValueError, TypeError): return None


def _read_with_meta_retry(cdp, wait):
    """read_lux can come back without Days Since Green/Red on the first read
    after a TF switch or fast symbol-flip — the LuxAlgo Meta indicator depends
    on Signals & Overlays and computes second. Retry once with a longer wait
    if meta is empty but the symbol does have signals values."""
    v = read_lux(cdp)
    if v and v.get("found"):
        meta = v.get("meta") or {}
        if not meta.get("Days Since Green") and not meta.get("Days Since Red"):
            time.sleep(max(wait, 1.8))
            v2 = read_lux(cdp)
            if v2 and v2.get("found") and (v2.get("meta") or {}).get("Days Since Green"):
                return v2
    return v


def scan_one_tf(cdp, tickers, names, mcaps, tf, wait):
    print(f"\n>>> Timeframe {tf}")
    set_timeframe(cdp, tf)
    # Longer warm-up after TF change — the indicators recompute in cascade
    # (Meta depends on Signals & Overlays) and the first ticker after a TF
    # switch is most likely to come back with empty Meta values.
    time.sleep(4.0)
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

            v = _read_with_meta_retry(cdp, wait)
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


def _parse_pane_record(p, names, mcaps):
    """Turn a single pane record from read_lux_all_panes() into the
    standardised regime row used by scan_one_tf."""
    if not p or not p.get("found"):
        return None
    vals = p.get("values") or {}
    meta = p.get("meta") or {}
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
    return {
        "regime": regime,
        "dsg": dsg, "dsr": dsr, "ts": ts,
        "fired_g": b or bp, "fired_g_plus": bp,
        "fired_r": be or bep, "fired_r_plus": bep,
    }


def _save_checkpoint(by_tf, processed_syms, args_snapshot):
    """Persist scan progress so a TV crash mid-run can be resumed."""
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "args": args_snapshot,
        "processed_count": len(processed_syms),
        "processed": list(processed_syms),
        "by_tf": by_tf,
    }
    tmp = CHECKPOINT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, CHECKPOINT_PATH)


def _load_checkpoint(args_snapshot):
    """Return (by_tf_dict, processed_set) if a valid resumable checkpoint
    exists for the same args; otherwise (None, None)."""
    if not os.path.exists(CHECKPOINT_PATH):
        return None, None
    try:
        d = json.load(open(CHECKPOINT_PATH))
    except (json.JSONDecodeError, OSError):
        return None, None
    # Only resume if the args match — otherwise the old checkpoint is for
    # a different universe / threshold / TF set and would corrupt this run.
    if d.get("args") != args_snapshot:
        return None, None
    return d.get("by_tf") or {}, set(d.get("processed") or [])


def _clear_checkpoint():
    try:
        os.remove(CHECKPOINT_PATH)
    except FileNotFoundError:
        pass


def scan_multi_pane(cdp, tickers, names, mcaps, expected_tfs, wait, sym_set,
                    resume_by_tf=None, resume_processed=None, args_snapshot=None):
    """Fast scan: relies on the TATA 4-pane layout with symbol sync ON.
    One chart_set_symbol per ticker propagates to every pane; one
    read_lux_all_panes() call returns D / W / M (and the 12H reference).

    expected_tfs: list of TF labels we want to capture (e.g. ['D','W','M']).
                  Pane resolutions not in this list (e.g. the 12H reference
                  pane) are read but discarded.
    """
    # Discover the pane layout once.
    panes_info = enumerate_panes(cdp) or []
    if isinstance(panes_info, dict) and panes_info.get("error"):
        # A TypeError mentioning TradingViewApi internals means the page is
        # half-booted (spinner state) — restartable, not fatal.
        raise TVCrashedError(f"enumerate_panes failed: {panes_info['error']}")
    print("Panes:", panes_info)
    res_to_pane = {p["resolution"]: p["pane"] for p in panes_info if "resolution" in p}
    missing = [tf for tf in expected_tfs if f"1{tf}" not in res_to_pane and tf not in res_to_pane]
    if missing:
        sys.exit(f"TATA layout missing panes for: {missing}. Got: {res_to_pane}")

    # Seed from a previous checkpoint if resuming.
    by_tf = {tf: {} for tf in expected_tfs}
    if resume_by_tf:
        for tf in expected_tfs:
            for sym, rec in (resume_by_tf.get(tf) or {}).items():
                by_tf[tf][sym] = rec
    processed = set(resume_processed or ())
    if processed:
        print(f"Resuming from checkpoint: {len(processed)} tickers already done")

    started = time.time()

    for i, sym in enumerate(tickers):
        if sym in processed:
            continue
        try:
            # One setSymbol updates ALL panes (symbol sync is on).
            r = sym_set(cdp, sym)
            if not r or not r.get("ok"):
                continue

            # Wait for all panes to settle. The slowest pane drives this.
            t0 = time.time()
            while time.time() - t0 < 4:
                if not is_loading(cdp):
                    break
                time.sleep(0.1)
            time.sleep(wait)

            panes = read_lux_all_panes(cdp)
            if not panes or (isinstance(panes, dict) and panes.get("error")):
                continue

            # First pass: parse every pane. Retry once for any pane whose Meta
            # came back empty (cascading-warm-up race condition).
            parsed = {}
            need_retry = False
            for p in panes:
                tf_label = _RESOLUTION_TO_TF.get(str(p.get("resolution")))
                if tf_label not in expected_tfs:
                    continue
                rec = _parse_pane_record(p, names, mcaps)
                if rec and (rec["dsg"] is not None or rec["dsr"] is not None):
                    parsed[tf_label] = rec
                else:
                    need_retry = True

            if need_retry:
                time.sleep(max(wait, 1.5))
                panes = read_lux_all_panes(cdp) or []
                for p in panes:
                    tf_label = _RESOLUTION_TO_TF.get(str(p.get("resolution")))
                    if tf_label not in expected_tfs or tf_label in parsed:
                        continue
                    rec = _parse_pane_record(p, names, mcaps)
                    if rec:
                        parsed[tf_label] = rec

            for tf_label, rec in parsed.items():
                by_tf[tf_label][sym] = {
                    **rec,
                    "name": names.get(sym, ""),
                    "mcap": mcaps.get(sym, 0),
                }
            processed.add(sym)

            if (i + 1) % 25 == 0:
                el = time.time() - started
                rate = (i + 1 - len(resume_processed or ())) / el if el else 0
                left = (len(tickers) - i - 1) / rate if rate else 0
                captured = sum(len(v) for v in by_tf.values())
                print(f"  [multi] {i+1}/{len(tickers)}  elapsed {el:.0f}s ~{left:.0f}s left  total captured={captured}")

            # Periodic checkpoint — survives TV crashes.
            if (i + 1) % CHECKPOINT_EVERY == 0 and args_snapshot is not None:
                _save_checkpoint(by_tf, processed, args_snapshot)
        except TVCrashedError:
            # Surface to caller — checkpoint is written below and the script
            # exits with EXIT_TV_CRASHED so the wrapper can restart TV.
            print(f"  [multi] TV CRASHED at ticker {i+1}/{len(tickers)} ({sym}) — writing checkpoint")
            if args_snapshot is not None:
                _save_checkpoint(by_tf, processed, args_snapshot)
            raise
        except Exception as e:
            print(f"  {sym}: ERR {e}")

    for tf, sym_map in by_tf.items():
        print(f"  [multi] {tf}: {len(sym_map)} captured")
    return by_tf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=3.5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--wait", type=float, default=1.2)
    ap.add_argument("--tfs", default="D,W,M")
    ap.add_argument("--multi-pane", action="store_true",
                    help="Use the TATA 4-pane layout: one setSymbol cascades "
                         "to all panes (requires symbol sync ON). ~3x faster.")
    ap.add_argument("--resume", action="store_true",
                    help="Load docs/regime_state_checkpoint.json and skip "
                         "tickers already captured there. Used by the wrapper "
                         "to continue after a mid-scan TV crash.")
    args = ap.parse_args()

    tickers = load_universe(args.threshold)
    names = load_names()
    mcaps = load_mcaps()
    if args.limit:
        tickers = tickers[: args.limit]
    tfs = args.tfs.split(",")

    # Identity tag for the checkpoint — only resume if the args match.
    args_snapshot = {
        "threshold": args.threshold, "limit": args.limit,
        "tfs": tfs, "multi_pane": bool(args.multi_pane),
        "universe_size": len(tickers),
    }

    resume_by_tf, resume_processed = (None, None)
    if args.resume:
        resume_by_tf, resume_processed = _load_checkpoint(args_snapshot)
        if resume_by_tf is None:
            print("--resume passed but no compatible checkpoint found; starting fresh")
            resume_by_tf, resume_processed = ({}, set())

    mode = "multi-pane" if args.multi_pane else "single-pane"
    print(f"Regime scan ({mode}): {len(tickers)} tickers @ ${args.threshold}B × {len(tfs)} TFs ({','.join(tfs)})")
    if args.multi_pane:
        est = len(tickers) * (args.wait + 0.5) / 60
    else:
        est = len(tickers) * (args.wait + 0.5) * len(tfs) / 60
    print(f"Estimated: ~{est:.1f} min")

    cdp = None
    by_tf = {}
    try:
        # CDPClient construction itself can raise TVCrashedError if the chart
        # page hasn't fully loaded yet (e.g. fresh TV launch). Catch it here
        # so the wrapper sees exit 87 (retriable) instead of exit 1.
        cdp = CDPClient()
        # A connected chart page can still be half-booted (endless spinner,
        # window.TradingViewApi undefined). Wait for the API before touching
        # it; raises TVCrashedError → exit 87 → wrapper restarts TV.
        wait_for_api(cdp)
        if args.multi_pane:
            sync = enable_symbol_sync(cdp, enable_symbol=True, enable_interval=False)
            print(f"Sync state: {sync}")
            by_tf = scan_multi_pane(cdp, tickers, names, mcaps, tfs, args.wait, set_symbol,
                                    resume_by_tf=resume_by_tf,
                                    resume_processed=resume_processed,
                                    args_snapshot=args_snapshot)
        else:
            for tf in tfs:
                by_tf[tf] = scan_one_tf(cdp, tickers, names, mcaps, tf, args.wait)
    except TVCrashedError as e:
        print(f"\nTV CRASHED: {e}")
        print(f"Wrapper will restart TV and re-run with --resume.")
        try:
            if cdp: cdp.close()
        except Exception:
            pass
        sys.exit(EXIT_TV_CRASHED)
    finally:
        try:
            if cdp: cdp.close()
        except Exception:
            pass

    # Successful completion — clear the checkpoint so the next run starts fresh.
    _clear_checkpoint()

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
