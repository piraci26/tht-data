#!/usr/bin/env python3
"""
Setup classifier — reads docs/regime_state.json and emits docs/setups.json
with all setup groups (A-H) plus aggregate breadth (F).

Notation:
  M[g] / W[g] / D[g]   ticker currently in green regime on that TF
  M[r] / W[r] / D[r]   ticker currently in red regime on that TF
  D[g!]                ticker fires Bullish (or Bullish+) on the current bar

The "age" of a regime = bars since the fire that started it. For a ticker in
the green regime, age = Days Since Green; in the red regime, age = Days Since
Red. Some filters use the OPPOSITE-side days-since to gauge how deep the prior
regime was right before today's flip (e.g. "first D-green after a long red"
checks dsr right after a green fire — that is the depth of the prior red).
"""
import json, os, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import supabase_client as sb  # no-op when SUPABASE_* env vars are unset

IN_PATH  = os.path.join(HERE, "docs", "regime_state.json")
OUT_PATH = os.path.join(HERE, "docs", "setups.json")

# Per-TF "young / mature" thresholds (bars since the regime-starting fire)
THRESH = {
    "D": {"young": 5,  "mature": 20},
    "W": {"young": 4,  "mature": 12},
    "M": {"young": 2,  "mature": 6},
}
PREMIUM_TS = 60.0


def _age(row):
    """Age (in bars) of the current regime, or None if regime unknown."""
    r = row.get("regime")
    if r == "g": return row.get("dsg")
    if r == "r": return row.get("dsr")
    return None


def _entry(sym, by_tf):
    d = by_tf.get("D", {}).get(sym, {}) or {}
    w = by_tf.get("W", {}).get(sym, {}) or {}
    m = by_tf.get("M", {}).get(sym, {}) or {}
    def pack(row):
        return {
            "regime":       row.get("regime"),
            "age":          _age(row),
            "dsg":          row.get("dsg"),
            "dsr":          row.get("dsr"),
            "ts":           row.get("ts"),
            "fired_g":      bool(row.get("fired_g")),
            "fired_r":      bool(row.get("fired_r")),
            "fired_g_plus": bool(row.get("fired_g_plus")),
            "fired_r_plus": bool(row.get("fired_r_plus")),
        }
    return {
        "sym":  sym,
        "name": d.get("name") or w.get("name") or m.get("name") or "",
        "mcap": d.get("mcap") or w.get("mcap") or m.get("mcap") or 0,
        "D": pack(d), "W": pack(w), "M": pack(m),
    }


def main():
    src = json.load(open(IN_PATH))
    by_tf = src.get("by_tf", {})
    syms = set()
    for tf in ("D", "W", "M"):
        syms.update(by_tf.get(tf, {}).keys())

    rows = {s: _entry(s, by_tf) for s in syms}

    R = lambda e, tf, side: e[tf]["regime"] == side       # has-regime
    F = lambda e, tf, side: e[tf][f"fired_{side}"]         # fired today

    groups = {}

    # ─── A: Stack + fresh trigger ─────────────────────────────────────────
    groups["A1"] = [e for e in rows.values()
                    if R(e,"M","g") and R(e,"W","g") and F(e,"D","g")]
    groups["A2"] = [e for e in rows.values()
                    if R(e,"M","g") and F(e,"W","g") and R(e,"D","g")]
    groups["A3"] = [e for e in rows.values()
                    if R(e,"M","g") and F(e,"W","g")
                    and not F(e,"D","g") and not F(e,"D","r")]
    groups["A4_premium"] = [e for e in rows.values()
                    if R(e,"M","g") and R(e,"W","g") and F(e,"D","g")
                    and (e["M"]["ts"] or 0) >= PREMIUM_TS]

    # ─── B: Multi-TF same-day flips ───────────────────────────────────────
    groups["B1_triple_green"] = [e for e in rows.values()
                    if F(e,"D","g") and F(e,"W","g") and F(e,"M","g")]
    groups["B2_WM_green_D_already"] = [e for e in rows.values()
                    if F(e,"W","g") and F(e,"M","g")
                    and R(e,"D","g") and not F(e,"D","g")]
    groups["B3_DW_green_M_already"] = [e for e in rows.values()
                    if F(e,"D","g") and F(e,"W","g")
                    and R(e,"M","g") and not F(e,"M","g")]
    groups["B4_DW_green_counter_macro"] = [e for e in rows.values()
                    if F(e,"D","g") and F(e,"W","g") and not R(e,"M","g")]

    # ─── C: Backbone disagreement (early bottom / top) ────────────────────
    # After a green fire today, D.dsr = depth of the prior red regime.
    groups["C1_first_D_green_deep_red"] = [e for e in rows.values()
                    if R(e,"M","r") and R(e,"W","r") and F(e,"D","g")
                    and (e["D"]["dsr"] or 0) >= 60]
    groups["C2_D_green_W_recently_g"] = [e for e in rows.values()
                    if R(e,"M","r") and F(e,"D","g")
                    and R(e,"W","g") and (e["W"]["dsg"] or 999) <= 5]
    # After a red fire today, D.dsg = depth of the prior green regime.
    groups["C3_first_D_red_deep_green"] = [e for e in rows.values()
                    if R(e,"M","g") and R(e,"W","g") and F(e,"D","r")
                    and (e["D"]["dsg"] or 0) >= 60]

    # ─── D: Pullback in trend ─────────────────────────────────────────────
    groups["D1_pullback"] = [e for e in rows.values()
                    if R(e,"M","g") and R(e,"W","g") and F(e,"D","r")]
    groups["D2_regreen"] = [e for e in rows.values()
                    if R(e,"M","g") and R(e,"W","g") and F(e,"D","g")
                    and (e["D"]["dsr"] or 999) <= 10]

    # ─── E: Bearish mirrors ───────────────────────────────────────────────
    groups["E1_stack_red_D_trigger"] = [e for e in rows.values()
                    if R(e,"M","r") and R(e,"W","r") and F(e,"D","r")]
    groups["E2_triple_red"] = [e for e in rows.values()
                    if F(e,"D","r") and F(e,"W","r") and F(e,"M","r")]
    groups["E3_DW_red_M_already"] = [e for e in rows.values()
                    if F(e,"D","r") and F(e,"W","r")
                    and R(e,"M","r") and not F(e,"M","r")]

    # ─── G: Full alignment (no fire required) ─────────────────────────────
    g1 = [e for e in rows.values() if R(e,"M","g") and R(e,"W","g") and R(e,"D","g")]
    g2 = [e for e in rows.values() if R(e,"M","r") and R(e,"W","r") and R(e,"D","r")]
    groups["G1_bull_stack"] = g1
    groups["G2_bear_stack"] = g2
    groups["G1_young"] = [e for e in g1
                          if (e["D"]["age"] or 999) <= THRESH["D"]["young"]
                          or (e["W"]["age"] or 999) <= THRESH["W"]["young"]
                          or (e["M"]["age"] or 999) <= THRESH["M"]["young"]]
    groups["G1_mature"] = [e for e in g1
                           if (e["D"]["age"] or 0) >= THRESH["D"]["mature"]
                           and (e["W"]["age"] or 0) >= THRESH["W"]["mature"]
                           and (e["M"]["age"] or 0) >= THRESH["M"]["mature"]]
    groups["G1_premium"] = [e for e in g1
                            if (e["M"]["ts"] or 0) >= PREMIUM_TS
                            and (e["W"]["ts"] or 0) >= PREMIUM_TS]
    groups["G2_young"] = [e for e in g2
                          if (e["D"]["age"] or 999) <= THRESH["D"]["young"]
                          or (e["W"]["age"] or 999) <= THRESH["W"]["young"]
                          or (e["M"]["age"] or 999) <= THRESH["M"]["young"]]
    groups["G2_mature"] = [e for e in g2
                           if (e["D"]["age"] or 0) >= THRESH["D"]["mature"]
                           and (e["W"]["age"] or 0) >= THRESH["W"]["mature"]
                           and (e["M"]["age"] or 0) >= THRESH["M"]["mature"]]

    # ─── H: Internal disagreement (info-only) ─────────────────────────────
    groups["H1_bull_stack_cracking"] = [e for e in rows.values()
                    if R(e,"M","g") and R(e,"W","g") and R(e,"D","r")]
    groups["H2_swing_rolled"] = [e for e in rows.values()
                    if R(e,"M","g") and R(e,"W","r")]
    groups["H3_bottoms_forming"] = [e for e in rows.values()
                    if R(e,"M","r") and R(e,"D","g")]
    groups["H4_tops_forming"] = [e for e in rows.values()
                    if R(e,"M","g") and F(e,"D","r") and (e["D"]["dsg"] or 0) >= 30]

    # ─── F: aggregate breadth ─────────────────────────────────────────────
    U = max(1, len(rows))
    def pct(n): return round(100 * n / U, 1)
    breadth = {
        "universe_size":    len(rows),
        "pct_M_green":      pct(sum(1 for e in rows.values() if R(e,"M","g"))),
        "pct_M_red":        pct(sum(1 for e in rows.values() if R(e,"M","r"))),
        "pct_MW_green":     pct(sum(1 for e in rows.values() if R(e,"M","g") and R(e,"W","g"))),
        "pct_MW_red":       pct(sum(1 for e in rows.values() if R(e,"M","r") and R(e,"W","r"))),
        "pct_full_bull":    pct(len(g1)),
        "pct_full_bear":    pct(len(g2)),
        "D_fires_g_today":  sum(1 for e in rows.values() if F(e,"D","g")),
        "D_fires_r_today":  sum(1 for e in rows.values() if F(e,"D","r")),
        "W_fires_g_today":  sum(1 for e in rows.values() if F(e,"W","g")),
        "W_fires_r_today":  sum(1 for e in rows.values() if F(e,"W","r")),
        "M_fires_g_today":  sum(1 for e in rows.values() if F(e,"M","g")),
        "M_fires_r_today":  sum(1 for e in rows.values() if F(e,"M","r")),
    }

    # Sort each group: strongest M trend strength first, fall back to mcap
    def _sort(e):
        ts = e["M"]["ts"]
        return (-(ts if ts is not None else -1), -(e.get("mcap") or 0))
    for k in groups:
        groups[k].sort(key=_sort)

    # group_meta: each key → {name, kind, rule, summary}
    #   name    — human-readable display name
    #   kind    — 'state' (where regime stack stands; no fire required) or
    #             'event' (something flipped today)
    #   rule    — the precise predicate restated in plain English
    #   summary — what it means / when to use
    group_meta = {
        # ── EVENT: confluence longs (backbone + fresh trigger) ────────────
        "A1": {
            "name": "Sniper Long",
            "kind": "event",
            "rule": "Monthly and weekly are both in green regime, and the daily fires green today.",
            "summary": "The canonical 3-timeframe confluence entry — backbone up, swing up, today's daily flip is your fresh trigger. Highest-probability long setup.",
        },
        "A4_premium": {
            "name": "Premium Sniper",
            "kind": "event",
            "rule": "A Sniper Long where the monthly Trend Strength is ≥60.",
            "summary": "Sniper Long filtered to the strongest backbones. Removes weak 'technically aligned' names.",
        },
        "A2": {
            "name": "Weekly Trigger Long",
            "kind": "event",
            "rule": "Monthly in green regime, weekly fires green today, daily already in green regime.",
            "summary": "Slower, stronger confirmation than A1 — weekly flips are rarer and more durable. The trend has more room to run.",
        },
        "A3": {
            "name": "Early Weekly Trigger",
            "kind": "event",
            "rule": "Monthly in green regime, weekly fires green today, daily neutral (no fire).",
            "summary": "Weekly just turned but daily hasn't joined yet. Earlier than A2 — better price, slightly more risk.",
        },
        # ── EVENT: same-day cascades ──────────────────────────────────────
        "B1_triple_green": {
            "name": "Triple Green Cascade",
            "kind": "event",
            "rule": "Daily, weekly, AND monthly all fire green on the same bar.",
            "summary": "Major regime shift across every timeframe simultaneously. Extremely rare, very loud — worth a dedicated alert.",
        },
        "B2_WM_green_D_already": {
            "name": "Backbone + Swing Establishing",
            "kind": "event",
            "rule": "Weekly AND monthly fire green today; daily already in green regime.",
            "summary": "The macro and swing trends are being established THIS bar. Stronger than A2 because the M backbone is fresh.",
        },
        "B3_DW_green_M_already": {
            "name": "Daily + Weekly Burst (Bull Macro)",
            "kind": "event",
            "rule": "Daily AND weekly fire green today; monthly was already in green regime.",
            "summary": "Inside an existing bull macro, both faster timeframes flip together. Sharp swing-trade entry.",
        },
        "B4_DW_green_counter_macro": {
            "name": "Counter-Macro Burst",
            "kind": "event",
            "rule": "Daily AND weekly fire green; monthly NOT in green regime.",
            "summary": "Faster TFs flip up against a non-bull macro. Short-term mean-reversion trade only — lower conviction.",
        },
        # ── EVENT: pullback / dip-buy ─────────────────────────────────────
        "D2_regreen": {
            "name": "Dip-Buy Completion",
            "kind": "event",
            "rule": "Monthly and weekly in green regime, daily fires green today, with a daily red fire within the last 10 bars.",
            "summary": "The pullback finished and the trend resumed. A Sniper Long at a better price because you waited for the dip.",
        },
        "D1_pullback": {
            "name": "Pullback in Bull Stack",
            "kind": "event",
            "rule": "Monthly and weekly in green regime; daily fires red today.",
            "summary": "Healthy retrace inside a trend. Not an entry alone — watch this ticker for re-green (D2) at better price.",
        },
        # ── EVENT: early bottoms ──────────────────────────────────────────
        "C2_D_green_W_recently_g": {
            "name": "Weekly Turned, Daily Confirms",
            "kind": "event",
            "rule": "Monthly still red, weekly flipped green within last 5 bars, daily fires green today.",
            "summary": "Two of three aligning bullish before the macro turns. Second-derivative bottom signal — confluence is building.",
        },
        "C1_first_D_green_deep_red": {
            "name": "First Green After a Deep Red",
            "kind": "event",
            "rule": "Monthly and weekly in red regime, daily fires green today, prior daily-red regime was at least 60 bars deep.",
            "summary": "First daily green after months of selling pressure. Early bottom-fishing — low hit rate, big payoff when right.",
        },
        # ── EVENT: tops / risk ────────────────────────────────────────────
        "C3_first_D_red_deep_green": {
            "name": "First Red After a Long Bull",
            "kind": "event",
            "rule": "Monthly and weekly in green regime, daily fires red today, prior daily-green regime was at least 60 bars deep.",
            "summary": "First daily red after a sustained green run inside a bull. Doesn't mean exit — means tighten stops / take partials. Critical risk signal.",
        },
        "H4_tops_forming": {
            "name": "Top Warning — Daily Red After Long Bull",
            "kind": "event",
            "rule": "Monthly in green regime, daily fires red today, with prior daily-green regime ≥30 bars.",
            "summary": "Softer cousin of C3 — same idea (first daily red in a bull) with a less strict threshold and no W[g] requirement.",
        },
        # ── EVENT: shorts (bearish mirrors) ───────────────────────────────
        "E1_stack_red_D_trigger": {
            "name": "Sniper Short",
            "kind": "event",
            "rule": "Monthly and weekly in red regime; daily fires red today.",
            "summary": "Mirror of Sniper Long. Highest-probability short trigger in an established bear.",
        },
        "E2_triple_red": {
            "name": "Triple Red Cascade",
            "kind": "event",
            "rule": "Daily, weekly, AND monthly all fire red on the same bar.",
            "summary": "Major bear regime shift across every timeframe. Mirror of B1.",
        },
        "E3_DW_red_M_already": {
            "name": "Daily + Weekly Burst (Bear Macro)",
            "kind": "event",
            "rule": "Daily AND weekly fire red today; monthly already in red regime.",
            "summary": "Sharp short-side momentum inside an existing bear macro. Mirror of B3.",
        },
        # ── STATE: full alignment ─────────────────────────────────────────
        "G1_bull_stack": {
            "name": "Full Bull Stack",
            "kind": "state",
            "rule": "Monthly, weekly, AND daily all currently in green regime (any age).",
            "summary": "The healthiest names — long-only buy universe. Use this to hold winners rather than chase entries.",
        },
        "G1_young": {
            "name": "Fresh Bull Stack",
            "kind": "state",
            "rule": "Full Bull Stack where at least one TF flipped recently (D ≤ 5 bars, W ≤ 4, M ≤ 2).",
            "summary": "The stack just came together. Likely to have more upside ahead of it than an established stack.",
        },
        "G1_mature": {
            "name": "Established Bull Stack",
            "kind": "state",
            "rule": "Full Bull Stack where every TF is well past its flip (D ≥ 20, W ≥ 12, M ≥ 6).",
            "summary": "Stable, long-running bull. Use for momentum continuation rather than fresh entries.",
        },
        "G1_premium": {
            "name": "Premium Bull Stack",
            "kind": "state",
            "rule": "Full Bull Stack with monthly Trend Strength ≥ 60 AND weekly TS ≥ 60.",
            "summary": "Quality filter — keeps stacks where both higher TFs are showing real momentum, not just technical alignment.",
        },
        "G2_bear_stack": {
            "name": "Full Bear Stack",
            "kind": "state",
            "rule": "Monthly, weekly, AND daily all currently in red regime.",
            "summary": "The weakest names. Short universe or simply 'avoid going long.' Mirror of G1.",
        },
        "G2_young": {
            "name": "Fresh Bear Stack",
            "kind": "state",
            "rule": "Full Bear Stack where ≥1 TF flipped recently.",
            "summary": "Stack just came together — typically more downside ahead than an established bear stack.",
        },
        "G2_mature": {
            "name": "Established Bear Stack",
            "kind": "state",
            "rule": "Full Bear Stack with every TF well past its flip.",
            "summary": "Long-running bear. For continuation rather than fresh entries.",
        },
        # ── STATE: internal disagreement ──────────────────────────────────
        "H1_bull_stack_cracking": {
            "name": "Daily Currently Red in Bull Stack",
            "kind": "state",
            "rule": "Monthly and weekly in green regime; daily in red regime (not necessarily today's flip).",
            "summary": "The state version of D1. A bull stack with the daily currently against — pullback-watch pool.",
        },
        "H2_swing_rolled": {
            "name": "Swing Rolled, Backbone Holding",
            "kind": "state",
            "rule": "Monthly in green regime; weekly in red regime.",
            "summary": "The weekly has already turned even though the monthly is still up. More serious than H1 — close watch.",
        },
        "H3_bottoms_forming": {
            "name": "Macro Red, Daily Green",
            "kind": "state",
            "rule": "Monthly in red regime; daily in green regime.",
            "summary": "Macro hasn't turned but the daily already has. Early bottom-watch list — pre-confluence.",
        },
    }

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source_updated_at": src.get("updated_at"),
        "threshold_b": src.get("threshold_b"),
        "universe_size": len(rows),
        "breadth": breadth,
        "group_meta": group_meta,
        "groups": {k: v for k, v in groups.items()},
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Saved: {OUT_PATH}")
    print(f"Universe: {len(rows)}  |  M-green: {breadth['pct_M_green']}%  |  Full-bull stack: {breadth['pct_full_bull']}%  |  Full-bear stack: {breadth['pct_full_bear']}%")
    print("\nGroup counts (non-empty):")
    for k in sorted(groups.keys()):
        n = len(groups[k])
        if n:
            print(f"  {k:32s} {n}")

    # ── Supabase dual-write ─────────────────────────────────────────────
    finished_at = out["updated_at"]

    # 1) setup_groups (metadata)
    sb.upsert(
        "setup_groups",
        [{"group_key": k, "name": v.get("name"), "kind": v.get("kind"),
          "rule": v.get("rule"), "summary": v.get("summary"),
          "updated_at": finished_at}
         for k, v in group_meta.items()],
        on_conflict="group_key",
    )

    # 2) setup_hits (replace fully — wipe then upsert this run's hits)
    sb.truncate("setup_hits")
    hit_rows = []
    for gk, entries in groups.items():
        for e in entries:
            hit_rows.append({
                "symbol": e["sym"], "group_key": gk,
                "name": e.get("name") or "", "mcap": e.get("mcap") or 0,
                "m_ts": (e["M"].get("ts") if e.get("M") else None),
                "d_regime": e["D"]["regime"], "w_regime": e["W"]["regime"], "m_regime": e["M"]["regime"],
                "d_age": e["D"]["age"], "w_age": e["W"]["age"], "m_age": e["M"]["age"],
                "d_fired_g": e["D"]["fired_g"], "w_fired_g": e["W"]["fired_g"], "m_fired_g": e["M"]["fired_g"],
                "d_fired_r": e["D"]["fired_r"], "w_fired_r": e["W"]["fired_r"], "m_fired_r": e["M"]["fired_r"],
                "scanned_at": finished_at,
            })
    n_hits = sb.upsert("setup_hits", hit_rows, on_conflict="symbol,group_key")

    # 3) breadth_snapshots (append-only history)
    b = breadth
    sb.upsert(
        "breadth_snapshots",
        [{
            "scanned_at": finished_at,
            "threshold_b": src.get("threshold_b"),
            "universe_size": len(rows),
            "pct_m_green": b["pct_M_green"], "pct_m_red": b["pct_M_red"],
            "pct_mw_green": b["pct_MW_green"], "pct_mw_red": b["pct_MW_red"],
            "pct_full_bull": b["pct_full_bull"], "pct_full_bear": b["pct_full_bear"],
            "d_fires_g_today": b["D_fires_g_today"], "d_fires_r_today": b["D_fires_r_today"],
            "w_fires_g_today": b["W_fires_g_today"], "w_fires_r_today": b["W_fires_r_today"],
            "m_fires_g_today": b["M_fires_g_today"], "m_fires_r_today": b["M_fires_r_today"],
        }],
        on_conflict="scanned_at",
    )

    sb.record_run("setups_classify", src.get("updated_at", finished_at), finished_at,
                  True, n_hits, f"{len(rows)} universe, {n_hits} hits across {sum(1 for v in groups.values() if v)} groups")
    if n_hits:
        print(f"Supabase: upserted {n_hits} setup_hits + group metadata + breadth snapshot")


if __name__ == "__main__":
    main()
