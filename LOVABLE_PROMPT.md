# Build: THT Dual Scan — a multi-timeframe equities scanner

You're building the UI for a live equities scanner. The data math is already
done by external Python pipelines that write to Supabase — your job is the
web app on top of it.

## Hard constraints (do not deviate)

1. **Supabase backend** — already provisioned and populated.
   - URL: `https://uwtxrgrydaxtglerdfxt.supabase.co`
   - Anon key: `sb_publishable_1-VR3k-YyepvDSHS4XCe4A_sbIldGMm`
   - Use `@supabase/supabase-js` with realtime subscriptions for live data.
2. **Light AND dark theme** — both must be designed properly, not just an
   inverted palette. User can toggle, and the choice persists in
   localStorage. Default to system preference (`prefers-color-scheme`).
3. **No auth.** App is open to everyone. Supabase client is anon-only.
   No `/login`, `/signup`, `/account`, no `_authenticated` layout, no
   `useAuth()`, no profiles table. Header has nav + theme toggle only.
4. **No billing.** No `/pricing`, no Stripe, no `user_tier`, no
   Free/Pro distinction. Every route is public.
5. **Mobile-responsive** — these tables are scanned on phones too.

## Design — your call

Everything visual is yours. Palette, typography, layout, component
library, motion, iconography — pick whatever lands cleanly for a
data-dense trading app. Two non-negotiables:

- Both themes must feel deliberate, not lazy inversions.
- Tabular numerics (mcap, price, %, ages) must be **monospaced/tabular**
  so they align cleanly in tables. Other text can be whatever.

Brand name to use: **THT Dual Scan**. No tagline required, but if you
write one keep it short and trader-precise (e.g. "Where the regime
breaks before the headline does"). No emojis in product copy.

## Data model — read-only from your perspective

Tables in Supabase (RLS already permissive-read for anon; Pro gating
happens in your app):

| Table | Purpose | Primary key |
|---|---|---|
| `universe` | The ticker watchlist (~1,470 names @ $3.5B+) | `symbol` |
| `regime_state` | Per-(symbol, tf) current regime: `regime g/r`, `dsg`, `dsr`, `ts`, `fired_*` booleans | `(symbol, tf)` where `tf ∈ 'D','W','M'` |
| `setup_groups` | Metadata for every setup category: `group_key, name, kind, rule, summary` | `group_key`. `kind ∈ 'state','event'`. |
| `setup_hits` | Which tickers match which group, with full per-TF info | `(symbol, group_key)`. Subscribe to realtime. |
| `breadth_snapshots` | One row per classifier run — universe-level percentages, append-only history | `scanned_at` |
| `flip_results` | The every-5-min Yahoo scan: Bands and BX greens/reds per timeframe | `(timeframe, product, side, symbol)`. Subscribe to realtime. |
| `extrema_events` | ATH/ATL today + rolling accumulator | `(kind, is_today, symbol)` |
| `scan_runs` | Pipeline lineage — when each scan last ran and was OK | `(pipeline, started_at)` |

## Pages

### `/` — Live tape (free)
Three timeframe tabs (Daily / Weekly / Monthly). Filter bar with min-
market-cap input (default $3.5B; preset chips $3.5/10/50/200/1T).
Four stacked sections fed by `flip_results` filtered to the active TF:

1. Bands flipped GREEN today
2. Bands flipped RED today
3. BX flipped GREEN today
4. BX flipped RED today

Columns: `# · Symbol · Name · Mcap · Price · Basis · BX · Streak`.
Click a row to expand an inline TradingView lightweight-chart of the
ticker (use `lightweight-charts@4.2.0`).

### `/bands` and `/bx` (free)
Same shell as `/` but filtered to a single product.

### `/extrema` (free)
Single section: stocks making new ATH (radio toggle for ATL) today,
with a "rolling accumulator" sub-section. Data: `extrema_events`.

### `/lux`
LuxAlgo per-timeframe greens/reds. Query `regime_state` filtered:
- Green for active TF = `tf=X` AND `fired_g=true`.
- Red for active TF = `tf=X` AND `fired_r=true`.

### `/setups` — the marquee view
A breadth strip at the top (latest row of `breadth_snapshots`):
universe size · M[g]% · M[r]% · M+W[g]% · full bull stack % · full bear
stack % · today's D fires green/red.

Then **two top-level sections**:

#### State — where the regime stack stands right now
`setup_groups.kind = 'state'`. Order:

1. Full Bull Stack — `G1_bull_stack`
2. Premium Bull Stack — `G1_premium`
3. Fresh Bull Stack — `G1_young`
4. Established Bull Stack — `G1_mature`
5. Daily Currently Red in Bull Stack — `H1_bull_stack_cracking`
6. Swing Rolled, Backbone Holding — `H2_swing_rolled`
7. Macro Red, Daily Green — `H3_bottoms_forming`
8. Full Bear Stack — `G2_bear_stack`
9. Fresh Bear Stack — `G2_young`
10. Established Bear Stack — `G2_mature`

#### Event — what just flipped today
`setup_groups.kind = 'event'`. Order:

11. Sniper Long — `A1`
12. Premium Sniper — `A4_premium`
13. Weekly Trigger Long — `A2`
14. Early Weekly Trigger — `A3`
15. Triple Green Cascade — `B1_triple_green`
16. Backbone + Swing Establishing — `B2_WM_green_D_already`
17. Daily + Weekly Burst (Bull Macro) — `B3_DW_green_M_already`
18. Counter-Macro Burst — `B4_DW_green_counter_macro`
19. Dip-Buy Completion — `D2_regreen`
20. Pullback in Bull Stack — `D1_pullback`
21. Weekly Turned, Daily Confirms — `C2_D_green_W_recently_g`
22. First Green After a Deep Red — `C1_first_D_green_deep_red`
23. First Red After a Long Bull — `C3_first_D_red_deep_green`
24. Top Warning — Daily Red After Long Bull — `H4_tops_forming`
25. Sniper Short — `E1_stack_red_D_trigger`
26. Triple Red Cascade — `E2_triple_red`
27. Daily + Weekly Burst (Bear Macro) — `E3_DW_red_M_already`

Each group block: name (large) · count pill · code-key (small badge)
· paragraph summary (from `setup_groups.summary`) · one-line rule
(from `setup_groups.rule`) · sortable table.

Setup_hits table columns: `# · Symbol · Name · Mcap · M · W · D · M·TS`.
Render M/W/D as colored regime badges showing the regime letter (G/R)
with a small bar-age subscript and a ▲/▼ if `*_fired_g` / `*_fired_r`.

### `/setups` — hidden affordance
Place an "extra-data" trigger somewhere unconventional but discoverable
on hover (e.g. a single letter in the page title). Clicking opens a
floating panel showing the latest `breadth_snapshots` row, top 3 picks
per group (G1, G2, A1, B1), and the full rule reference table from
`setup_groups`. Esc closes.

### `/cot` — Positioning Monitor
Port the existing static CFTC COT page. Source files are in
`github.com/piraci26/tht-dashboard/docs/cot.{html,css,js,-index.pine}`
— copy verbatim into your project; the page is self-contained.

### `/status` (free)
Diagnostics. List every pipeline from `scan_runs`:

- `scan_py` — last run, ok, age in seconds. Red if age > 10 min.
- `lux_scan_full` — last run, ok, age. Red if > 30 hours.
- `regime_scan` — last run, ok, age. Red if > 30 hours.
- `setups_classify` — last run, ok, age. Red if > 30 hours.

## Realtime

Subscribe these tables to Supabase realtime:

- `flip_results` (filter by active timeframe and product).
- `setup_hits` (full table; component re-renders affected groups).
- `breadth_snapshots` (push new rows into the strip and the history chart).
- `regime_state` (subscribe filtered to active tf when on `/lux`).

No polling. Stale-while-revalidate is fine for initial loads.

## What you do NOT build

- The math (Python pipelines run externally — you only read the tables).
- The Pine source files (served as static download from `/cot`).
- The Yahoo data fetcher.

## Build order

1. Supabase connection + theme system + the four tape pages
   (`/`, `/bands`, `/bx`, `/extrema`) + `/status`.
2. `/setups` — the marquee view.
3. `/lux`.
4. `/cot` (static port).

Start there.
