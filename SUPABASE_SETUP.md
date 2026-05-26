# Supabase migration — your one-time setup steps

The Python side is already wired (`supabase_client.py`, dual-write in
`scan.py`, `regime_scan.py`, `setups_classify.py`, `.github/workflows/scan.yml`).
All writes are no-ops until two env vars are set, so the existing JSON
pipeline keeps running unchanged during the migration.

This document is your manual checklist. ~15 minutes total.

---

## 1. Create the Supabase project

1. Go to https://supabase.com → **Start your project** → log in with GitHub.
2. **New project** → name it `tht-dual-scan` (or anything).
   - Region: **closest to you** (London → `eu-west-2` London).
   - Password: generate a strong one and save it in 1Password.
   - Plan: **Free** is enough — you'll use <500 MB and <50k row reads/day.
3. Wait ~2 min for provisioning.

## 2. Run the schema

1. In the Supabase dashboard → **SQL Editor** → **New query**.
2. Paste the entire contents of `supabase_schema.sql` (in this repo).
3. **Run**. You should see "Success. No rows returned."
4. Sanity check: **Table Editor** in the left nav should now show
   `universe`, `regime_state`, `setup_groups`, `setup_hits`,
   `breadth_snapshots`, `flip_results`, `extrema_events`, `scan_runs`.

## 3. Grab the two keys

From **Settings → API** in the Supabase dashboard:

- **Project URL** — looks like `https://abcdefghijk.supabase.co`
- **service_role secret** — under "Project API keys". **NOT the `anon` key.**
  The service_role key bypasses RLS — keep it secret.

## 4. Local pipelines — `.env` file

Create `~/tht-data/.env` (this dir's `.gitignore` already excludes `.env`):

```
SUPABASE_URL=https://abcdefghijk.supabase.co
SUPABASE_SERVICE_KEY=eyJh...long...key
```

Source it before manual scans:

```sh
set -a; source .env; set +a
python3 regime_scan.py --threshold 3.5 --limit 50 --tfs D
python3 setups_classify.py
```

Both will print `Supabase: upserted N rows` when the env is loaded; nothing
when it isn't.

## 5. GitHub Actions — add the secrets

So `scan.py` running on Actions also writes to Supabase:

1. https://github.com/piraci26/tht-data → **Settings → Secrets and
   variables → Actions → New repository secret**.
2. Add both:
   - Name `SUPABASE_URL`, value: your project URL.
   - Name `SUPABASE_SERVICE_KEY`, value: your service_role key.
3. The next scheduled scan picks them up automatically (the workflow
   already references `secrets.SUPABASE_URL` / `secrets.SUPABASE_SERVICE_KEY`).

## 6. Verify

After a manual scan or the next GH Action run:

- **Table Editor → regime_state** should have rows.
- **Table Editor → scan_runs** should show recent entries with `ok=true`.
- **Realtime → Inspector** should show change events when a new scan lands.

If nothing appears, the most likely cause is the service_role key being
wrong (anon key by mistake) — check Settings → API in Supabase.

## 7. Lovable

Hand the Lovable AI the contents of `LOVABLE_BOOTSTRAP.md` (next door to
this file). It contains the full spec, schema reference, and starter
prompts to build the new dashboard against this Supabase project.

---

## Rollback / safety notes

- The JSON dual-write is **not removed**. Even with Supabase active, the
  existing GH Pages dashboards keep working from the JSON files.
- To pause Supabase writes without code changes: unset the env vars (or
  remove the GH Actions secrets). All scans fall back to JSON-only.
- The `service_role` key has full DB power. Never paste it into the
  browser, never commit it. Rotate it from Supabase Settings → API if it
  ever leaks.
