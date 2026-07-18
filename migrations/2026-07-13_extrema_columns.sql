-- Add the five columns the ATH/ATL table renders but extrema_events never had.
-- Without these, Today's High, % to ATH, 52w High/Low and New ATHs 30d render
-- as em-dashes. scan.py already computes all of them into ath_list.json.
--
-- Columns are kind-generic: an "extreme" is the ATH for kind='ath' and the ATL
-- for kind='atl'. The dashboard adapter maps them back to the ath_*/atl_* keys.
--
-- Safe to re-run.

alter table extrema_events
  add column if not exists today_extreme  numeric,   -- today's high (ath) / low (atl)
  add column if not exists pct_to_extreme numeric,   -- % from last close to the extreme
  add column if not exists wk52_high      numeric,
  add column if not exists wk52_low       numeric,
  add column if not exists extreme_30d    integer;   -- new ATHs/ATLs in last 30d
