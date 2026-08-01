#!/usr/bin/env python3
"""Re-push docs/regime_state.json to the Supabase regime_state table.

Recovery tool for runs where the scan captured cleanly but the Supabase
write failed (e.g. the Jul 28 - Aug 1 2026 window: project migration left
an invalid secret key in the wrapper's environment, so every nightly push
401-skipped). Run with fresh SUPABASE_* env, then run setups_classify.py
to re-push setups / groups / breadth for the same snapshot:

    set -a; source .env; set +a
    python3 repush_supabase.py && python3 setups_classify.py

Mirrors the dual-write block at the end of regime_scan.main() — keep the
row shape in sync with it.
"""
import json, os, sys
import supabase_client as sb

HERE = os.path.dirname(os.path.abspath(__file__))
IN_PATH = os.path.join(HERE, "docs", "regime_state.json")


def main():
    doc = json.load(open(IN_PATH))
    finished_at = doc.get("updated_at")
    rows = []
    for tf, by_sym in (doc.get("by_tf") or {}).items():
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
    if not rows:
        print("regime_state.json has no by_tf rows — nothing to push")
        sys.exit(1)
    n = sb.upsert("regime_state", rows, on_conflict="symbol,tf")
    print(f"Supabase: upserted {n} regime_state rows (snapshot {finished_at})")
    sb.record_run("regime_scan", finished_at, finished_at, True, n,
                  f"repush of {len(rows)} rows from regime_state.json")


if __name__ == "__main__":
    main()
