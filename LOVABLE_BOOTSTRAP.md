# Lovable bootstrap — paste this into a new Lovable project

> Hand the contents of this file (everything below the `---`) to the
> Lovable AI on a fresh project. It contains the full product spec, the
> Supabase schema reference, and the page-by-page build plan.

---

# THT Dual Scan — product spec for Lovable

Build a multi-timeframe equities scanner dashboard. The math is already
done — your job is the UI and the auth/billing layer. All data lives in
Supabase (already provisioned and seeded by an external Python pipeline).

## Brand & feel

- **Name:** THT Dual Scan
- **Tone:** Trader-precise, terminal-dark, monospace-numeric, zero
  marketing fluff. No emojis in product copy.
- **Palette (dark):**
  - bg `#0a0a0a`, fg `#e8e8e8`, dim `#888`, line `#222`
  - green `#00ffbb`, green-dim `#1a4a3a`
  - red `#ff4040`, red-dim `#4a1a1a`
  - table-bg `#111`, row-alt `#161616`
- **Typeface:** system stack for body, `ui-monospace, Menlo, monospace`
  for symbols / numbers / regime badges.

## Auth & billing

- Supabase Auth — email/password and GitHub.
- Stripe via Lovable's Stripe integration. Two tiers:
  - **Free** — sees Bands/BX flips, ATH/ATL.
  - **Pro** (£29/mo) — adds LuxAlgo per-TF, **Setups** view, the hidden
    extra-data panel, and 365 days of breadth history.
- Gate premium routes server-side via a `user_tier` column on the user
  profile.

## Connect to Supabase

Use the project the Python pipelines write to. Required env:

- `VITE_SUPABASE_URL` — project URL
- `VITE_SUPABASE_ANON_KEY` — anon key (NOT service_role — that one
  belongs to the scans only)

## Data model (read-only from the app's POV)

| Table | What's in it | Notes |
|---|---|---|
| `universe` | The 1,470 tickers we scan, `symbol, name, mcap_b, sector, industry` | PK `symbol`. |
| `regime_state` | Per-(symbol, tf) current regime — `regime g/r`, `dsg`, `dsr`, `ts`, `fired_*` booleans | PK `(symbol, tf)`. `tf` ∈ `'D','W','M'`. |
| `setup_groups` | Metadata for setup categories — `group_key, name, kind, rule, summary` | `kind` ∈ `'state','event'`. |
| `setup_hits` | Which tickers match which group — flattened ticker+group rows with full per-TF regime info | Subscribe via realtime. |
| `breadth_snapshots` | One row per classifier run — universe-level percentages | Append-only history. Use for the breadth sparkline. |
| `flip_results` | The 5-min Yahoo scan output: Bands and BX greens/reds per timeframe | PK `(timeframe, product, side, symbol)`. |
| `extrema_events` | ATH/ATL today + rolling accumulator | `is_today` boolean splits the two. |
| `scan_runs` | Pipeline lineage (when each scan last ran, ok status) | Surface on a /status page. |

## Pages

### `/` — Live tape (free)
Three top tabs: **Daily / Weekly / Monthly**. Beneath: filter row with
min-market-cap input (default $3.5B; preset chips for $3.5/10/50/200/1T).

Four stacked sections for the active timeframe (data from `flip_results`):

1. Bands flipped GREEN today
2. Bands flipped RED today
3. BX flipped GREEN today
4. BX flipped RED today

Columns: `# | Symbol | Name | Mcap | Price | Basis | BX | Streak`.
Click a row to expand an inline TradingView lightweight-chart of the
ticker (use `lightweight-charts@4.2.0`).

### `/bands` and `/bx` (free)
Same shell as `/` but filtered to a single product. The Daily/Weekly/
Monthly tabs still apply.

### `/extrema` (free)
Single section: stocks making new ATH (or ATL — radio toggle) today,
with a "rolling accumulator" sub-section of recent makers. Data from
`extrema_events`.

### `/lux` (Pro)
LuxAlgo per-timeframe greens and reds. Single source: `setup_hits` is
not for this — query `regime_state` directly:
- Green for TF X = rows where `tf=X` AND `fired_g=true` (today's fire).
- Red for TF X = rows where `tf=X` AND `fired_r=true`.

### `/setups` (Pro) — the marquee view
Two top-level sections:

#### State — where the regime stack stands right now
Subscribe to `setup_hits` filtered to rows where `setup_groups.kind = 'state'`.
Display in this order, each as its own group block:

1. Full Bull Stack (`G1_bull_stack`)
2. Premium Bull Stack (`G1_premium`)
3. Fresh Bull Stack (`G1_young`)
4. Established Bull Stack (`G1_mature`)
5. Daily Currently Red in Bull Stack (`H1_bull_stack_cracking`)
6. Swing Rolled, Backbone Holding (`H2_swing_rolled`)
7. Macro Red, Daily Green (`H3_bottoms_forming`)
8. Full Bear Stack (`G2_bear_stack`)
9. Fresh Bear Stack (`G2_young`)
10. Established Bear Stack (`G2_mature`)

#### Event — what just flipped today
Same pattern, `kind = 'event'`, this order:

11. Sniper Long (`A1`)
12. Premium Sniper (`A4_premium`)
13. Weekly Trigger Long (`A2`)
14. Early Weekly Trigger (`A3`)
15. Triple Green Cascade (`B1_triple_green`)
16. Backbone + Swing Establishing (`B2_WM_green_D_already`)
17. Daily + Weekly Burst (Bull Macro) (`B3_DW_green_M_already`)
18. Counter-Macro Burst (`B4_DW_green_counter_macro`)
19. Dip-Buy Completion (`D2_regreen`)
20. Pullback in Bull Stack (`D1_pullback`)
21. Weekly Turned, Daily Confirms (`C2_D_green_W_recently_g`)
22. First Green After a Deep Red (`C1_first_D_green_deep_red`)
23. First Red After a Long Bull (`C3_first_D_red_deep_green`)
24. Top Warning — Daily Red After Long Bull (`H4_tops_forming`)
25. Sniper Short (`E1_stack_red_D_trigger`)
26. Triple Red Cascade (`E2_triple_red`)
27. Daily + Weekly Burst (Bear Macro) (`E3_DW_red_M_already`)

Each group block:
```
[Name]   [count pill]   [GROUP_KEY badge]
{summary from setup_groups.summary — 2 sentences}
Rule: {setup_groups.rule}
[table]
```

Table columns for setup_hits:
`# | Symbol | Name | Mcap | M | W | D | M·TS`

Render M/W/D as colored regime badges: green/red letter (G/R) + small
bar-age subscript + a ▲/▼ arrow if `*_fired_g` / `*_fired_r` is true on
that TF.

Above everything, a **breadth strip** of pills (latest row from
`breadth_snapshots`):
- Universe size
- M[g] / M[r] %
- M+W[g] %
- Full bull stack %
- Full bear stack %
- D fires today (g/r)

### `/setups` hidden affordance
Place an "extra-data" trigger on the letter **D** of "Dual Scan" in the
header. On click, open a floating panel showing the latest
`breadth_snapshots` row + top-3 picks per group (G1, G2, A1, B1) +
the full rule reference table from `setup_groups`. Esc to close.

### `/cot` (Pro) — Positioning Monitor
Port the existing static CFTC COT page. Source files are in
`piraci26/tht-dashboard/docs/cot.{html,css,js,-index.pine}` —
copy verbatim; it's self-contained.

### `/status` (free)
Diagnostics page. Lists every pipeline from `scan_runs`:
- `scan_py` — last run, ok status, age in seconds.
- `lux_scan_full` — last run, ok, age (will be hours since it's daily).
- `regime_scan` — same.
- `setups_classify` — same.

Color any pipeline red if its age > 2× its expected cadence (scan_py >
10 min, regime_scan > 30 hours).

### `/account`, `/pricing`, `/login` — standard.

## Realtime

Subscribe these tables to Supabase realtime:
- `flip_results` (filter by active timeframe in the UI)
- `setup_hits`
- `breadth_snapshots`
- `regime_state` (only used by /lux; subscribe filtered to the active tf)

On insert/update, refresh the affected component. No more 5-min polling.

## What you do NOT need to build
- The math pipelines (Python, run externally, populate Supabase).
- The Pine sources (delivered as static download from /cot).
- The Yahoo data fetchers.

## Build order

1. Auth + Supabase connection + the four free pages (`/`, `/bands`,
   `/bx`, `/extrema`).
2. `/status` — quick to ship and immediately useful.
3. Stripe + the `user_tier` gate.
4. `/setups` — the marquee Pro feature.
5. `/lux`.
6. `/cot` (static port).

Start there.
