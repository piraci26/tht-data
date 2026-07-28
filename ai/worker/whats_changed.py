#!/usr/bin/env python3
"""IQ Algo "What's changed?" -- module + CLI.

Given a ticker and a since-timestamp, replays the engine-state change log the
reads worker appends to state/events.jsonl, renders the raw field diffs into
IQ vocabulary, and asks the LLM what changed and what it means. This is the
exact function the future Supabase edge function will mirror: same inputs
(ticker, since), same event log, same prompt, same output (a short answer).

Degrades gracefully: if the LLM is unreachable the deterministic change
summary is returned instead of an answer, prefixed with a note -- callers
always get usable text.

Env config is shared with worker.py (LLM_BASE_URL / LLM_MODEL / LLM_API_KEY).
Prompt text comes from ../prompts/whats_changed.txt when present.

CLI:
  python3 whats_changed.py NVDA --since 7d
  python3 whats_changed.py MSFT --since 2026-07-27T00:00:00Z
  python3 whats_changed.py NVDA --since 24h --no-llm   # deterministic diff only
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

from worker import (EVENTS_FILE, STATE_FILE, chat, load_json, load_prompt,
                    log, make_client)

FALLBACK_WHATS_CHANGED_PROMPT = """\
You explain engine-state changes for the IQ Algo scanner (IQ Bands = trend
regime, IQ Oscillator = momentum). Voice: professional trader shorthand. No
hype, no emojis, no financial advice -- say "the scan shows X", never "you
should buy". Given a chronological list of state changes for one ticker,
answer in 2-4 sentences: what changed and what it means for the picture.
Use ONLY the changes listed -- never invent signals, streaks, or confluence
counts that are not in the log. Weigh the most recent changes heaviest; call
out reversals (a flip that later flipped back) explicitly. Output only the
answer."""

# Raw fingerprint fields -> IQ vocabulary.
FIELD_LABEL = {
    "bands": "IQ Bands trend regime",
    "osc": "IQ Oscillator momentum signal",
    "osc_sign": "IQ Oscillator zero line",
}
VALUE_WORD = {
    "bands": {"green": "fresh bullish flip", "red": "fresh bearish flip",
              None: "no fresh flip"},
    "osc": {"green": "fresh bullish signal", "red": "fresh bearish signal",
            None: "no fresh signal"},
    "osc_sign": {"pos": "above zero", "neg": "below zero", None: "n/a"},
}


def parse_since(text):
    """Accept ISO-8601 (Z ok) or shorthand like 24h / 7d / 90m."""
    m = re.fullmatch(r"(\d+)([mhd])", text.strip())
    if m:
        qty, unit = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=qty), "h": timedelta(hours=qty),
                 "d": timedelta(days=qty)}[unit]
        return datetime.now(timezone.utc) - delta
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_events(ticker, since_dt):
    """Stream events.jsonl and return this ticker's events since the cutoff."""
    ticker = ticker.upper()
    out = []
    try:
        fh = EVENTS_FILE.open("r", encoding="utf-8")
    except OSError:
        return out
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue  # tolerate a torn line from a crashed append
            if ev.get("ticker", "").upper() != ticker:
                continue
            try:
                ts = datetime.fromisoformat(ev.get("ts", "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= since_dt:
                out.append(ev)
    return out


def render_change(change):
    """One field diff -> one IQ-vocabulary line, or None if unrenderable."""
    field = change.get("field", "")
    if "." not in field:
        return None
    tf, key = field.split(".", 1)
    label = FIELD_LABEL.get(key)
    words = VALUE_WORD.get(key)
    if not label or not words:
        return None  # unknown future field; skip rather than guess
    frm = words.get(change.get("from"), str(change.get("from")))
    to = words.get(change.get("to"), str(change.get("to")))
    return "%s %s: %s -> %s" % (tf, label, frm, to)


def render_events(events):
    """Chronological, human-readable change log for the prompt (and fallback)."""
    lines = []
    for ev in events:
        rendered = [render_change(c) for c in ev.get("changes", [])]
        rendered = [r for r in rendered if r]
        if rendered:
            lines.append("%s:" % ev.get("ts", "?"))
            lines.extend("  - " + r for r in rendered)
    return "\n".join(lines)


def whats_changed(ticker, since, client=None, use_llm=True):
    """Answer "what changed for <ticker> since <since>?".

    since: datetime, ISO string, or shorthand ("24h", "7d").
    Returns a string in every case; never raises on LLM failure.
    """
    ticker = ticker.upper()
    since_dt = since if isinstance(since, datetime) else parse_since(str(since))
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)

    events = load_events(ticker, since_dt)
    if not events:
        return ("No engine-state changes for %s since %s. The scan shows the "
                "same picture as before." % (ticker, since_dt.isoformat(timespec="seconds")))

    diff_text = render_events(events)
    if not diff_text:
        return ("State changes were logged for %s since %s but none map to "
                "known signal fields." % (ticker, since_dt.isoformat(timespec="seconds")))

    if not use_llm:
        return diff_text

    if client is None:
        client = make_client()
    if client is None:
        return "LLM unavailable -- raw change log for %s:\n%s" % (ticker, diff_text)

    prompt = load_prompt("whats_changed.txt", FALLBACK_WHATS_CHANGED_PROMPT)
    since_str = since_dt.isoformat(timespec="seconds")
    if "{{EVENTS_JSON}}" in prompt:
        # prompts/whats_changed.txt is a completion-style template with
        # {{SINCE}} / {{EVENTS_JSON}} / {{NOW_JSON}} slots — fill them and
        # send the whole thing as a single user message. Events go in already
        # rendered into IQ vocabulary; NOW is the current fingerprint from
        # state/last_states.json.
        now_fp = ((load_json(STATE_FILE, {}) or {}).get(ticker) or {}).get(
            "fingerprint") or {}
        system = None
        user = (prompt
                .replace("{{SINCE}}", since_str)
                .replace("{{EVENTS_JSON}}",
                         "(ticker %s, oldest first)\n%s" % (ticker, diff_text))
                .replace("{{NOW_JSON}}",
                         json.dumps({"ticker": ticker, "state": now_fp},
                                    sort_keys=True)))
    else:
        system = prompt
        user = ("Ticker: %s\nWindow: since %s (UTC)\nEngine-state changes, "
                "oldest first:\n%s" % (ticker, since_str, diff_text))
    try:
        answer = chat(client, system, user, max_tokens=300)
        if answer:
            return answer
        raise ValueError("empty completion")
    except Exception as exc:
        log("whats_changed LLM call failed for %s: %s" % (ticker, exc))
        return "LLM unavailable -- raw change log for %s:\n%s" % (ticker, diff_text)


def main(argv=None):
    ap = argparse.ArgumentParser(description="What changed for a ticker?")
    ap.add_argument("ticker", help="symbol, e.g. NVDA")
    ap.add_argument("--since", default="7d",
                    help="ISO timestamp or shorthand 24h / 7d / 90m (default 7d)")
    ap.add_argument("--no-llm", action="store_true",
                    help="print the deterministic diff without calling the LLM")
    args = ap.parse_args(argv)
    try:
        since_dt = parse_since(args.since)
    except ValueError:
        ap.error("could not parse --since %r (use ISO-8601 or 24h/7d/90m)" % args.since)
    print(whats_changed(args.ticker, since_dt, use_llm=not args.no_llm))
    return 0


if __name__ == "__main__":
    sys.exit(main())
