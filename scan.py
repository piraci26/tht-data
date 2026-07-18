#!/usr/bin/env python3
"""
THT Dual Scan — runs every 5 min, writes docs/results.json for the dashboard.
Pulls daily OHLCV from Yahoo, computes THT Fair Value Bands + B-Xtrender,
detects flips between yesterday and today, writes results to JSON.
"""
import json, urllib.request, math, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TICKERS = json.load(open(os.path.join(HERE, "universe.json")))
try:
    SHARES = {k: v["shares"] for k, v in json.load(open(os.path.join(HERE, "shares_outstanding.json"))).items()}
except FileNotFoundError:
    SHARES = {}
try:
    UNIVERSE_NAMES = json.load(open(os.path.join(HERE, "universe_names.json")))
except FileNotFoundError:
    UNIVERSE_NAMES = {}
# Authoritative mcap cache built by build_universe_3_5b.py via yfinance.fast_info.
# For ADRs (BCH, HDB, CUK, TSM…) the price × SHARES calc is junk because SHARES
# holds total home-listing share count denominated in the home currency. The
# cache holds Yahoo's marketCap field which represents the whole company in USD
# — correct for sizing in every case I've checked.
try:
    MCAP_CACHE = {k: v["mcap_b"] for k, v in json.load(open(os.path.join(HERE, "docs", "mcap_cache.json"))).items() if isinstance(v, dict) and v.get("mcap_b")}
except (FileNotFoundError, KeyError, ValueError):
    MCAP_CACHE = {}

def live_mcap(sym, price):
    """Live mcap in $B. Prefer cached Yahoo marketCap (correct for ADRs and
    US-only), fall back to price × shares (only safe for pure US single-class)."""
    cached = MCAP_CACHE.get(sym)
    if cached:
        return round(cached, 2)
    s = SHARES.get(sym)
    if s and price:
        return round(price * s / 1e9)
    return MCAPS.get(sym, 0)

# ─── Sector / industry metadata ───────────────────────────────────────────
SECTORS_PATH = os.path.join(HERE, "universe_sectors.json")
try:
    UNIVERSE_SECTORS = json.load(open(SECTORS_PATH))
except FileNotFoundError:
    UNIVERSE_SECTORS = {}

def sector_of(sym):
    return UNIVERSE_SECTORS.get(sym, {}).get("sector")
def industry_of(sym):
    return UNIVERSE_SECTORS.get(sym, {}).get("industry")

# ─── Append-only event + snapshot logs ────────────────────────────────────
HISTORY_DIR = os.path.join(HERE, "docs", "history")
EVENTS_PATH    = os.path.join(HISTORY_DIR, "events.jsonl")
SNAPSHOTS_PATH = os.path.join(HISTORY_DIR, "snapshots.jsonl")

def _append_jsonl(path, rows):
    if not rows: return
    os.makedirs(HISTORY_DIR, exist_ok=True)
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(',', ':')) + "\n")

# ─── Indicator math (ported from THT Pine source) ─────────────────────────
def sma(values, length):
    if len(values) < length: return None
    return sum(values[-length:]) / length

def ema_series(values, length):
    if len(values) < length: return [None] * len(values)
    k = 2 / (length + 1)
    out = [None] * (length - 1)
    e = sum(values[:length]) / length
    out.append(e)
    for v in values[length:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def rsi_series(values, length=14):
    n = len(values)
    if n < length + 1: return [None] * n
    out = [None] * length
    gains, losses = [0.0], [0.0]
    for i in range(1, n):
        d = values[i] - values[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[1:length+1]) / length
    avg_l = sum(losses[1:length+1]) / length
    rs = avg_g / avg_l if avg_l else float("inf")
    out.append(100 - 100/(1+rs))
    for i in range(length+1, n):
        avg_g = (avg_g * (length-1) + gains[i]) / length
        avg_l = (avg_l * (length-1) + losses[i]) / length
        rs = avg_g / avg_l if avg_l else float("inf")
        out.append(100 - 100/(1+rs))
    return out

def _iq_bands_regimes(ohlc, length=33, mult=0.25):
    """Compute the full sticky-regime series for IQ Bands.
    Source: OHLC4 for basis (SMA length=33), close for stdev. Sticky regime:
      bull turns on when close > basis + mult*sd; bear when close < basis - mult*sd; else stays.
    Returns (regimes_list, basis_today, price_today) or None.
    """
    opens = ohlc["opens"]; highs = ohlc["highs"]; lows = ohlc["lows"]; closes = ohlc["closes"]
    n = len(closes)
    if n < length + 2: return None
    ohlc4 = [(opens[i] + highs[i] + lows[i] + closes[i]) / 4 for i in range(n)]

    # Rolling SMA on OHLC4
    basis = [None] * n
    s = sum(ohlc4[:length])
    basis[length - 1] = s / length
    for i in range(length, n):
        s += ohlc4[i] - ohlc4[i - length]
        basis[i] = s / length

    # Rolling sample-style stdev on close (Pine ta.stdev uses population by default; close-window variance)
    sd = [None] * n
    for i in range(length - 1, n):
        window = closes[i - length + 1:i + 1]
        m = sum(window) / length
        var = sum((c - m) ** 2 for c in window) / length
        sd[i] = var ** 0.5

    # Sticky regime walk
    regimes = [None] * n
    is_bull = closes[length - 1] > basis[length - 1]
    regimes[length - 1] = is_bull
    for i in range(length, n):
        if basis[i] is None or sd[i] is None:
            regimes[i] = is_bull; continue
        tu = basis[i] + mult * sd[i]
        tl = basis[i] - mult * sd[i]
        if closes[i] > tu:
            is_bull = True
        elif closes[i] < tl:
            is_bull = False
        regimes[i] = is_bull
    return regimes, basis[-1], closes[-1]

def iq_bands_state(ohlc, length=33, mult=0.25):
    """IQ Bands sticky-regime state for today vs yesterday."""
    out = _iq_bands_regimes(ohlc, length, mult)
    if out is None: return None
    regimes, basis_today, price_today = out
    if regimes[-1] is None or regimes[-2] is None: return None
    return {
        "today_bull": regimes[-1],
        "yest_bull":  regimes[-2],
        "basis": basis_today,
        "price": price_today,
    }

def iq_bands_prior_days(ohlc, length=33, mult=0.25):
    """Count how many bars the prior regime lasted before today's flip."""
    out = _iq_bands_regimes(ohlc, length, mult)
    if out is None: return None
    regimes, _, _ = out
    if regimes[-1] is None or regimes[-2] is None: return None
    if regimes[-1] == regimes[-2]: return None
    prior_bull = regimes[-2]
    days = 0
    for i in range(len(regimes) - 2, length - 2, -1):
        if regimes[i] == prior_bull:
            days += 1
        else:
            break
    return days

def iq_osc_prior_days(closes, short_fast=5, short_slow=20, short_rsi=5):
    """If IQ Oscillator flipped today (shortBxt crossed zero), count how many bars the PRIOR sign lasted.
    shortBxt = RSI(EMA(close, short_fast) - EMA(close, short_slow), short_rsi) - 50.
    """
    e_fast = ema_series(closes, short_fast)
    e_slow = ema_series(closes, short_slow)
    diff = [a - b if a is not None and b is not None else None for a, b in zip(e_fast, e_slow)]
    valid = [d for d in diff if d is not None]
    if len(valid) < short_rsi + 2: return None
    rsi_vals = rsi_series(valid, short_rsi)
    bxt = [None] * len(closes)
    j = 0
    for i in range(len(closes)):
        if diff[i] is not None:
            bxt[i] = (rsi_vals[j] - 50) if j < len(rsi_vals) and rsi_vals[j] is not None else None
            j += 1
    if bxt[-1] is None or bxt[-2] is None: return None
    today_pos = bxt[-1] > 0
    yest_pos  = bxt[-2] > 0
    if today_pos == yest_pos: return None
    prior_pos = yest_pos
    days = 0
    for i in range(len(closes) - 2, -1, -1):
        if bxt[i] is None: break
        if (bxt[i] > 0) == prior_pos:
            days += 1
        else:
            break
    return days

def iq_osc_state(closes, short_fast=5, short_slow=20, short_rsi=5):
    """IQ Oscillator shortBxt today vs yesterday (zero-cross is the flip)."""
    if len(closes) < 80: return None
    e_fast = ema_series(closes, short_fast)
    e_slow = ema_series(closes, short_slow)
    diff = []
    for i in range(len(closes)):
        if e_fast[i] is not None and e_slow[i] is not None:
            diff.append(e_fast[i] - e_slow[i])
        else:
            diff.append(None)
    diff_clean = [d for d in diff if d is not None]
    if len(diff_clean) < short_rsi + 2: return None
    rsi_vals = rsi_series(diff_clean, short_rsi)
    short_term = [r - 50 if r is not None else None for r in rsi_vals]
    short_clean = [s for s in short_term if s is not None]
    if len(short_clean) < 2: return None
    return {"today": short_clean[-1], "yest": short_clean[-2]}

# ─── Yahoo fetch ───────────────────────────────────────────────────────────
def fetch(sym, interval="1d", rng="1y"):
    """Returns (sym, ohlc) where ohlc = dict of {opens, highs, lows, closes} aligned arrays."""
    sym_q = sym.replace(".", "-")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_q}?range={rng}&interval={interval}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        result = data["chart"]["result"][0]
        q = result["indicators"]["quote"][0]
        n = len(q["close"])
        opens = []; highs = []; lows = []; closes = []
        for i in range(n):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if c is None or o is None or h is None or l is None:
                continue
            opens.append(o); highs.append(h); lows.append(l); closes.append(c)
        return sym, {"opens": opens, "highs": highs, "lows": lows, "closes": closes}
    except Exception:
        return sym, None

def fetch_ohlc(sym, interval="1d", rng="1y"):
    """OHLC bars for charting."""
    sym_q = sym.replace(".", "-")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_q}?range={rng}&interval={interval}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        result = data["chart"]["result"][0]
        t = result["timestamp"]
        q = result["indicators"]["quote"][0]
        bars = []
        v = q.get("volume", [None]*len(t))
        for i, ts in enumerate(t):
            if q["open"][i] is None: continue
            bars.append({"time": ts, "open": q["open"][i], "high": q["high"][i],
                         "low": q["low"][i], "close": q["close"][i],
                         "volume": v[i] if v[i] is not None else 0})
        return sym, bars
    except Exception:
        return sym, None

def fetch_ath(sym):
    """Returns (sym, ath, atl, wk52_high, wk52_low, ath_30d_count, atl_30d_count,
                last_high, last_low, last_close, made_ath_today, made_atl_today)."""
    sym_q = sym.replace(".","-")
    try:
        # range=max&interval=1wk → true all-time high/low (Yahoo silently downsamples old data to
        # quarterly bars; per-bar min/max are preserved, so max/min-of-array are correct).
        url_w = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_q}?range=max&interval=1wk"
        req = urllib.request.Request(url_w, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            wk_data = json.loads(r.read())
        wq = wk_data["chart"]["result"][0]["indicators"]["quote"][0]
        wk_highs = [c for c in wq["high"] if c is not None]
        wk_lows  = [c for c in wq["low"]  if c is not None]
        ath = max(wk_highs) if wk_highs else None
        atl = min(wk_lows)  if wk_lows  else None

        # Daily bars 5y → true daily resolution for 52w window + new-ATH/ATL counting.
        wk52_h = wk52_l = None
        ath_30d = 0
        atl_30d = 0
        last_high = last_low = last_close = None
        made_ath_today = False
        made_atl_today = False
        url_d = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_q}?range=5y&interval=1d"
        req = urllib.request.Request(url_d, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d_data = json.loads(r.read())
        d_quote = d_data["chart"]["result"][0]["indicators"]["quote"][0]
        d_highs  = [c for c in d_quote["high"]  if c is not None]
        d_lows   = [c for c in d_quote["low"]   if c is not None]
        d_closes = [c for c in d_quote["close"] if c is not None]
        if d_highs:  wk52_h = max(d_highs[-252:])  # ~252 trading days = 52 weeks
        if d_lows:   wk52_l = min(d_lows[-252:])
        if d_highs:  last_high  = d_highs[-1]
        if d_lows:   last_low   = d_lows[-1]
        if d_closes: last_close = d_closes[-1]

        # ATH count over last 30 trading days (or whatever history exists if shorter).
        # Short-history stocks (recent IPOs): every bar is a candidate new ATH.
        if d_highs:
            window = d_highs[-30:]
            baseline_pool = d_highs[:-30] if len(d_highs) > 30 else []
            running_max = max(baseline_pool) if baseline_pool else -float("inf")
            if ath and ath > (max(d_highs) if d_highs else -float("inf")):
                running_max = max(running_max, ath)  # true ATH older than 5y window
            for h in window:
                if h is not None and h > running_max:
                    ath_30d += 1
                    running_max = h

        if d_lows:
            window = d_lows[-30:]
            baseline_pool = d_lows[:-30] if len(d_lows) > 30 else []
            running_min = min(baseline_pool) if baseline_pool else float("inf")
            if atl and atl < (min(d_lows) if d_lows else float("inf")):
                running_min = min(running_min, atl)
            for l in window:
                if l is not None and l < running_min:
                    atl_30d += 1
                    running_min = l

        # NEW: "made ATH today" — today's daily high strictly exceeds the prior all-time max.
        # Prior max = max of all daily highs EXCLUDING today's bar.
        if last_high is not None and len(d_highs) > 1:
            prior_ath_5y = max(d_highs[:-1])
            # If true ATH lives outside the 5y daily window, use it as the floor for the comparison.
            prior_ath_floor = prior_ath_5y
            if ath is not None and ath > max(d_highs):
                prior_ath_floor = max(prior_ath_floor, ath)
            made_ath_today = last_high > prior_ath_floor

        if last_low is not None and len(d_lows) > 1:
            prior_atl_5y = min(d_lows[:-1])
            prior_atl_floor = prior_atl_5y
            if atl is not None and atl < min(d_lows):
                prior_atl_floor = min(prior_atl_floor, atl)
            made_atl_today = last_low < prior_atl_floor

        return (sym, ath, atl, wk52_h, wk52_l, ath_30d, atl_30d,
                last_high, last_low, last_close, made_ath_today, made_atl_today)
    except Exception:
        return sym, None, None, None, None, 0, 0, None, None, None, False, False

# ─── Hardcoded names + market caps ─────────────────────────────────────────
NAMES = {
    "BRK.B":"Berkshire Hathaway B","KO":"Coca-Cola","PEP":"PepsiCo","WMB":"Williams","EQT":"EQT Corp","D":"Dominion Energy",
    "AXP":"American Express","AMAT":"Applied Materials","NEM":"Newmont","COF":"Capital One","EMR":"Emerson Electric",
    "IR":"Ingersoll Rand","WSM":"Williams-Sonoma","CPAY":"Corpay","ALGN":"Align Technology","PCAR":"PACCAR","PNR":"Pentair",
    "DD":"DuPont","DPZ":"Domino's Pizza","BKNG":"Booking Holdings","CCL":"Carnival","LUV":"Southwest Airlines",
    "O":"Realty Income","CCI":"Crown Castle","YUM":"Yum! Brands","DLR":"Digital Realty","GM":"General Motors",
    "IRM":"Iron Mountain","LRCX":"Lam Research","BAC":"Bank of America","MA":"Mastercard","JPM":"JPMorgan",
    "INCY":"Incyte","STE":"STERIS","ALLE":"Allegion","APTV":"Aptiv","SHW":"Sherwin-Williams","DECK":"Deckers",
    "TGT":"Target","HD":"Home Depot","COST":"Costco","WMT":"Walmart","CMG":"Chipotle","ULTA":"Ulta Beauty",
    "GPC":"Genuine Parts","FFIV":"F5 Inc.","PSKY":"Paramount Skydance","BF.B":"Brown-Forman","WM":"Waste Management",
    "AMT":"American Tower","CL":"Colgate-Palmolive","ADBE":"Adobe","CRM":"Salesforce","INTU":"Intuit","BX":"Blackstone",
    "MDLZ":"Mondelez","ZBH":"Zimmer Biomet","ZTS":"Zoetis","HRL":"Hormel Foods","IDXX":"IDEXX Labs","MTB":"M&T Bank",
    "PNW":"Pinnacle West","CB":"Chubb","WTW":"Willis Towers Watson","CPRT":"Copart","EBAY":"eBay","BG":"Bunge",
    "VZ":"Verizon","FDS":"FactSet","DTE":"DTE Energy","AEE":"Ameren","ADM":"ADM","FSLR":"First Solar","PPL":"PPL Corp",
    "TRGP":"Targa Resources","UDR":"UDR Inc.","HIG":"Hartford","NKE":"Nike","GEN":"Gen Digital",
    "MPC":"Marathon Petroleum","PSX":"Phillips 66","VLO":"Valero Energy","WRB":"W.R. Berkley","DVA":"DaVita",
    "ACGL":"Arch Capital","COP":"ConocoPhillips","OXY":"Occidental","ADSK":"Autodesk","ROP":"Roper Technologies",
    "PAYX":"Paychex","ANET":"Arista","AVGO":"Broadcom","ETN":"Eaton","CIEN":"Ciena","COHR":"Coherent",
    "SYY":"Sysco","RL":"Ralph Lauren","EXPE":"Expedia","PPG":"PPG","GLW":"Corning","GRMN":"Garmin",
    "ARES":"Ares","ARE":"Alexandria RE","BIIB":"Biogen","BAX":"Baxter","NRG":"NRG","LITE":"Lumentum","CTAS":"Cintas",
    "POOL":"Pool Corp","DRI":"Darden","HSIC":"Henry Schein","LNT":"Alliant Energy","TPL":"Texas Pacific Land",
    "TRMB":"Trimble","SNA":"Snap-on","VRSK":"Verisk","PLTR":"Palantir","MLM":"Martin Marietta","ZBRA":"Zebra",
    "WELL":"Welltower","HLT":"Hilton","MAR":"Marriott","C":"Citi","ECL":"Ecolab","KEYS":"Keysight",
    "GWW":"Grainger","RMD":"ResMed","SBAC":"SBA Comms","UHS":"Universal Health","EQIX":"Equinix",
    "DLTR":"Dollar Tree","BRO":"Brown & Brown","ALB":"Albemarle","A":"Agilent","OTIS":"Otis","PNC":"PNC",
}
MCAPS = {
    "BRK.B":1100,"KO":330,"PEP":210,"WMB":70,"EQT":40,"D":50,
    "AXP":230,"AMAT":150,"NEM":120,"COF":110,"EMR":80,"IR":35,"WSM":24,"CPAY":22,"ALGN":17,"PCAR":50,"PNR":15,
    "DD":35,"DPZ":15,"BKNG":190,"CCL":30,"LUV":22,"O":50,"CCI":45,"YUM":45,"DLR":60,"GM":85,"IRM":34,"LRCX":92,
    "INCY":15,"STE":22,"ALLE":11,"APTV":15,"SHW":75,"DECK":17,"TGT":62,"HD":330,"COST":450,"WMT":700,
    "CMG":80,"ULTA":25,"GPC":20,"FFIV":15,"PSKY":15,"BF.B":13,"WM":80,"AMT":95,"CL":80,"ADBE":200,"CRM":280,
    "INTU":175,"BX":150,"MDLZ":95,"ZBH":25,"ZTS":80,"HRL":15,"IDXX":50,"MTB":25,"PNW":10,"CB":110,"WTW":35,
    "CPRT":60,"EBAY":30,"BG":13,"MA":520,"VZ":190,"FDS":18,"DTE":30,"AEE":25,"ADM":25,"FSLR":25,"PPL":25,
    "TRGP":40,"UDR":13,"HIG":42,"NKE":67,"GEN":17,"MPC":50,"PSX":50,"VLO":50,"WRB":18,"DVA":15,
    "ACGL":52,"COP":135,"OXY":42,"ADSK":60,"ROP":62,"PAYX":55,"ANET":150,"AVGO":1300,"ETN":110,"CIEN":40,
    "COHR":48,"SYY":35,"RL":25,"EXPE":24,"PPG":24,"GLW":35,"GRMN":56,"ARES":58,"ARE":7,"BIIB":21,"BAX":15,
    "NRG":25,"LITE":11,"CTAS":85,"POOL":11,"DRI":24,"HSIC":9,"LNT":15,"TPL":33,"TRMB":15,"SNA":15,"VRSK":40,
    "PLTR":270,"MLM":40,"ZBRA":13,"WELL":75,"HLT":80,"MAR":98,"C":140,"ECL":75,"KEYS":58,"GWW":51,"RMD":33,
    "SBAC":23,"UHS":12,"EQIX":80,"DLTR":18,"BRO":18,"ALB":21,"A":33,"OTIS":33,"PNC":52,
}

# ─── Run ───────────────────────────────────────────────────────────────────
TIMEFRAMES = {
    # name        interval   range
    "daily":   ("1d",  "1y"),
    "weekly":  ("1wk", "5y"),
    "monthly": ("1mo", "max"),
}

def run_scan(timeframe="daily"):
    interval, rng = TIMEFRAMES[timeframe]
    t0 = time.time()
    MIN_OK = 100  # below this, treat as Yahoo rate-limit / network failure and skip the write
    closes_map = {}
    ohlc_map = {}
    with ThreadPoolExecutor(max_workers=25) as ex:
        for f in as_completed([ex.submit(fetch, s, interval, rng) for s in TICKERS]):
            sym, ohlc = f.result()
            if ohlc and ohlc.get("closes"):
                ohlc_map[sym] = ohlc
                closes_map[sym] = ohlc["closes"]

    if len(closes_map) < MIN_OK:
        print(f"[{datetime.now(timezone.utc).isoformat()}] [{timeframe}] ABORT — only {len(closes_map)} tickers fetched "
              f"(< {MIN_OK} threshold). Likely Yahoo rate-limit. Keeping previous results.json untouched.")
        return

    rows = []
    for sym, ohlc in ohlc_map.items():
        closes = ohlc["closes"]
        fvb = iq_bands_state(ohlc)
        bxt = iq_osc_state(closes)
        if not fvb or not bxt: continue
        fvb_g = fvb["today_bull"] and not fvb["yest_bull"]
        fvb_r = (not fvb["today_bull"]) and fvb["yest_bull"]
        bxt_g = bxt["today"] > 0 and bxt["yest"] <= 0
        bxt_r = bxt["today"] < 0 and bxt["yest"] >= 0
        if not (fvb_g or fvb_r or bxt_g or bxt_r): continue
        nm = UNIVERSE_NAMES.get(sym) or NAMES.get(sym, sym)
        fvb_streak = iq_bands_prior_days(ohlc) if (fvb_g or fvb_r) else None
        bxt_streak = iq_osc_prior_days(closes) if (bxt_g or bxt_r) else None
        rows.append({
            "sym": sym, "name": nm, "mcap": live_mcap(sym, fvb["price"]),
            "price": round(fvb["price"], 2), "basis": round(fvb["basis"], 2),
            "bxt_today": round(bxt["today"], 2), "bxt_yest": round(bxt["yest"], 2),
            "fvb_g": fvb_g, "fvb_r": fvb_r, "bxt_g": bxt_g, "bxt_r": bxt_r,
            "fvb_streak": fvb_streak, "bxt_streak": bxt_streak,
        })

    # 4 separate flip categories (sorted by mcap desc)
    sort_mcap = lambda x: -x["mcap"]
    fvb_green_list = sorted([r for r in rows if r["fvb_g"]], key=sort_mcap)
    fvb_red_list   = sorted([r for r in rows if r["fvb_r"]], key=sort_mcap)
    bxt_green_list = sorted([r for r in rows if r["bxt_g"]], key=sort_mcap)
    bxt_red_list   = sorted([r for r in rows if r["bxt_r"]], key=sort_mcap)

    # Fetch ATH/ATL for any flipped ticker (union across all 4 categories)
    flipped_syms = {r["sym"] for r in fvb_green_list + fvb_red_list + bxt_green_list + bxt_red_list}
    ath_map = {}; atl_map = {}
    wk52h_map = {}; wk52l_map = {}
    ath30d_map = {}; atl30d_map = {}
    with ThreadPoolExecutor(max_workers=15) as ex:
        for f in as_completed([ex.submit(fetch_ath, s) for s in flipped_syms]):
            sym, ath, atl, wk52h, wk52l, ath30d, atl30d, _, _, _, _, _ = f.result()
            ath_map[sym] = ath
            atl_map[sym] = atl
            wk52h_map[sym] = wk52h
            wk52l_map[sym] = wk52l
            ath30d_map[sym] = ath30d
            atl30d_map[sym] = atl30d
    for r in rows:
        ath = ath_map.get(r["sym"])
        atl = atl_map.get(r["sym"])
        wk52h = wk52h_map.get(r["sym"])
        wk52l = wk52l_map.get(r["sym"])
        r["ath"] = round(ath, 2) if ath else None
        r["atl"] = round(atl, 2) if atl else None
        r["pct_to_ath"] = round((ath - r["price"]) / r["price"] * 100, 1) if ath else None
        r["pct_to_atl"] = round((r["price"] - atl) / atl * 100, 1) if atl else None
        r["wk52_high"] = round(wk52h, 2) if wk52h else None
        r["wk52_low"]  = round(wk52l, 2) if wk52l else None
        r["ath_30d"] = ath30d_map.get(r["sym"], 0)
        r["atl_30d"] = atl30d_map.get(r["sym"], 0)

    # Diff vs previous run — track all 4 categories
    out_path = os.path.join(HERE, "docs", "results.json" if timeframe == "daily" else f"results_{timeframe}.json")
    prev = {}
    prev_sets = {"fvb_green": set(), "fvb_red": set(), "bxt_green": set(), "bxt_red": set()}
    prev_ts = None
    try:
        with open(out_path) as f:
            prev = json.load(f)
        for k in prev_sets:
            prev_sets[k] = {r["sym"] for r in prev.get(k, [])}
        prev_ts = prev.get("updated_at")
    except Exception:
        pass

    cur_lists = {
        "fvb_green": fvb_green_list, "fvb_red": fvb_red_list,
        "bxt_green": bxt_green_list, "bxt_red": bxt_red_list,
    }
    cur_sets = {k: {r["sym"] for r in v} for k, v in cur_lists.items()}
    cur_lookup = {r["sym"]: r for lst in cur_lists.values() for r in lst}
    prev_lookup = {}
    try:
        for k in prev_sets:
            for pr in prev.get(k, []):
                prev_lookup[pr["sym"]] = pr
    except Exception: pass

    def enrich(sym, action):
        src = cur_lookup.get(sym) if action == "added" else prev_lookup.get(sym, cur_lookup.get(sym))
        if not src:
            return {"sym": sym, "name": UNIVERSE_NAMES.get(sym) or NAMES.get(sym, sym), "mcap": 0}
        return {
            "sym": src["sym"], "name": src.get("name", sym),
            "mcap": src.get("mcap", 0),
            "price": src.get("price"), "basis": src.get("basis"),
            "bxt_today": src.get("bxt_today"),
            "ath": src.get("ath"), "pct_to_ath": src.get("pct_to_ath"),
            "wk52_high": src.get("wk52_high"),
            "wk52_low":  src.get("wk52_low"),
            "ath_30d": src.get("ath_30d", 0),
        }
    def annotate(syms, action):
        items = [enrich(s, action) for s in syms]
        return sorted(items, key=lambda x: -(x.get("mcap") or 0))

    changes = {"compared_to": prev_ts}
    for k in cur_sets:
        changes[f"{k}_added"]   = annotate(cur_sets[k] - prev_sets[k], "added")
        changes[f"{k}_removed"] = annotate(prev_sets[k] - cur_sets[k], "removed")

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "timeframe": timeframe,
        "scan_seconds": round(time.time() - t0, 2),
        "scanned_count": len(closes_map),
        "fvb_green": fvb_green_list,
        "fvb_red":   fvb_red_list,
        "bxt_green": bxt_green_list,
        "bxt_red":   bxt_red_list,
        "changes": changes,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    n_changes = sum(len(v) for k, v in changes.items() if isinstance(v, list))

    # ─── Supabase dual-write (no-op when SUPABASE_* env vars unset) ─────
    try:
        import supabase_client as sb
        flip_rows = []
        BUCKETS = (
            ("fvb_green", "fvb", "green"),
            ("fvb_red",   "fvb", "red"),
            ("bxt_green", "bxt", "green"),
            ("bxt_red",   "bxt", "red"),
        )
        # Wipe just THIS timeframe's rows so we don't kill the other slices.
        sb.truncate("flip_results", where_filter=f"timeframe=eq.{timeframe}")
        for key, product, side in BUCKETS:
            for r in out[key]:
                flip_rows.append({
                    "timeframe": timeframe, "product": product, "side": side,
                    "symbol": r.get("sym"), "name": r.get("name") or "",
                    "mcap": r.get("mcap") or 0, "price": r.get("price"),
                    "basis": r.get("basis"), "streak": r.get(f"{product}_streak"),
                    "bxt_today": r.get("bxt_today"),
                    "scanned_at": out["updated_at"],
                })
        n_flips = sb.upsert("flip_results", flip_rows, on_conflict="timeframe,product,side,symbol")
        sb.record_run("scan_py", out["updated_at"], out["updated_at"], True, n_flips,
                      f"{timeframe} · {len(closes_map)} scanned · {n_flips} flips")
    except Exception as e:
        print(f"  [supabase] dual-write skipped: {e}")

    # ─── Append events + snapshot to history logs ──────────────────────
    now_iso = out["updated_at"]
    events = []
    EVENT_TYPE = {
        "fvb_green": "iq_bands_green",
        "fvb_red":   "iq_bands_red",
        "bxt_green": "iq_osc_green",
        "bxt_red":   "iq_osc_red",
    }
    for k, etype in EVENT_TYPE.items():
        for r in changes.get(f"{k}_added", []):
            events.append({
                "ts":   now_iso,
                "tf":   timeframe,
                "sym":  r["sym"],
                "name": r.get("name"),
                "event": etype,
                "action": "flip",  # opened a new regime
                "price":  r.get("price"),
                "basis":  r.get("basis"),
                "osc":    r.get("bxt_today"),
                "mcap":   r.get("mcap"),
                "sector":   sector_of(r["sym"]),
                "industry": industry_of(r["sym"]),
            })
        for r in changes.get(f"{k}_removed", []):
            events.append({
                "ts":   now_iso,
                "tf":   timeframe,
                "sym":  r["sym"],
                "name": r.get("name"),
                "event": etype,
                "action": "unflip",  # closed a prior regime
                "price":  r.get("price"),
                "basis":  r.get("basis"),
                "osc":    r.get("bxt_today"),
                "mcap":   r.get("mcap"),
                "sector":   sector_of(r["sym"]),
                "industry": industry_of(r["sym"]),
            })
    _append_jsonl(EVENTS_PATH, events)

    _append_jsonl(SNAPSHOTS_PATH, [{
        "ts":     now_iso,
        "tf":     timeframe,
        "scanned": out["scanned_count"],
        "fvb_g":  len(fvb_green_list),
        "fvb_r":  len(fvb_red_list),
        "bxt_g":  len(bxt_green_list),
        "bxt_r":  len(bxt_red_list),
        "events": len(events),
    }])

    # Cache OHLC bars for EVERY clickable ticker on the dashboard:
    # flipped tickers + every "removed" row in the changes table. Always re-fetch
    # so bars files never go stale even when a ticker stays in the changes
    # accumulator longer than a single cron tick.
    clickable_syms = set(flipped_syms)
    for k in cur_sets:
        for r in changes.get(f"{k}_removed", []):
            clickable_syms.add(r["sym"])
    bars_dir = os.path.join(HERE, "docs", "bars" if timeframe == "daily" else f"bars_{timeframe}")
    os.makedirs(bars_dir, exist_ok=True)
    with ThreadPoolExecutor(max_workers=15) as ex:
        for f in as_completed([ex.submit(fetch_ohlc, s, interval, rng) for s in clickable_syms]):
            sym, bars = f.result()
            if bars:
                with open(os.path.join(bars_dir, f"{sym}.json"), "w") as fh:
                    json.dump(bars, fh, separators=(',', ':'))

    print(f"[{out['updated_at']}] [{timeframe}] scanned {out['scanned_count']} in {out['scan_seconds']}s — "
          f"FVB {len(fvb_green_list)}g/{len(fvb_red_list)}r, BXT {len(bxt_green_list)}g/{len(bxt_red_list)}r "
          f"({n_changes} changes, {len(clickable_syms)} bars refreshed)")

def dual_write_extrema(ath_path, atl_path, now_iso):
    """Mirror ath_list.json / atl_list.json into Supabase `extrema_events`.

    Reads back what was just written to disk rather than re-deriving the rows,
    so the table can't drift from the JSON the free dashboard also serves.
    No-op when SUPABASE_* env vars are unset.
    """
    try:
        import supabase_client as sb
        extrema_rows = []
        counts = {}
        for kind, path in (("ath", ath_path), ("atl", atl_path)):
            # The JSON keys are kind-specific; the table columns are generic.
            today_key = "last_high" if kind == "ath" else "last_low"
            pct_key   = "pct_to_ath" if kind == "ath" else "pct_to_atl"
            cnt_key   = "ath_30d"    if kind == "ath" else "atl_30d"

            def to_row(r, is_today, occurred_at):
                return {
                    "kind": kind, "is_today": is_today,
                    "symbol": r.get("sym"), "name": r.get("name") or "",
                    "mcap": r.get("mcap") or 0, "price": r.get("price"),
                    "ath_or_atl": r.get(kind),
                    "today_extreme":  r.get(today_key),
                    "pct_to_extreme": r.get(pct_key),
                    "wk52_high":      r.get("wk52_high"),
                    "wk52_low":       r.get("wk52_low"),
                    "extreme_30d":    r.get(cnt_key),
                    "occurred_at": occurred_at,
                    "scanned_at": now_iso,
                }

            with open(path) as f:
                doc = json.load(f)
            counts[kind] = len(doc.get("list", []))
            # is_today rows are today's fresh extrema; the accumulator holds
            # symbols first seen on earlier runs. The PK is (kind, is_today,
            # symbol), so a symbol in both slices doesn't collide.
            for r in doc.get("list", []):
                extrema_rows.append(to_row(r, True, doc.get("updated_at")))
            for e in doc.get("accumulator", []):
                extrema_rows.append(to_row(e, False, e.get("first_seen_at")))
            # Wipe just THIS kind so a symbol that stops printing extrema today
            # doesn't linger as a stale is_today row.
            sb.truncate("extrema_events", where_filter=f"kind=eq.{kind}")
        n = sb.upsert("extrema_events", extrema_rows, on_conflict="kind,is_today,symbol")
        # scan_runs.pipeline is CHECK-constrained to the four known pipelines,
        # so this logs under scan_py and leans on notes to mark the ath/atl leg.
        sb.record_run("scan_py", now_iso, now_iso, True, n,
                      f"ath/atl · {counts.get('ath', 0)} ath · {counts.get('atl', 0)} atl · {n} rows")
        return n
    except Exception as e:
        print(f"  [supabase] ath/atl dual-write skipped: {e}")
        return 0


def run_ath_atl_universe():
    """Scan FULL universe for stocks currently making ATHs / ATLs.
    Writes docs/ath_list.json + docs/atl_list.json. Caches raw fetch results
    for 60 minutes to avoid hammering Yahoo on every cron tick.
    """
    t0 = time.time()
    ATH_REFRESH_MIN = 60
    cache_path = os.path.join(HERE, "docs", "ath_atl_cache.json")
    ath_path   = os.path.join(HERE, "docs", "ath_list.json")
    atl_path   = os.path.join(HERE, "docs", "atl_list.json")

    do_full_scan = True
    cache_data = {}
    try:
        with open(cache_path) as f:
            cache_data = json.load(f)
        last_ts_iso = cache_data.get("updated_at")
        if last_ts_iso:
            last_ts = datetime.fromisoformat(last_ts_iso.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - last_ts).total_seconds() < ATH_REFRESH_MIN * 60:
                do_full_scan = False
    except Exception:
        pass

    if do_full_scan:
        results = {}
        with ThreadPoolExecutor(max_workers=20) as ex:
            for f in as_completed([ex.submit(fetch_ath, s) for s in TICKERS]):
                r = f.result()
                if r[1] is None:  # ath is None — fetch failed
                    continue
                results[r[0]] = list(r[1:])  # drop sym from tuple
        cache_data = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "results": results,
        }
        with open(cache_path, "w") as f:
            json.dump(cache_data, f)

    cached = cache_data.get("results", {})
    ath_list, atl_list = [], []
    for sym, vals in cached.items():
        # Tolerate the older 9-field cache while we transition; default the new flags to False.
        if len(vals) >= 11:
            ath, atl, wk52h, wk52l, ath30d, atl30d, last_high, last_low, last_close, made_ath_today, made_atl_today = vals[:11]
        else:
            ath, atl, wk52h, wk52l, ath30d, atl30d, last_high, last_low, last_close = vals[:9]
            made_ath_today = made_atl_today = False
        if last_close is None: continue
        nm = UNIVERSE_NAMES.get(sym) or NAMES.get(sym, sym)
        mcap = live_mcap(sym, last_close)
        row = {
            "sym": sym, "name": nm, "mcap": mcap,
            "price": round(last_close, 2),
            "ath": round(ath, 2) if ath else None,
            "atl": round(atl, 2) if atl else None,
            "pct_to_ath": round((ath - last_close) / last_close * 100, 1) if ath else None,
            "pct_to_atl": round((last_close - atl) / atl * 100, 1) if atl else None,
            "wk52_high": round(wk52h, 2) if wk52h else None,
            "wk52_low":  round(wk52l, 2) if wk52l else None,
            "ath_30d": ath30d,
            "atl_30d": atl30d,
            "last_high": round(last_high, 2) if last_high else None,
            "last_low":  round(last_low, 2)  if last_low  else None,
            "made_ath_today": made_ath_today,
            "made_atl_today": made_atl_today,
        }
        # ATH / ATL list now strictly requires a NEW extremum printed TODAY.
        if made_ath_today: ath_list.append(row)
        if made_atl_today: atl_list.append(row)
    ath_list.sort(key=lambda x: -(x.get("mcap") or 0))
    atl_list.sort(key=lambda x: -(x.get("mcap") or 0))

    def write_with_accumulator(path, cur_list):
        """Persist current list + an append-only accumulator of newly-seen symbols."""
        prev_syms = set()
        accumulator = []
        try:
            with open(path) as f:
                prev = json.load(f)
                prev_syms = {r["sym"] for r in prev.get("list", [])}
                accumulator = prev.get("accumulator", [])
        except Exception:
            pass
        cur_lookup = {r["sym"]: r for r in cur_list}
        new_syms = sorted({r["sym"] for r in cur_list} - prev_syms)
        now_iso = datetime.now(timezone.utc).isoformat()
        seen_in_acc = {e["sym"] for e in accumulator}
        for s in new_syms:
            entry = dict(cur_lookup[s])
            entry["first_seen_at"] = now_iso
            if s in seen_in_acc:
                # Already in accumulator from a prior cycle — refresh, don't duplicate
                accumulator = [e for e in accumulator if e["sym"] != s]
            accumulator.append(entry)
        accumulator = accumulator[-200:]  # cap

        with open(path, "w") as f:
            json.dump({
                "updated_at": now_iso,
                "count": len(cur_list),
                "list": cur_list,
                "accumulator": accumulator,
                "added_this_run": new_syms,
            }, f, indent=2)
        return new_syms, cur_lookup

    ath_new, ath_lookup = write_with_accumulator(ath_path, ath_list)
    atl_new, atl_lookup = write_with_accumulator(atl_path, atl_list)

    # Append ATH/ATL events to history log.
    now_iso = datetime.now(timezone.utc).isoformat()
    events = []
    for s in ath_new:
        r = ath_lookup[s]
        events.append({
            "ts":   now_iso, "tf": "ath", "sym": s, "name": r.get("name"),
            "event": "ath_today", "action": "new",
            "price":      r.get("price"),
            "today_high": r.get("last_high"),
            "ath":        r.get("ath"),
            "ath_30d":    r.get("ath_30d"),
            "mcap":       r.get("mcap"),
            "sector":   sector_of(s),
            "industry": industry_of(s),
        })
    for s in atl_new:
        r = atl_lookup[s]
        events.append({
            "ts":   now_iso, "tf": "atl", "sym": s, "name": r.get("name"),
            "event": "atl_today", "action": "new",
            "price":     r.get("price"),
            "today_low": r.get("last_low"),
            "atl":       r.get("atl"),
            "atl_30d":   r.get("atl_30d"),
            "mcap":      r.get("mcap"),
            "sector":   sector_of(s),
            "industry": industry_of(s),
        })
    _append_jsonl(EVENTS_PATH, events)

    _append_jsonl(SNAPSHOTS_PATH, [{
        "ts":   now_iso,
        "tf":   "ath_atl",
        "ath":  len(ath_list),
        "atl":  len(atl_list),
        "ath_new": len(ath_new),
        "atl_new": len(atl_new),
    }])

    dual_write_extrema(ath_path, atl_path, now_iso)

    # Cache OHLC bars for every clickable ATH/ATL row — list + accumulator.
    # Always re-fetch so accumulator entries from prior days don't show stale charts.
    bars_dir = os.path.join(HERE, "docs", "bars")
    os.makedirs(bars_dir, exist_ok=True)
    clickable = {r["sym"] for r in ath_list} | {r["sym"] for r in atl_list}
    for path in (ath_path, atl_path):
        try:
            with open(path) as f:
                clickable |= {e["sym"] for e in json.load(f).get("accumulator", [])}
        except Exception: pass
    if clickable:
        with ThreadPoolExecutor(max_workers=15) as ex:
            for f in as_completed([ex.submit(fetch_ohlc, s, "1d", "1y") for s in clickable]):
                sym, bars = f.result()
                if bars:
                    with open(os.path.join(bars_dir, f"{sym}.json"), "w") as fh:
                        json.dump(bars, fh, separators=(',', ':'))

    print(f"[{datetime.now(timezone.utc).isoformat()}] [ath-atl-universe] "
          f"{'FULL SCAN' if do_full_scan else 'cached'} in {round(time.time()-t0,1)}s — "
          f"ath_list={len(ath_list)}, atl_list={len(atl_list)}, "
          f"{len(clickable)} bars refreshed")

if __name__ == "__main__":
    run_scan("daily")
    run_scan("weekly")
    run_scan("monthly")
    run_ath_atl_universe()
