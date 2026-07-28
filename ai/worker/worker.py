#!/usr/bin/env python3
"""IQ Algo reads worker.

Watches the scan output (results.json / results_weekly.json / results_monthly.json),
fingerprints each ticker's ENGINE STATE (signals, not prices), diffs against the
previously persisted state, logs every state change to state/events.jsonl, and asks
the LLM for a 2-3 sentence "read" in the IQ voice for every NEW or CHANGED ticker.
Reads land in out/reads.json as {ticker: {read, updated_at, state_hash}}.

DATA-SOURCE ASSUMPTIONS (verified against the live schema 2026-07-28):
- The scan lists are FLIP lists, not state lists: a ticker sits in fvb_green only
  while its IQ Bands regime flip is fresh on that timeframe's current bar. A ticker
  can appear in one fvb_* list and one bxt_* list at the same time.
- Field names are legacy: "fvb" = IQ Bands (trend regime), "bxt" = IQ Oscillator
  (momentum). All user-facing text must use the IQ names, never fvb/bxt.
- Per-row fields we rely on (feature-detected, all optional except sym):
  sym, name, mcap, price, basis, bxt_today, bxt_yest, fvb_streak, bxt_streak,
  ath, pct_to_ath, wk52_high, wk52_low, ath_30d, atl_30d.
- Production data lives on GitHub Pages (https://piraci26.github.io/tht-data);
  the local ~/tht-data checkout is a stale mirror. Default source is the URL;
  set SCAN_BASE to a local directory only for offline dev.

FINGERPRINT DESIGN:
- Per ticker, per timeframe: {"bands": green|red|None, "osc": green|red|None,
  "osc_sign": pos|neg|None}. Membership in a list == fresh flip on that side.
- Deliberately EXCLUDED from the fingerprint: price, basis, bxt_today magnitude,
  and rising/falling direction (all of these flicker intraday on the 5-minute
  scan cadence and would spam events + LLM calls). They still appear in the
  facts block handed to the LLM so reads can cite them.
- If a timeframe fails to fetch on a pass, that timeframe's previous state is
  carried forward unchanged (no false "dropped" events on network blips).
- Removal (ticker leaves all lists) is logged to events.jsonl but does NOT
  trigger an LLM read; the last read is kept in reads.json.

LLM: OpenAI-compatible chat completions. Env-configurable so dev (Ollama) and
prod (DeepInfra/Groq/Anthropic) are a pure env swap:
  LLM_BASE_URL  default http://localhost:11434/v1
  LLM_MODEL     default llama3.1:8b
  LLM_API_KEY   default "ollama"
  LLM_TIMEOUT   default 60 (seconds per request)
  SCAN_BASE     default https://piraci26.github.io/tht-data (URL or local dir)
  MAX_READS_PER_PASS  default 25 (mcap-desc priority; the rest retry next pass)

Prompt text comes from ../prompts/read.txt when present; a built-in fallback
keeps the worker functional if the prompts are missing. The prompts/ files are
completion-style templates ({{STATE_JSON}} etc.): when the placeholder is
present the worker fills it and sends the whole template as a single user
message; otherwise the file is used as the system prompt with the facts block
as the user message.

CLI:
  python3 worker.py --once
  python3 worker.py --loop --interval 300
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- paths

WORKER_DIR = Path(__file__).resolve().parent
STATE_DIR = WORKER_DIR / "state"
OUT_DIR = WORKER_DIR / "out"
PROMPTS_DIR = WORKER_DIR.parent / "prompts"

STATE_FILE = STATE_DIR / "last_states.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"
READS_FILE = OUT_DIR / "reads.json"

# --------------------------------------------------------------------------- config

# .strip() everywhere: secrets pasted into CI/UI fields often carry a stray
# trailing newline, which turns into a 401 that looks like a bad key.
SCAN_BASE = os.environ.get("SCAN_BASE", "https://piraci26.github.io/tht-data").strip()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.1:8b").strip()
LLM_API_KEY = os.environ.get("LLM_API_KEY", "ollama").strip()
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
MAX_READS_PER_PASS = int(os.environ.get("MAX_READS_PER_PASS", "25"))
FETCH_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "30"))

TIMEFRAME_FILES = {
    "daily": "results.json",
    "weekly": "results_weekly.json",
    "monthly": "results_monthly.json",
}
TIMEFRAMES = list(TIMEFRAME_FILES.keys())
LIST_KEYS = ("fvb_green", "fvb_red", "bxt_green", "bxt_red")

# Consecutive LLM failures before we stop trying for the rest of the pass.
LLM_FAIL_LIMIT = 3

FALLBACK_READ_PROMPT = """\
You write short "reads" for the IQ Algo scanner (IQ Bands = trend regime,
IQ Oscillator = momentum). Voice: professional trader shorthand. No hype, no
emojis, no financial advice -- say "the scan shows X", never "you should buy".
Vocabulary: trend regime (bullish/bearish, flips), momentum wave and signal,
extremes/stretched, 52-week range, all-time high.
Given the engine-state facts for one ticker, write a 2-3 sentence read that a
trader can absorb in five seconds. Use ONLY the facts provided -- never invent
signals, levels, or confluence counts that are not listed. Lead with the most
significant signal. Mention the timeframe. Cite at most two numbers. Output
only the read text, no preamble, no quotes."""


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (ts, msg), flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- prompts


def load_prompt(filename, fallback):
    """Read a prompt from ../prompts/, falling back to a built-in string."""
    path = PROMPTS_DIR / filename
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return fallback


# --------------------------------------------------------------------------- LLM


def make_client():
    """Build the OpenAI-compatible client, or return None if unavailable."""
    try:
        from openai import OpenAI
    except ImportError:
        log("openai package not installed -- reads will be skipped "
            "(pip install -r requirements.txt)")
        return None
    return OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=LLM_TIMEOUT)


def chat(client, system, user, max_tokens=220, temperature=0.4):
    """One chat completion. Raises on failure; callers decide how to degrade.

    system may be None/empty for single-message completion-style prompts.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    text = (resp.choices[0].message.content or "").strip()
    return text.strip('"').strip()


# --------------------------------------------------------------------------- scan loading


def load_scan(timeframe):
    """Load one timeframe's results file from SCAN_BASE (URL or local dir).

    Returns the parsed dict or None on any failure.
    """
    name = TIMEFRAME_FILES[timeframe]
    try:
        if SCAN_BASE.startswith("http://") or SCAN_BASE.startswith("https://"):
            url = SCAN_BASE.rstrip("/") + "/" + name
            req = urllib.request.Request(url, headers={"User-Agent": "iq-algo-reads-worker"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as fh:
                data = json.loads(fh.read().decode("utf-8"))
        else:
            data = json.loads((Path(SCAN_BASE) / name).read_text(encoding="utf-8"))
    except Exception as exc:  # network truncation raises non-OSError types
        # (e.g. http.client.IncompleteRead); a bad fetch must never kill a pass
        log("fetch failed for %s: %s" % (name, exc))
        return None
    if not isinstance(data, dict):
        log("unexpected payload shape for %s (not a dict) -- skipping" % name)
        return None
    return data


def _num(row, key):
    v = row.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def extract_current(scans):
    """Build per-ticker fingerprints and facts from the fetched scans.

    Returns (fingerprints, facts, meta):
      fingerprints: {sym: {tf: {"bands":.., "osc":.., "osc_sign":..}}}
      facts:        {sym: {tf: {raw fields for the prompt}}}
      meta:         {sym: {"name":.., "mcap":..}}
    """
    fingerprints, facts, meta = {}, {}, {}
    for tf, data in scans.items():
        for key in LIST_KEYS:
            rows = data.get(key)
            if not isinstance(rows, list):
                continue
            product, side = key.split("_")  # ("fvb"|"bxt", "green"|"red")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                sym = row.get("sym")
                if not sym:
                    continue
                fp = fingerprints.setdefault(sym, {}).setdefault(
                    tf, {"bands": None, "osc": None, "osc_sign": None})
                if product == "fvb":
                    fp["bands"] = side
                else:
                    fp["osc"] = side
                bxt = _num(row, "bxt_today")
                if bxt is not None:
                    fp["osc_sign"] = "pos" if bxt >= 0 else "neg"
                facts.setdefault(sym, {})[tf] = row
                m = meta.setdefault(sym, {})
                if row.get("name"):
                    m["name"] = row["name"]
                if _num(row, "mcap") is not None:
                    m["mcap"] = row["mcap"]
    return fingerprints, facts, meta


# --------------------------------------------------------------------------- state


def fingerprint_hash(fp):
    blob = json.dumps(fp, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def append_events(events):
    if not events:
        return
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, separators=(",", ":")) + "\n")


def diff_fingerprints(old_fp, new_fp):
    """Flat field-level diff: [{"field": "daily.bands", "from": x, "to": y}]."""
    changes = []
    for tf in TIMEFRAMES:
        o = old_fp.get(tf) or {}
        n = new_fp.get(tf) or {}
        for field in ("bands", "osc", "osc_sign"):
            ov, nv = o.get(field), n.get(field)
            if ov != nv:
                changes.append({"field": "%s.%s" % (tf, field), "from": ov, "to": nv})
    return changes


# --------------------------------------------------------------------------- facts -> prompt text

SIDE_WORD = {"green": "bullish", "red": "bearish"}


def render_facts(sym, fp, tf_facts, meta):
    """Translate raw scan fields into IQ-vocabulary facts for the LLM."""
    lines = []
    name = meta.get("name") or sym
    mcap = meta.get("mcap")
    head = "TICKER: %s (%s)" % (sym, name)
    if mcap is not None:
        head += ", market cap $%.0fB" % mcap
    lines.append(head)

    for tf in TIMEFRAMES:
        state = fp.get(tf)
        row = tf_facts.get(tf)
        if not state or not row:
            continue
        lines.append("%s timeframe:" % tf.upper())

        if state.get("bands"):
            word = SIDE_WORD[state["bands"]]
            streak = _num(row, "fvb_streak")
            s = "- IQ Bands: trend regime flipped %s on this bar" % word
            if streak:
                s += " (prior %s regime lasted %d bars)" % (
                    "bearish" if word == "bullish" else "bullish", int(streak))
            lines.append(s + ".")
        price, basis = _num(row, "price"), _num(row, "basis")
        if price is not None and basis is not None:
            rel = "above" if price >= basis else "below"
            lines.append("- Price %.2f is %s the IQ Bands basis %.2f." % (price, rel, basis))

        bxt, bxt_y = _num(row, "bxt_today"), _num(row, "bxt_yest")
        if state.get("osc"):
            word = SIDE_WORD[state["osc"]]
            streak = _num(row, "bxt_streak")
            s = "- IQ Oscillator: momentum signal flipped %s on this bar" % word
            if streak:
                s += " (prior opposite wave lasted %d bars)" % int(streak)
            lines.append(s + ".")
        if bxt is not None:
            zone = "above zero" if bxt >= 0 else "below zero"
            s = "- IQ Oscillator value %.1f (%s" % (bxt, zone)
            if bxt_y is not None:
                s += ", %s from %.1f" % ("rising" if bxt >= bxt_y else "falling", bxt_y)
            lines.append(s + ").")

        if state.get("bands") and state.get("osc") and state["bands"] == state["osc"]:
            lines.append("- Confluence: trend and momentum flipped %s together on the %s."
                         % (SIDE_WORD[state["bands"]], tf))

        pct_ath = _num(row, "pct_to_ath")
        if pct_ath is not None:
            if pct_ath <= 2:
                lines.append("- Extremes: within %.1f%% of the all-time high." % pct_ath)
            else:
                # pct_to_ath is UPSIDE REQUIRED to reach the ATH, not distance
                # below it — "105% below" is impossible and confuses the model.
                lines.append("- Needs +%.1f%% to reclaim the all-time high." % pct_ath)
        lo, hi = _num(row, "wk52_low"), _num(row, "wk52_high")
        if lo is not None and hi is not None:
            lines.append("- 52-week range %.2f to %.2f." % (lo, hi))
        ath30, atl30 = _num(row, "ath_30d"), _num(row, "atl_30d")
        if ath30:
            lines.append("- %d new all-time high(s) in the last 30 days." % int(ath30))
        if atl30:
            lines.append("- %d new all-time low(s) in the last 30 days." % int(atl30))
    return "\n".join(lines)


def generate_read(client, sym, fp, tf_facts, meta):
    prompt = load_prompt("read.txt", FALLBACK_READ_PROMPT)
    facts = render_facts(sym, fp, tf_facts, meta)
    if "{{STATE_JSON}}" in prompt:
        # prompts/read.txt is a completion-style template ending in
        # "STATE:\n{{STATE_JSON}}\nREAD:" — fill the slot and send the whole
        # thing as a single user message (few-shots stay adjacent to the data).
        return chat(client, None, prompt.replace("{{STATE_JSON}}", facts))
    return chat(client, prompt, facts)


# --------------------------------------------------------------------------- one pass


def run_pass(client):
    scans = {}
    for tf in TIMEFRAMES:
        data = load_scan(tf)
        if data is not None:
            scans[tf] = data
    if not scans:
        log("no scan data reachable this pass -- nothing to do")
        return

    fetched_tfs = set(scans.keys())
    current_fp, facts, meta = extract_current(scans)
    old_state = load_json(STATE_FILE, {})
    if not isinstance(old_state, dict):
        old_state = {}

    ts = now_iso()
    new_state = {}
    events = []
    changed_syms = set()

    for sym in sorted(set(old_state) | set(current_fp)):
        old_entry = old_state.get(sym) or {}
        old_fp = old_entry.get("fingerprint") or {}

        # Merge: fetched timeframes come from the scan; unfetched carry forward.
        merged = {}
        for tf in TIMEFRAMES:
            if tf in fetched_tfs:
                if sym in current_fp and tf in current_fp[sym]:
                    merged[tf] = current_fp[sym][tf]
            elif tf in old_fp:
                merged[tf] = old_fp[tf]

        if not merged:
            # Ticker left every list: log the removal, drop the state entry.
            if old_fp:
                events.append({"ts": ts, "ticker": sym,
                               "changes": diff_fingerprints(old_fp, {})})
            continue

        changes = diff_fingerprints(old_fp, merged)
        if changes:
            events.append({"ts": ts, "ticker": sym, "changes": changes})
            changed_syms.add(sym)
        new_state[sym] = {
            "fingerprint": merged,
            "hash": fingerprint_hash(merged),
            "updated_at": ts if changes else old_entry.get("updated_at", ts),
        }

    append_events(events)
    atomic_write_json(STATE_FILE, new_state)
    log("pass: %d tickers tracked, %d state changes, timeframes ok: %s"
        % (len(new_state), len(events), ",".join(sorted(fetched_tfs))))

    # ---- reads: any ticker present in the current scan whose read is stale.
    reads = load_json(READS_FILE, {})
    if not isinstance(reads, dict):
        reads = {}

    candidates = []
    for sym in current_fp:
        h = new_state.get(sym, {}).get("hash")
        if h and reads.get(sym, {}).get("state_hash") != h:
            candidates.append(sym)
    # Tickers whose state changed THIS pass are live signals — they jump the
    # queue; the stale backlog fills whatever capacity remains, by mcap.
    changed_now = {e["ticker"] for e in events}
    candidates.sort(key=lambda s: (s not in changed_now,
                                   -(meta.get(s, {}).get("mcap") or 0)))
    queue = candidates[:MAX_READS_PER_PASS]
    if len(candidates) > len(queue):
        log("read backlog: %d tickers stale (%d changed this pass), generating %d this pass"
            % (len(candidates), len(changed_now), len(queue)))

    if queue and client is None:
        log("LLM client unavailable -- skipping %d reads (state and events "
            "still updated; reads retry next pass)" % len(queue))
        return

    generated, failures = 0, 0
    last_error = None
    for sym in queue:
        try:
            text = generate_read(client, sym, new_state[sym]["fingerprint"],
                                 facts.get(sym, {}), meta.get(sym, {}))
            if not text:
                raise ValueError("empty completion")
            reads[sym] = {"read": text, "updated_at": now_iso(),
                          "state_hash": new_state[sym]["hash"]}
            generated += 1
            failures = 0
        except Exception as exc:  # LLM down/misconfigured must never kill the loop
            failures += 1
            last_error = "%s: %s: %s" % (sym, type(exc).__name__, str(exc)[:300])
            log("read failed for %s: %s" % (sym, exc))
            if failures >= LLM_FAIL_LIMIT:
                log("%d consecutive LLM failures -- abandoning reads for this pass"
                    % failures)
                break

    if generated:
        atomic_write_json(READS_FILE, reads)
    log("reads: %d generated (%d queued, %d already fresh)"
        % (generated, len(queue), len(current_fp) - len(candidates)))
    # Pass telemetry, published alongside reads.json — headless environments
    # (GitHub Actions) have no readable logs, so failures must surface here.
    atomic_write_json(OUT_DIR / "status.json", {
        "ts": now_iso(), "model": LLM_MODEL, "base_url": LLM_BASE_URL,
        "events": len(events), "queued": len(queue),
        "generated": generated, "last_error": last_error,
    })


# --------------------------------------------------------------------------- main


def main(argv=None):
    ap = argparse.ArgumentParser(description="IQ Algo reads worker")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run a single pass")
    mode.add_argument("--loop", action="store_true", help="run forever")
    ap.add_argument("--interval", type=int, default=300,
                    help="seconds between passes in --loop mode (default 300)")
    args = ap.parse_args(argv)

    log("reads worker starting: scan=%s llm=%s model=%s"
        % (SCAN_BASE, LLM_BASE_URL, LLM_MODEL))
    client = make_client()

    if args.once:
        run_pass(client)
        return 0

    while True:
        started = time.monotonic()
        try:
            run_pass(client)
        except Exception as exc:  # belt and braces: the loop must survive anything
            log("pass crashed: %s: %s" % (type(exc).__name__, exc))
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, args.interval - elapsed))


if __name__ == "__main__":
    sys.exit(main())
