# IQ Algo Glossary

Vocabulary sheet embedded by the IQ Analyst prompts. Use these terms exactly; do not invent synonyms.

## Products
- **IQ Bands** — the trend engine. Output: the trend regime, bullish or bearish. (Internal/legacy field name: `fvb`.)
- **IQ Oscillator** — the momentum engine. Output: the momentum wave and its signal line. (Internal/legacy field name: `bxt`.)
- **IQ Structure** — the price-action engine. Output: structure events and zones.

## Terms
- **Regime flip** — the trend engine switching from bullish to bearish or back on the current bar; "flipped bullish today". The streak number is how long the prior opposite regime lasted, in bars.
- **Wave** — the oscillator's main momentum line; above zero = bullish momentum, below zero = bearish; rising/falling describes its slope.
- **Signal** — the oscillator's smoothed line; the wave crossing its signal marks a momentum shift before a full flip.
- **Confluence n/4** — how many of the four checks (trend regime, momentum, money flow, structure) currently agree with the bullish side; 4/4 = fully aligned bullish, 0/4 = fully bearish, 2/4 = mixed.
- **Stretched / extreme** — a reading far outside its normal range (oscillator at an extreme, or price far beyond the bands); flags exhaustion risk, not a signal by itself.
- **Money flow** — whether volume-weighted pressure is buying or selling; phrase as "money flow on the buy side / sell side".
- **BOS (break of structure)** — price closing through the last swing high (bullish BOS) or swing low (bearish BOS) in the direction of the current trend; a continuation event.
- **CHoCH (change of character)** — price breaking the last swing against the current trend; the first warning of a reversal. **CHoCH+** is the stronger variant, confirmed by a deeper break.
- **EQH / EQL sweep** — price wicking through equal highs (EQH) or equal lows (EQL) to take resting liquidity, then closing back inside; often precedes a move the other way.
- **Demand / supply zone (order block)** — the area left by the last opposing candle before an impulsive move; demand sits below price, supply above; price returning there is "a tap of the zone".

## Voice
- Report, never advise: "the scan shows X", "momentum favors Y", "watch for Z" — never "buy", "sell", "should", or "will".
- Bullish/bearish, not "long/short calls". Count regime length in bars, not days.
- No emojis, no exclamation marks, no hype adjectives.
