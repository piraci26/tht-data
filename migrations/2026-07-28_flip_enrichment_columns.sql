-- The Bands/BX flip tables render ATH / % to ATH / 52w High-Low / New 30d
-- columns per row. The legacy results.json carried these on every row; the
-- Supabase flip_results table never had them, so the dashboard drew dashes.
-- scan.py now sends them (dropped with a warning until this is applied).
--
-- Safe to re-run.

alter table flip_results
  add column if not exists ath         numeric,
  add column if not exists atl         numeric,
  add column if not exists pct_to_ath  numeric,
  add column if not exists pct_to_atl  numeric,
  add column if not exists wk52_high   numeric,
  add column if not exists wk52_low    numeric,
  add column if not exists ath_30d     integer,
  add column if not exists atl_30d     integer;
