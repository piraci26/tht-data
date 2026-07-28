#!/usr/bin/env python3
"""Sync IQ Algo AI worker output into Supabase.

  reads.json   -> ticker_reads   (batch upsert, whole file every run)
  events.jsonl -> state_events   (append-only; a local byte-offset cursor file
                                  ensures each line is inserted once)

Env (required):
  SUPABASE_URL           https://<project>.supabase.co
  SUPABASE_SERVICE_KEY   service-role key — server-side only, never ship to a client

Env (optional, defaults are relative to this script's directory and match
where worker.py actually writes):
  READS_PATH        default ./out/reads.json
  EVENTS_PATH       default ./state/events.jsonl
  SYNC_STATE_PATH   default ./.sync_supabase_cursor.json

Requires: pip install supabase   (supabase-py v2)

Safe to run from cron/launchd: exits 0 when there is nothing new, advances the
events cursor only after a successful insert (a mid-run crash can at worst
re-insert the last in-flight chunk — state_events has a bigserial PK, so that
shows up as duplicate rows, not an error; keep chunks small).

Expected reads.json shape (what worker.py writes):
  {"MSFT": {"read": "...", "state_hash": "...", "updated_at": "..."}}
Also tolerated: a {"reads": {sym: {...}}} wrapper, a list of row objects, and
"read_text"/"text" in place of "read".

Expected events.jsonl lines (what worker.py writes):
  {"ts": "2026-07-28T05:00:00+00:00", "ticker": "MSFT",
   "changes": [{"field": "daily.bands", "from": null, "to": "red"}, ...]}
("sym" is accepted in place of "ticker".)
"""

import json
import os
import sys
from datetime import datetime, timezone

try:
    from supabase import create_client
except ImportError:
    sys.exit("sync_supabase: missing dependency — pip install supabase")

HERE = os.path.dirname(os.path.abspath(__file__))
READS_PATH = os.environ.get("READS_PATH", os.path.join(HERE, "out", "reads.json"))
EVENTS_PATH = os.environ.get("EVENTS_PATH", os.path.join(HERE, "state", "events.jsonl"))
SYNC_STATE_PATH = os.environ.get(
    "SYNC_STATE_PATH", os.path.join(HERE, ".sync_supabase_cursor.json")
)
CHUNK = 500


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_read_row(ticker, obj):
    """Map one worker read entry onto a ticker_reads row."""
    if not isinstance(obj, dict):
        return None
    text = obj.get("read") or obj.get("read_text") or obj.get("text") or ""
    if not ticker or not text:
        return None
    return {
        "ticker": str(ticker).upper(),
        "read_text": text,
        "state_hash": obj.get("state_hash"),
        "updated_at": obj.get("updated_at") or now_iso(),
    }


def load_reads(path):
    """Return a list of ticker_reads rows from reads.json (tolerant of shape)."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    rows = []
    if isinstance(data, dict) and isinstance(data.get("reads"), dict):
        items = data["reads"].items()
    elif isinstance(data, dict):
        # bare {sym: {...}} map — skip scalar top-level keys like updated_at
        items = ((k, v) for k, v in data.items() if isinstance(v, dict))
    elif isinstance(data, list):
        items = ((o.get("ticker") or o.get("sym"), o) for o in data if isinstance(o, dict))
    else:
        return []

    for ticker, obj in items:
        row = normalize_read_row(ticker, obj)
        if row:
            rows.append(row)
    return rows


def load_cursor(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(json.load(f).get("events_offset", 0))
    except (OSError, ValueError):
        return 0


def save_cursor(path, offset):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"events_offset": offset, "saved_at": now_iso()}, f)
    os.replace(tmp, path)


def read_new_events(path, offset):
    """Yield (row, end_offset) for each complete new line after `offset`."""
    if not os.path.exists(path):
        return
    size = os.path.getsize(path)
    if offset > size:  # file was truncated/rotated — start over
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line or not line.endswith(b"\n"):
                break  # EOF or partial line still being written
            end = f.tell()
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                offset = end
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                offset = end  # skip malformed line, don't re-read it forever
                continue
            ticker = obj.get("ticker") or obj.get("sym")
            if ticker:
                row = {
                    "ticker": str(ticker).upper(),
                    "ts": obj.get("ts") or now_iso(),
                    "changes": obj.get("changes") or [],
                }
                yield row, end
            offset = end


def main():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("sync_supabase: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    client = create_client(url, key)

    # --- ticker_reads: upsert everything in reads.json -----------------------
    reads = load_reads(READS_PATH)
    for i in range(0, len(reads), CHUNK):
        client.table("ticker_reads").upsert(reads[i : i + CHUNK]).execute()
    print(f"sync_supabase: upserted {len(reads)} ticker_reads rows")

    # --- state_events: insert only lines past the cursor ---------------------
    cursor = load_cursor(SYNC_STATE_PATH)
    batch, batch_end, inserted = [], cursor, 0
    for row, end in read_new_events(EVENTS_PATH, cursor):
        batch.append(row)
        batch_end = end
        if len(batch) >= CHUNK:
            client.table("state_events").insert(batch).execute()
            save_cursor(SYNC_STATE_PATH, batch_end)
            inserted += len(batch)
            batch = []
    if batch:
        client.table("state_events").insert(batch).execute()
        save_cursor(SYNC_STATE_PATH, batch_end)
        inserted += len(batch)
    print(f"sync_supabase: inserted {inserted} state_events rows")


if __name__ == "__main__":
    main()
