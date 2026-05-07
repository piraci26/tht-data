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

def live_mcap(sym, price):
    """Live mcap in $B = price × shares_outstanding / 1e9. Falls back to MCAPS dict."""
    s = SHARES.get(sym)
    if s and price:
        return round(price * s / 1e9)
    return MCAPS.get(sym, 0)

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

def fvb_prior_days(closes, length=20):
    """If FVB flipped today, count how many bars the PRIOR regime lasted (yesterday looking back)."""
    n = len(closes)
    if n < length + 2: return None
    # Rolling SMA series
    basis = [None] * n
    s = sum(closes[:length])
    basis[length - 1] = s / length
    for i in range(length, n):
        s += closes[i] - closes[i - length]
        basis[i] = s / length
    if basis[-1] is None or basis[-2] is None: return None
    today_bull = closes[-1] > basis[-1]
    yest_bull  = closes[-2] > basis[-2]
    if today_bull == yest_bull: return None
    prior_bull = yest_bull
    days = 0
    for i in range(n - 2, length - 2, -1):
        if (closes[i] > basis[i]) == prior_bull:
            days += 1
        else:
            break
    return days

def bxt_prior_days(closes, length=15):
    """If BXT flipped today (crossed zero), count how many bars the PRIOR sign lasted."""
    e5  = ema_series(closes, 5)
    e20 = ema_series(closes, 20)
    diff = [a - b if a is not None and b is not None else None for a, b in zip(e5, e20)]
    valid = [d for d in diff if d is not None]
    if len(valid) < length + 2: return None
    rsi_vals = rsi_series(valid, length)
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

def fvb_state(closes, length=20):
    if len(closes) < length + 2: return None
    basis_today = sma(closes, length)
    basis_yest  = sma(closes[:-1], length)
    return {
        "today_bull": closes[-1] > basis_today,
        "yest_bull":  closes[-2] > basis_yest,
        "basis": basis_today,
        "price": closes[-1],
    }

def bxt_state(closes):
    if len(closes) < 80: return None
    e5  = ema_series(closes, 5)
    e20 = ema_series(closes, 20)
    diff = []
    for i in range(len(closes)):
        if e5[i] is not None and e20[i] is not None:
            diff.append(e5[i] - e20[i])
        else:
            diff.append(None)
    diff_clean = [d for d in diff if d is not None]
    if len(diff_clean) < 16: return None
    rsi_vals = rsi_series(diff_clean, 15)
    short_term = [r - 50 if r is not None else None for r in rsi_vals]
    short_clean = [s for s in short_term if s is not None]
    if len(short_clean) < 2: return None
    return {"today": short_clean[-1], "yest": short_clean[-2]}

# ─── Yahoo fetch ───────────────────────────────────────────────────────────
def fetch(sym, interval="1d", rng="1y"):
    sym_q = sym.replace(".", "-")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_q}?range={rng}&interval={interval}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        result = data["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        return sym, closes
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
    sym_q = sym.replace(".","-")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_q}?range=max&interval=1wk"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        result = data["chart"]["result"][0]
        high = [c for c in result["indicators"]["quote"][0]["high"] if c is not None]
        return sym, max(high) if high else None
    except Exception:
        return sym, None

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
    with ThreadPoolExecutor(max_workers=25) as ex:
        for f in as_completed([ex.submit(fetch, s, interval, rng) for s in TICKERS]):
            sym, closes = f.result()
            if closes: closes_map[sym] = closes

    if len(closes_map) < MIN_OK:
        print(f"[{datetime.now(timezone.utc).isoformat()}] [{timeframe}] ABORT — only {len(closes_map)} tickers fetched "
              f"(< {MIN_OK} threshold). Likely Yahoo rate-limit. Keeping previous results.json untouched.")
        return

    rows = []
    for sym, closes in closes_map.items():
        fvb = fvb_state(closes)
        bxt = bxt_state(closes)
        if not fvb or not bxt: continue
        fvb_g = fvb["today_bull"] and not fvb["yest_bull"]
        fvb_r = (not fvb["today_bull"]) and fvb["yest_bull"]
        bxt_g = bxt["today"] > 0 and bxt["yest"] <= 0
        bxt_r = bxt["today"] < 0 and bxt["yest"] >= 0
        if not (fvb_g or fvb_r or bxt_g or bxt_r): continue
        nm = UNIVERSE_NAMES.get(sym) or NAMES.get(sym, sym)
        fvb_streak = fvb_prior_days(closes) if (fvb_g or fvb_r) else None
        bxt_streak = bxt_prior_days(closes) if (bxt_g or bxt_r) else None
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

    # Fetch ATH for any flipped ticker (union across all 4 categories)
    flipped_syms = {r["sym"] for r in fvb_green_list + fvb_red_list + bxt_green_list + bxt_red_list}
    ath_map = {}
    with ThreadPoolExecutor(max_workers=15) as ex:
        for f in as_completed([ex.submit(fetch_ath, s) for s in flipped_syms]):
            sym, ath = f.result()
            ath_map[sym] = ath
    for r in rows:
        ath = ath_map.get(r["sym"])
        r["ath"] = round(ath, 2) if ath else None
        r["pct_to_ath"] = round((ath - r["price"]) / r["price"] * 100, 1) if ath else None

    # Cache OHLC bars for ALL flipped tickers (one folder per timeframe)
    bars_dir = os.path.join(HERE, "docs", "bars" if timeframe == "daily" else f"bars_{timeframe}")
    os.makedirs(bars_dir, exist_ok=True)
    with ThreadPoolExecutor(max_workers=15) as ex:
        for f in as_completed([ex.submit(fetch_ohlc, s, interval, rng) for s in flipped_syms]):
            sym, bars = f.result()
            if bars:
                with open(os.path.join(bars_dir, f"{sym}.json"), "w") as fh:
                    json.dump(bars, fh, separators=(',', ':'))

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
    print(f"[{out['updated_at']}] [{timeframe}] scanned {out['scanned_count']} in {out['scan_seconds']}s — "
          f"FVB {len(fvb_green_list)}g/{len(fvb_red_list)}r, BXT {len(bxt_green_list)}g/{len(bxt_red_list)}r ({n_changes} changes)")

if __name__ == "__main__":
    run_scan("daily")
    run_scan("weekly")
    run_scan("monthly")
