-- THT Dual Scan — Supabase schema
-- Run this once in your Supabase project's SQL editor.
-- All tables are wiped + re-populated on each scan (upsert by primary key).
-- Realtime is enabled per-table at the bottom.

-- ────────────────────────────────────────────────────────────────────────
-- 1. UNIVERSE (the watchlist — currently 1,470 tickers @ $3.5B+)
-- ────────────────────────────────────────────────────────────────────────
create table if not exists universe (
  symbol       text primary key,
  name         text,
  mcap_b       numeric,
  threshold_b  numeric,
  sector       text,
  industry     text,
  updated_at   timestamptz not null default now()
);

-- ────────────────────────────────────────────────────────────────────────
-- 2. REGIME STATE (lux_scan_full + regime_scan output)
--    One row per (symbol × timeframe) — overwritten each scan.
-- ────────────────────────────────────────────────────────────────────────
create table if not exists regime_state (
  symbol         text not null,
  tf             text not null check (tf in ('D','W','M')),
  regime         text check (regime in ('g','r')),  -- null = unknown
  dsg            numeric,                            -- Days Since Green
  dsr            numeric,                            -- Days Since Red
  ts             numeric,                            -- Trend Strength 0-100
  fired_g        boolean default false,
  fired_g_plus   boolean default false,
  fired_r        boolean default false,
  fired_r_plus   boolean default false,
  name           text,
  mcap           numeric,
  scanned_at     timestamptz not null default now(),
  primary key (symbol, tf)
);
create index if not exists regime_state_scanned_at_idx on regime_state(scanned_at desc);

-- ────────────────────────────────────────────────────────────────────────
-- 3. SETUP GROUPS (metadata — name, kind, rule, summary per group key)
--    Replaced on each classifier run.
-- ────────────────────────────────────────────────────────────────────────
create table if not exists setup_groups (
  group_key   text primary key,
  name        text,
  kind        text check (kind in ('state','event')),
  rule        text,
  summary     text,
  updated_at  timestamptz not null default now()
);

-- ────────────────────────────────────────────────────────────────────────
-- 4. SETUP HITS (per-classifier output — which tickers match which groups)
--    Replaced on each classifier run.
-- ────────────────────────────────────────────────────────────────────────
create table if not exists setup_hits (
  symbol       text not null,
  group_key    text not null references setup_groups(group_key) on delete cascade,
  name         text,
  mcap         numeric,
  m_ts         numeric,
  d_regime     text, w_regime text, m_regime text,
  d_age        numeric, w_age numeric, m_age numeric,
  d_fired_g    boolean, w_fired_g boolean, m_fired_g boolean,
  d_fired_r    boolean, w_fired_r boolean, m_fired_r boolean,
  scanned_at   timestamptz not null default now(),
  primary key (symbol, group_key)
);
create index if not exists setup_hits_group_idx on setup_hits(group_key);
create index if not exists setup_hits_mcap_idx  on setup_hits(mcap desc);

-- ────────────────────────────────────────────────────────────────────────
-- 5. BREADTH SNAPSHOTS (one row per setups_classify run — append-only)
-- ────────────────────────────────────────────────────────────────────────
create table if not exists breadth_snapshots (
  scanned_at        timestamptz primary key default now(),
  threshold_b       numeric,
  universe_size     integer,
  pct_m_green       numeric,
  pct_m_red         numeric,
  pct_mw_green      numeric,
  pct_mw_red        numeric,
  pct_full_bull     numeric,
  pct_full_bear     numeric,
  d_fires_g_today   integer,
  d_fires_r_today   integer,
  w_fires_g_today   integer,
  w_fires_r_today   integer,
  m_fires_g_today   integer,
  m_fires_r_today   integer
);
-- Keep ~365 days of breadth history; older rows can be aged out by a cron.

-- ────────────────────────────────────────────────────────────────────────
-- 6. FLIP RESULTS (the existing Yahoo Bands/BX scan output)
--    Replaced on each scan.py run.
-- ────────────────────────────────────────────────────────────────────────
create table if not exists flip_results (
  timeframe   text not null check (timeframe in ('daily','weekly','monthly')),
  product     text not null check (product in ('fvb','bxt')),
  side        text not null check (side  in ('green','red')),
  symbol      text not null,
  name        text,
  mcap        numeric,
  price       numeric,
  basis       numeric,
  streak      numeric,
  bxt_today   numeric,
  ath         numeric,
  atl         numeric,
  pct_to_ath  numeric,
  pct_to_atl  numeric,
  wk52_high   numeric,
  wk52_low    numeric,
  ath_30d     integer,
  atl_30d     integer,
  scanned_at  timestamptz not null default now(),
  primary key (timeframe, product, side, symbol)
);

-- ────────────────────────────────────────────────────────────────────────
-- 7. ATH / ATL EVENTS (today's makers + the rolling accumulator)
-- ────────────────────────────────────────────────────────────────────────
-- Columns are kind-generic (an "extreme" is the ATH for kind='ath', the ATL for
-- kind='atl'); the adapter maps them back to the ath_*/atl_* keys the renderer
-- uses. Everything here is already computed by scan.py's run_ath_atl_universe.
create table if not exists extrema_events (
  kind           text not null check (kind in ('ath','atl')),
  is_today       boolean not null default false,
  symbol         text not null,
  name           text,
  mcap           numeric,
  price          numeric,                -- last close
  ath_or_atl     numeric,                -- the extreme value reached
  today_extreme  numeric,                -- today's high (ath) / low (atl)
  pct_to_extreme numeric,                -- % from last close to the extreme
  wk52_high      numeric,
  wk52_low       numeric,
  extreme_30d    integer,                -- new ATHs/ATLs printed in last 30d
  occurred_at    timestamptz,
  scanned_at     timestamptz not null default now(),
  primary key (kind, is_today, symbol)
);

-- ────────────────────────────────────────────────────────────────────────
-- 8. SCAN RUNS (lineage / observability — when did each pipeline last run)
-- ────────────────────────────────────────────────────────────────────────
create table if not exists scan_runs (
  pipeline    text not null check (pipeline in ('scan_py','lux_scan_full','regime_scan','setups_classify')),
  started_at  timestamptz not null,
  finished_at timestamptz,
  ok          boolean,
  rows_written integer,
  notes       text,
  primary key (pipeline, started_at)
);
create index if not exists scan_runs_recent on scan_runs(pipeline, started_at desc);

-- ────────────────────────────────────────────────────────────────────────
-- ROW-LEVEL SECURITY — public read, service-role write
-- ────────────────────────────────────────────────────────────────────────
-- Lovable's anon key can SELECT; the scan pipelines use the service_role
-- key (kept in env vars, never exposed to the browser) to upsert.
alter table universe          enable row level security;
alter table regime_state      enable row level security;
alter table setup_groups      enable row level security;
alter table setup_hits        enable row level security;
alter table breadth_snapshots enable row level security;
alter table flip_results      enable row level security;
alter table extrema_events    enable row level security;
alter table scan_runs         enable row level security;

-- Permissive read for everyone (anon + authenticated). Premium gating
-- happens at the application layer in Lovable (Stripe → user_tier column).
do $$
declare
  t text;
begin
  foreach t in array array[
    'universe','regime_state','setup_groups','setup_hits',
    'breadth_snapshots','flip_results','extrema_events','scan_runs'
  ]
  loop
    execute format('drop policy if exists %1$s_read_all on %1$s', t);
    execute format('create policy %1$s_read_all on %1$s for select using (true)', t);
  end loop;
end$$;

-- ────────────────────────────────────────────────────────────────────────
-- REALTIME — publish change events on the tables the UI subscribes to
-- ────────────────────────────────────────────────────────────────────────
alter publication supabase_realtime add table regime_state;
alter publication supabase_realtime add table setup_hits;
alter publication supabase_realtime add table breadth_snapshots;
alter publication supabase_realtime add table flip_results;
alter publication supabase_realtime add table extrema_events;
