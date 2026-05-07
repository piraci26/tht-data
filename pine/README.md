# THT Dual Scan — Pine Script Indicators

TradingView indicators that mirror the math in `scan.py`. A flip in TradingView matches a flip in the dashboard.

## Three scripts

| File | What it does | Where it lives |
|---|---|---|
| `THT-Dual-Scan-FVB.pine` | Fair Value Bands only | Price pane (overlay) |
| `THT-Dual-Scan-BXT.pine` | B-Xtrender only | Separate oscillator pane |
| `THT-Dual-Scan-Combined.pine` | Both, in one script | Combined (uses Pine v6 `force_overlay`) |

Use **Combined** if you want a single one-click indicator. Use **FVB + BXT separately** if you want to tune them independently or layer with other studies.

## Math (verbatim mirror of `scan.py`)

```
THT Fair Value Bands
   basis  = SMA(close, 20)
   upper  = basis + 2 × stdev(close, 20)
   lower  = basis - 2 × stdev(close, 20)
   bull   = close > basis           ← regime
   flip   = bull[t] != bull[t-1]    ← signal

B-Xtrender (short-term, the oscillator)
   short  = RSI(EMA(close, 5) − EMA(close, 20), 15) − 50
   smooth = WMA(short, 5)
   flip   = sign(short[t]) != sign(short[t-1])

B-Xtrender (long-term, the slow signal line)
   long   = WMA(RSI(close, 15) − 50, 5)
```

The dashboard's "**dual flip**" headline signal = both `FVB flip` AND `BXT flip` on the same bar. The Combined script highlights this with a triangle marker on the price pane.

## How to install on TradingView

1. **TradingView → Pine Editor → New indicator → Empty indicator**
2. Paste the contents of one of the `.pine` files
3. Click **Add to chart** (or save to your library first with **Save**)
4. Configure inputs in the indicator's settings gear (length, multipliers, colors)
5. (Optional) Set up alerts via the alarm-clock icon → choose any of the predefined alert conditions:
   - "FVB flipped GREEN" / "RED"
   - "BXT crossed above/below zero"
   - "BXT smoothed cross UP / DOWN"
   - "DUAL FLIP GREEN" / "RED" (Combined only — fires on the headline dual-flip signal)

## Using all three together

Apply **FVB** + **BXT** + your normal candles. You'll see:

- Price pane: candles + FVB cloud (green/red regions) + basis line + flip dots
- Oscillator pane (BXT): histogram bars + two smoothed lines + crossover dots

Or just use **Combined** for a single-indicator setup.

## Inputs you might tune

### FVB
- `Length` (default 20) — the SMA period. Match the dashboard's daily timeframe (20). For weekly use 20 weekly bars; for monthly use 20 monthly bars. *The math doesn't change between timeframes — your chart's timeframe sets the resolution.*
- `StdDev Multiplier` (default 2.0) — band width. Wider = fewer band-touches, more conservative.
- `Use close (uncheck for hl2)` — `hl2` smooths a tick of intraday noise.

### BXT
- `Short EMA Fast` / `Slow` (default 5 / 20) — the EMA pair whose difference goes into RSI.
- `Short RSI Length` (default 15) — RSI period over the EMA difference.
- `Long RSI Length` (default 15) — RSI period over raw close (the slower signal line).
- `Smoothing WMA` (default 5) — applied to both lines.

The dashboard's defaults are exactly these values. Don't change them unless you want a different signal than the dashboard reports.

## Pine version

Written in **Pine v6**. The Combined script uses `force_overlay = true` per-plot to render FVB on the price pane while staying in an oscillator-pane indicator — that feature requires v6.

## Alerts → trade-bot pipeline

Each script exposes **alertcondition** entries. To wire them to a webhook (e.g., Discord, Telegram, your trading bot):

1. Right-click the chart → **Add alert**
2. **Condition**: select your indicator → choose the specific alertcondition
3. **Notifications**: enable webhook URL → paste your endpoint
4. **Message**: TradingView will use the alert's default message (already populated with `{{ticker}}`, `{{interval}}`, `{{close}}` placeholders)

The dashboard's article disclaimer still applies — these are **observational signals**, not buy/sell recommendations.

## License

MIT. Use, modify, share. Attribution to the THT Dual Scan project appreciated but not required.
