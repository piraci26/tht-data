"""
Minimal Supabase REST client for the scan pipelines.

Why not the official `supabase` Python lib?
- Zero extra deps — the official lib pulls postgrest, gotrue, realtime,
  storage, etc. We only need to POST upserts via PostgREST.
- The whole client is ~80 lines and easy to audit.

Env vars expected:
  SUPABASE_URL          — e.g. https://abcdefg.supabase.co
  SUPABASE_SERVICE_KEY  — the service_role key (NOT the anon key).
                          Never commit this. GH Actions: add as a secret.

When either is missing, all writes are no-ops and a hint is printed once.
That way the existing JSON pipeline keeps working with zero changes during
the migration; flipping the env vars on activates Supabase writes.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
from typing import Iterable

_PRINTED_DISABLED_HINT = False


def _disabled() -> bool:
    global _PRINTED_DISABLED_HINT
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")):
        if not _PRINTED_DISABLED_HINT:
            print("supabase_client: SUPABASE_URL / SUPABASE_SERVICE_KEY not set — Supabase writes skipped.", file=sys.stderr)
            _PRINTED_DISABLED_HINT = True
        return True
    return False


def _headers(prefer_resolution: str = "merge-duplicates") -> dict:
    return {
        "Content-Type": "application/json",
        "apikey": os.environ["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_KEY']}",
        # PostgREST upsert semantics: insert OR update on PK conflict.
        "Prefer": f"resolution={prefer_resolution},return=minimal",
    }


def _url(table: str, on_conflict: str | None = None) -> str:
    base = os.environ["SUPABASE_URL"].rstrip("/") + f"/rest/v1/{table}"
    if on_conflict:
        base += f"?on_conflict={on_conflict}"
    return base


def upsert(table: str, rows: list[dict], on_conflict: str | None = None, chunk: int = 500) -> int:
    """
    Upsert rows into `table`. `on_conflict` is the comma-separated PK columns
    (e.g. "symbol,tf" for regime_state). Chunks at 500 rows to stay under
    PostgREST's body-size limits. Returns total rows attempted.
    """
    if _disabled() or not rows:
        return 0
    headers = _headers()
    sent = 0
    # Columns the live table doesn't have (schema drift: code updated, migration
    # not yet run). PGRST204 names the offender; we drop it and retry so the
    # remaining columns still land instead of the whole write dying. The lost
    # columns render as em-dashes until the migration in migrations/ is applied.
    dropped: set[str] = set()
    i = 0
    while i < len(rows):
        batch = rows[i : i + chunk]
        if dropped:
            batch = [{k: v for k, v in r.items() if k not in dropped} for r in batch]
        body = json.dumps(batch).encode("utf-8")
        req = urllib.request.Request(
            _url(table, on_conflict), data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()  # discard; we asked for return=minimal
            sent += len(batch)
            i += chunk
        except urllib.error.HTTPError as e:
            body_str = e.read().decode("utf-8", "replace")
            m = re.search(r"Could not find the '([^']+)' column", body_str)
            if e.code == 400 and m and len(dropped) < 8:
                dropped.add(m.group(1))
                print(f"  [supabase] {table}: live schema is missing column "
                      f"'{m.group(1)}' — writing without it. Apply the pending "
                      f"migration in migrations/ to restore it.", file=sys.stderr)
                continue  # retry this batch minus the unknown column
            raise RuntimeError(f"Supabase upsert {table} failed: {e.code} {body_str}") from e
    return sent


def truncate(table: str, where_filter: str | None = None) -> None:
    """
    Delete-all on a table (PostgREST DELETE with no PK = delete everything).
    Use sparingly — only for tables we fully replace on each run.

    where_filter: optional PostgREST filter like "tf=eq.D" to scope deletion.
    """
    if _disabled():
        return
    headers = _headers()
    url = _url(table)
    if where_filter:
        url += "?" + where_filter
    else:
        # PostgREST refuses unfiltered DELETE; use an always-true filter.
        url += "?scanned_at=not.is.null"
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body_str = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Supabase delete {table} failed: {e.code} {body_str}") from e


def record_run(pipeline: str, started_at: str, finished_at: str, ok: bool, rows_written: int = 0, notes: str = "") -> None:
    """Idempotent observability hook — drop a row into scan_runs."""
    upsert(
        "scan_runs",
        [{
            "pipeline": pipeline,
            "started_at": started_at,
            "finished_at": finished_at,
            "ok": ok,
            "rows_written": rows_written,
            "notes": notes[:500],
        }],
        on_conflict="pipeline,started_at",
    )
