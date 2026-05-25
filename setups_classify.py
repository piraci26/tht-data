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
import json, os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
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

    group_meta = {
        "A1": "Stack + D trigger — M[g] W[g] D[g!]",
        "A2": "Stack + W trigger — M[g] W[g!] D[g]",
        "A3": "Stack + W trigger, D neutral — M[g] W[g!] D[-]",
        "A4_premium": "A1 with strong M backbone (M TS ≥ 60)",
        "B1_triple_green": "Triple flip green — D[g!] W[g!] M[g!]",
        "B2_WM_green_D_already": "W+M flip green, D already green",
        "B3_DW_green_M_already": "D+W flip green inside existing M[g]",
        "B4_DW_green_counter_macro": "D+W flip green, M not green (counter-macro)",
        "C1_first_D_green_deep_red": "First D-green after a deep red structure (M[r] W[r], prior D-red ≥60)",
        "C2_D_green_W_recently_g": "D fires green, W flipped green within 5 bars, M[r]",
        "C3_first_D_red_deep_green": "First D-red in a deep green structure (top warning, prior D-green ≥60)",
        "D1_pullback": "M[g] W[g] D[r!] — pullback in trend (watchlist)",
        "D2_regreen": "M[g] W[g] D[g!] with D-red within last 10 bars (dip-buy completion)",
        "E1_stack_red_D_trigger": "Stack red + D fires red — short trigger",
        "E2_triple_red": "Triple flip red — D[r!] W[r!] M[r!]",
        "E3_DW_red_M_already": "D+W flip red inside existing M[r]",
        "G1_bull_stack": "Full bull stack — M[g] W[g] D[g], any age",
        "G1_young": "G1 with ≥1 TF freshly flipped (D≤5, W≤4, M≤2)",
        "G1_mature": "G1 with all TFs well past their flip (D≥20, W≥12, M≥6)",
        "G1_premium": "G1 with M TS ≥ 60 AND W TS ≥ 60",
        "G2_bear_stack": "Full bear stack — M[r] W[r] D[r]",
        "G2_young": "G2 with ≥1 TF freshly flipped",
        "G2_mature": "G2 with all TFs well past their flip",
        "H1_bull_stack_cracking": "M[g] W[g] D[r] — daily rolled inside bull stack",
        "H2_swing_rolled": "M[g] W[r] — weekly turned, backbone holding",
        "H3_bottoms_forming": "M[r] D[g] — macro red, daily already green",
        "H4_tops_forming": "M[g] D[r!] with prior green ≥30 bars",
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


if __name__ == "__main__":
    main()
