"""
NSE quarterly results -> Telegram channel bot.

What it does each time it runs:
1. Asks NSE for the list of quarterly results filed recently.
2. Skips companies it has already handled (remembered in state.json).
3. For each new company: gets market cap and sales/profit numbers
   (this quarter vs same quarter last year) from Yahoo Finance.
4. If market cap is above 500 crore, posts a card to your Telegram channel
   with a rating: Excellent, Good, Average or Poor.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta

import requests
import yfinance as yf

# ----------------------------- SETTINGS ------------------------------
MIN_MARKET_CAP_CRORE = 500      # only post companies bigger than this
LOOKBACK_DAYS = 2               # only look at results filed in the last N days
GIVE_UP_AFTER_DAYS = 3          # stop waiting for Yahoo numbers after N days
STATE_FILE = "state.json"
# ---------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")   # e.g. @mychannel

NSE_HOME = "https://www.nseindia.com"
NSE_API = (NSE_HOME + "/api/corporates-financial-results"
           "?index=equities&period=Quarterly")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOME + "/companies-listing/corporate-filings-financial-results",
}


# ------------------------------ STATE --------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"done": [], "pending": {}, "initialised": False}


def save_state(state):
    state["done"] = state["done"][-3000:]   # keep file small
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


# ------------------------------- NSE ---------------------------------
def parse_date(text):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except Exception:
            pass
    return None


def fetch_nse_results():
    s = requests.Session()
    s.headers.update(HEADERS)
    # NSE needs a normal page visit first to hand out cookies.
    s.get(NSE_HOME, timeout=30)
    time.sleep(1)
    r = s.get(NSE_API, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        data = data.get("data", [])
    return data


# --------------------------- NUMBERS (Yahoo) --------------------------
def crore(x):
    return x / 1e7


def pct_change(new, old):
    if old is None or old == 0:
        return None
    return (new - old) / abs(old) * 100


def get_numbers(symbol, quarter_end):
    """Return dict with this-quarter and last-year-quarter numbers,
    or None if Yahoo has not published them yet."""
    t = yf.Ticker(symbol + ".NS")
    try:
        mcap = t.fast_info["market_cap"]
    except Exception:
        mcap = None
    df = t.quarterly_income_stmt
    if df is None or df.empty:
        return {"mcap": mcap, "ready": False}

    cols = sorted(df.columns, reverse=True)
    latest = cols[0]
    # Yahoo must already show the quarter NSE just announced
    if quarter_end and abs((latest.to_pydatetime() - quarter_end).days) > 15:
        return {"mcap": mcap, "ready": False}

    # find the column about one year earlier
    target = latest - timedelta(days=365)
    prev = None
    for c in cols[1:]:
        if abs((c - target).days) <= 20:
            prev = c
            break
    if prev is None:
        return {"mcap": mcap, "ready": False}

    def row(name, col):
        for key in name:
            if key in df.index:
                v = df.loc[key, col]
                if v == v:           # not NaN
                    return float(v)
        return None

    rev_names = ["Total Revenue", "Operating Revenue"]
    np_names = ["Net Income", "Net Income Common Stockholders"]
    out = {
        "mcap": mcap, "ready": True, "quarter": latest,
        "sales": row(rev_names, latest), "sales_ly": row(rev_names, prev),
        "profit": row(np_names, latest), "profit_ly": row(np_names, prev),
    }
    if None in (out["sales"], out["sales_ly"], out["profit"], out["profit_ly"]):
        return {"mcap": mcap, "ready": False}
    return out


# ------------------------------ RATING --------------------------------
def rate(sales_g, profit_g, margin_change_pp, profit, profit_ly):
    """Simple points system. Edit the numbers here if you want
    to be stricter or more relaxed."""
    pts = 0
    # sales growth vs last year
    if sales_g is None:
        pass
    elif sales_g >= 15:
        pts += 2
    elif sales_g >= 5:
        pts += 1
    elif sales_g < 0:
        pts -= 1
    # profit growth vs last year
    if profit <= 0 and profit_ly > 0:
        pts -= 2                      # profit turned into a loss
    elif profit_g is None:
        pass
    elif profit_g >= 25:
        pts += 2
    elif profit_g >= 5:
        pts += 1
    elif profit_g < 0:
        pts -= 1
    # margin change (in percentage points)
    if margin_change_pp >= 1:
        pts += 1
    elif margin_change_pp <= -1:
        pts -= 1

    if pts >= 4:
        return "Excellent", "🟢"
    if pts >= 2:
        return "Good", "🔵"
    if pts >= 0:
        return "Average", "🟡"
    return "Poor", "🔴"


def arrow(g):
    if g is None:
        return "n/a"
    return f"{'▲' if g >= 0 else '▼'} {abs(g):.1f}%"


def build_card(name, symbol, n):
    sales_g = pct_change(n["sales"], n["sales_ly"])
    profit_g = pct_change(n["profit"], n["profit_ly"])
    m = n["profit"] / n["sales"] * 100
    m_ly = n["profit_ly"] / n["sales_ly"] * 100
    diff = m - m_ly
    tag, dot = rate(sales_g, profit_g, diff, n["profit"], n["profit_ly"])
    q = n["quarter"].strftime("%b %Y")
    return (
        f"{dot} <b>{name}</b> ({symbol})\n"
        f"Quarter ended {q}  |  Rating: <b>{tag}</b>\n\n"
        f"💰 Sales: ₹{crore(n['sales']):,.0f} Cr  (last yr ₹{crore(n['sales_ly']):,.0f} Cr)  {arrow(sales_g)}\n"
        f"📈 Net profit: ₹{crore(n['profit']):,.0f} Cr  (last yr ₹{crore(n['profit_ly']):,.0f} Cr)  {arrow(profit_g)}\n"
        f"📊 Profit margin: {m:.1f}%  (last yr {m_ly:.1f}%)  "
        f"{'▲' if diff >= 0 else '▼'} {abs(diff):.1f} pts\n"
        f"🏢 Market cap: ₹{crore(n['mcap']):,.0f} Cr\n\n"
        f"<i>Automatic summary, not investment advice.</i>"
    )


# ------------------------------ TELEGRAM ------------------------------
def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={
        "chat_id": CHANNEL, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text}")


# -------------------------------- MAIN --------------------------------
def main():
    if not BOT_TOKEN or not CHANNEL:
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL secret.")

    state = load_state()
    done = set(state["done"])
    pending = state["pending"]
    now = datetime.now()

    rows = fetch_nse_results()
    print(f"NSE returned {len(rows)} rows")

    # First ever run: remember everything silently so the channel
    # is not flooded with old results.
    if not state["initialised"]:
        for r in rows:
            done.add(f"{r.get('symbol')}|{r.get('toDate')}")
        state.update(done=sorted(done), initialised=True)
        save_state(state)
        print("First run: remembered existing results, nothing posted.")
        return

    # add newly filed results to the pending list
    for r in rows:
        symbol = r.get("symbol")
        qdate = r.get("toDate")
        key = f"{symbol}|{qdate}"
        if not symbol or key in done or key in pending:
            continue
        filed = parse_date(r.get("broadCastDate") or r.get("filingDate") or "")
        if filed and now - filed > timedelta(days=LOOKBACK_DAYS):
            done.add(key)           # old, ignore
            continue
        pending[key] = {"symbol": symbol, "name": r.get("companyName", symbol),
                        "qdate": qdate, "first_seen": now.isoformat()}

    # work through the pending list
    for key in list(pending):
        p = pending[key]
        qd = parse_date(p["qdate"] or "")
        try:
            n = get_numbers(p["symbol"], qd)
        except Exception as e:
            print(f"{p['symbol']}: error {e}")
            n = None
        age = now - datetime.fromisoformat(p["first_seen"])

        if n is None or not n.get("ready"):
            if age > timedelta(days=GIVE_UP_AFTER_DAYS):
                print(f"{p['symbol']}: numbers never appeared, giving up")
                done.add(key); del pending[key]
            continue      # try again on the next run

        mcap = n.get("mcap")
        if mcap is None or crore(mcap) < MIN_MARKET_CAP_CRORE:
            print(f"{p['symbol']}: below market cap limit, skipped")
            done.add(key); del pending[key]
            continue

        try:
            send_telegram(build_card(p["name"], p["symbol"], n))
            print(f"{p['symbol']}: posted")
            done.add(key); del pending[key]
            time.sleep(3)
        except Exception as e:
            print(f"{p['symbol']}: Telegram failed: {e}")

    state.update(done=sorted(done), pending=pending)
    save_state(state)


if __name__ == "__main__":
    main()
