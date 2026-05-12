"""Query the event log + snapshot log.

Examples:
  python analyze.py --summary
  python analyze.py --since 2026-05-12 --by sector --event iq_bands_green
  python analyze.py --since 2026-05-12 --by industry --tf daily
  python analyze.py --regime-timeline --tf daily
  python analyze.py --top-flippers --since 2026-05-12 --n 20
"""
import json, os, argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_PATH    = os.path.join(HERE, "docs", "history", "events.jsonl")
SNAPSHOTS_PATH = os.path.join(HERE, "docs", "history", "snapshots.jsonl")

def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def load_events(since=None, until=None, tf=None, event=None):
    out = []
    if not os.path.exists(EVENTS_PATH): return out
    since_dt = parse_iso(since) if since else None
    until_dt = parse_iso(until) if until else None
    with open(EVENTS_PATH) as f:
        for line in f:
            r = json.loads(line)
            if tf and r.get("tf") != tf: continue
            if event and r.get("event") != event: continue
            ts = parse_iso(r["ts"])
            if since_dt and ts < since_dt: continue
            if until_dt and ts > until_dt: continue
            out.append(r)
    return out

def load_snapshots(since=None, tf=None):
    out = []
    if not os.path.exists(SNAPSHOTS_PATH): return out
    since_dt = parse_iso(since) if since else None
    with open(SNAPSHOTS_PATH) as f:
        for line in f:
            r = json.loads(line)
            if tf and r.get("tf") != tf: continue
            if since_dt and parse_iso(r["ts"]) < since_dt: continue
            out.append(r)
    return out

def cmd_summary(args):
    events = load_events()
    snaps = load_snapshots()
    if not events and not snaps:
        print("No history yet — wait for the next cron tick.")
        return
    by_event = Counter(e["event"] for e in events)
    by_tf    = Counter(e.get("tf") for e in events)
    by_sec   = Counter(e.get("sector") for e in events if e.get("sector"))
    print(f"Events:    {len(events):>6} total")
    for k, v in by_event.most_common():
        print(f"  {v:>6}  {k}")
    print(f"\nBy timeframe:")
    for k, v in by_tf.most_common():
        print(f"  {v:>6}  {k}")
    print(f"\nBy sector (top 10):")
    for k, v in by_sec.most_common(10):
        print(f"  {v:>6}  {k}")
    print(f"\nSnapshots: {len(snaps):>6} total")
    if snaps:
        print(f"  first: {snaps[0]['ts']}")
        print(f"  last:  {snaps[-1]['ts']}")

def cmd_by(args):
    events = load_events(since=args.since, until=args.until, tf=args.tf, event=args.event)
    if not events:
        print("No matching events.")
        return
    key = args.by  # 'sector' / 'industry' / 'sym' / 'event'
    counts = Counter(e.get(key) for e in events if e.get(key))
    print(f"{len(events)} events grouped by {key} (filters: tf={args.tf} event={args.event} since={args.since}):")
    for k, v in counts.most_common(args.n or 25):
        print(f"  {v:>6}  {k}")

def cmd_regime_timeline(args):
    snaps = load_snapshots(since=args.since, tf=args.tf or "daily")
    if not snaps:
        print("No snapshots yet.")
        return
    # Coarse aggregation: group by hour
    by_hour = defaultdict(lambda: {"fvb_g":0,"fvb_r":0,"bxt_g":0,"bxt_r":0,"n":0})
    for s in snaps:
        hr = s["ts"][:13]  # YYYY-MM-DDTHH
        for k in ("fvb_g","fvb_r","bxt_g","bxt_r"):
            by_hour[hr][k] = max(by_hour[hr][k], s.get(k, 0))
        by_hour[hr]["n"] += 1
    print(f"{'Hour (UTC)':<20} {'Bands G':>8} {'Bands R':>8} {'Osc G':>8} {'Osc R':>8}  net")
    for hr in sorted(by_hour.keys()):
        v = by_hour[hr]
        net = (v["fvb_g"]+v["bxt_g"]) - (v["fvb_r"]+v["bxt_r"])
        net_str = f"+{net}" if net >= 0 else str(net)
        print(f"{hr:<20} {v['fvb_g']:>8} {v['fvb_r']:>8} {v['bxt_g']:>8} {v['bxt_r']:>8}  {net_str}")

def cmd_top_flippers(args):
    events = load_events(since=args.since, until=args.until, tf=args.tf, event=args.event)
    if not events:
        print("No events.")
        return
    counts = Counter(e["sym"] for e in events)
    print(f"Top symbols by event count (filters: tf={args.tf} event={args.event}):")
    for sym, n in counts.most_common(args.n or 20):
        ev_breakdown = Counter(e["event"] for e in events if e["sym"] == sym)
        breakdown = ", ".join(f"{k}={v}" for k, v in ev_breakdown.most_common())
        print(f"  {n:>4}  {sym:<6}  ({breakdown})")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--by", choices=["sector","industry","sym","event","tf"])
    ap.add_argument("--regime-timeline", action="store_true")
    ap.add_argument("--top-flippers", action="store_true")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--tf", choices=["daily","weekly","monthly","ath","atl","ath_atl"])
    ap.add_argument("--event")
    ap.add_argument("--n", type=int)
    args = ap.parse_args()

    if args.summary:               cmd_summary(args)
    elif args.by:                  cmd_by(args)
    elif args.regime_timeline:     cmd_regime_timeline(args)
    elif args.top_flippers:        cmd_top_flippers(args)
    else:                          cmd_summary(args)

if __name__ == "__main__":
    main()
