# IQ Algo reads worker

Watches the scan output, fingerprints each ticker's engine state (signals, not
prices), logs every change to `state/events.jsonl`, and generates 2-3 sentence
per-ticker "reads" in the IQ voice via an OpenAI-compatible LLM. Output lands
in `out/reads.json`. `whats_changed.py` answers "what changed for TICKER since
T?" from the same event log — the future Supabase edge function mirrors it.

## Files

- `worker.py` — the service. `--once` or `--loop --interval N`.
- `whats_changed.py` — module + CLI for the change-explainer.
- `state/last_states.json` — persisted fingerprints (created on first run).
- `state/events.jsonl` — append-only change log (powers "What's changed?").
- `out/reads.json` — `{ticker: {read, updated_at, state_hash}}`.
- Prompts are read from `../prompts/read.txt` and `../prompts/whats_changed.txt`;
  built-in fallbacks are used when those files are missing. The prompt files
  are completion-style templates — the worker fills `{{STATE_JSON}}` /
  `{{SINCE}}` / `{{EVENTS_JSON}}` / `{{NOW_JSON}}` and sends the filled
  template as a single user message (a prompt file without placeholders is
  used as a plain system prompt instead).

## Run (dev, local Ollama)

```bash
# 1. Start Ollama and pull the dev model (one-time pull)
ollama serve            # skip if the Ollama app is already running
ollama pull llama3.1:8b

# 2. Install deps
cd "/Users/kubakaszynski/Desktop/IQ ALGO/ai-stack/worker"
python3 -m pip install -r requirements.txt

# 3. Single pass
python3 worker.py --once

# 4. Or run as a service (5-minute cadence, matching the scan)
python3 worker.py --loop --interval 300

# 5. Ask what changed
python3 whats_changed.py NVDA --since 7d
python3 whats_changed.py MSFT --since 2026-07-27T00:00:00Z
python3 whats_changed.py NVDA --since 24h --no-llm   # no LLM, raw diff
```

Defaults (no env needed for dev): `LLM_BASE_URL=http://localhost:11434/v1`,
`LLM_MODEL=llama3.1:8b`, `LLM_API_KEY=ollama`.

## Environment variables

| Var | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint |
| `LLM_MODEL` | `llama3.1:8b` | model name at that endpoint |
| `LLM_API_KEY` | `ollama` | API key (any string for Ollama) |
| `LLM_TIMEOUT` | `60` | seconds per LLM request |
| `SCAN_BASE` | `https://piraci26.github.io/tht-data` | scan data source; a local dir path also works for offline dev |
| `MAX_READS_PER_PASS` | `25` | LLM-read cap per pass (mcap-desc priority; rest retry next pass) |
| `FETCH_TIMEOUT` | `30` | seconds per scan-file fetch |

Note on `SCAN_BASE`: production scanning runs in GitHub Actions and publishes
to GitHub Pages; the local `~/tht-data` checkout is a stale mirror. Keep the
default URL unless deliberately testing offline
(`SCAN_BASE=/Users/kubakaszynski/tht-data/docs`).

## Swapping to production models

Everything goes through the three `LLM_*` vars — no code changes.

**DeepInfra**

```bash
export LLM_BASE_URL=https://api.deepinfra.com/v1/openai
export LLM_MODEL=meta-llama/Meta-Llama-3.1-70B-Instruct
export LLM_API_KEY=<deepinfra key>
```

**Groq**

```bash
export LLM_BASE_URL=https://api.groq.com/openai/v1
export LLM_MODEL=llama-3.1-70b-versatile
export LLM_API_KEY=<groq key>
```

**Anthropic (copilot tier; OpenAI-compat endpoint)**

```bash
export LLM_BASE_URL=https://api.anthropic.com/v1/
export LLM_MODEL=claude-sonnet-4-5
export LLM_API_KEY=<anthropic key>
```

Verify a swap with one cheap pass before looping:

```bash
python3 worker.py --once
python3 whats_changed.py NVDA --since 24h
```

## Behavior notes

- The scan lists are FLIP lists: a ticker appears only while its flip is fresh
  on that timeframe's bar. Fingerprints cover list membership (IQ Bands side,
  IQ Oscillator side) plus the oscillator's zero-line side, per timeframe.
  Prices, basis, and oscillator magnitude are excluded from the fingerprint
  (they flicker on the 5-minute cadence) but are still fed to the LLM as facts.
- If a timeframe file fails to fetch, its previous state carries forward — no
  false "dropped" events on network blips.
- A ticker leaving all lists is logged to `events.jsonl` but gets no new LLM
  read; its last read stays in `out/reads.json`.
- LLM down: the worker logs, skips reads, and keeps diffing state. Stale reads
  are retried on later passes (state_hash mismatch drives the retry queue).
  After 3 consecutive LLM failures the read phase aborts for that pass.
- First run generates a large backlog (every listed ticker is "new");
  `MAX_READS_PER_PASS` drains it over successive passes, biggest mcap first.
