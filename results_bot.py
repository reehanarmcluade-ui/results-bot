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

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta

import requests
import yfinance as yf
from PIL import Image, ImageDraw, ImageFont

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


# ----------------------------- IMAGE CARD ------------------------------
TAG_COLORS = {"Excellent": (27, 158, 90), "Good": (47, 111, 222),
              "Average": (214, 150, 0), "Poor": (211, 63, 73)}
GREEN, RED = (27, 158, 90), (211, 63, 73)
INK, GREY, LINE = (28, 33, 43), (112, 120, 135), (226, 229, 235)
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/"]


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        try:
            return ImageFont.truetype(d + name, size)
        except Exception:
            pass
    return ImageFont.load_default()


def money(x):
    sign = "-" if x < 0 else ""
    return f"{sign}₹{abs(crore(x)):,.0f} Cr"


def fit(draw, text, f, max_w):
    while draw.textlength(text, font=f) > max_w and len(text) > 4:
        text = text[:-2].rstrip() + "…"
    return text


def render_card_image(name, symbol, n, test=False):
    sales_g = pct_change(n["sales"], n["sales_ly"])
    profit_g = pct_change(n["profit"], n["profit_ly"])
    m = n["profit"] / n["sales"] * 100
    m_ly = n["profit_ly"] / n["sales_ly"] * 100
    diff = m - m_ly
    tag, _ = rate(sales_g, profit_g, diff, n["profit"], n["profit_ly"])
    color = TAG_COLORS[tag]
    q = n["quarter"].strftime("%b %Y")

    W, H = 1000, 700
    img = Image.new("RGB", (W, H), (246, 247, 249))
    d = ImageDraw.Draw(img)

    # header band
    d.rectangle([0, 0, W, 165], fill=color)
    d.text((40, 30), fit(d, name, font(42, True), 600), font=font(42, True), fill="white")
    d.text((40, 95), f"{symbol}   |   Quarter ended {q}", font=font(24), fill=(255, 255, 255))
    # rating badge
    bf = font(34, True)
    bw = d.textlength(tag.upper(), font=bf) + 60
    d.rounded_rectangle([W - 40 - bw, 45, W - 40, 115], radius=35, fill="white")
    d.text((W - 40 - bw / 2, 80), tag.upper(), font=bf, fill=color, anchor="mm")

    def change(x_right, y, g, suffix):
        """colored triangle + number, right-aligned at x_right"""
        if g is None:
            d.text((x_right, y), "n/a", font=font(34, True), fill=GREY, anchor="ra")
            return
        c = GREEN if g >= 0 else RED
        txt = f"{abs(g):.1f}{suffix}"
        f = font(36, True)
        tw = d.textlength(txt, font=f)
        d.text((x_right, y), txt, font=f, fill=c, anchor="ra")
        tx = x_right - tw - 34
        if g >= 0:
            d.polygon([(tx, y + 38), (tx + 26, y + 38), (tx + 13, y + 10)], fill=c)
        else:
            d.polygon([(tx, y + 10), (tx + 26, y + 10), (tx + 13, y + 38)], fill=c)

    rows = [
        ("SALES", money(n["sales"]), "Last year  " + money(n["sales_ly"]), sales_g, "%"),
        ("NET PROFIT", money(n["profit"]), "Last year  " + money(n["profit_ly"]), profit_g, "%"),
        ("PROFIT MARGIN", f"{m:.1f}%", f"Last year  {m_ly:.1f}%", diff, " pts"),
    ]
    y = 200
    for label, val, ly, g, suf in rows:
        d.text((40, y), label, font=font(22, True), fill=GREY)
        d.text((40, y + 34), val, font=font(46, True), fill=INK)
        d.text((40, y + 96), ly, font=font(24), fill=GREY)
        change(W - 40, y + 50, g, suf)
        d.line([40, y + 140, W - 40, y + 140], fill=LINE, width=2)
        y += 155

    # footer
    d.text((40, 668), f"Market cap  {money(n['mcap'])}", font=font(24, True), fill=INK, anchor="lm")
    d.text((W - 40, 668), "Automatic summary, not investment advice", font=font(18),
           fill=GREY, anchor="rm")
    if test:
        d.text((W - 40, 176), "TEST - NOT REAL DATA", font=font(20, True),
               fill=RED, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), tag


def send_photo(png, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    r = requests.post(url, data={"chat_id": CHANNEL, "caption": caption},
                      files={"photo": ("card.png", png, "image/png")}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text}")


def send_card(name, symbol, n, test=False):
    """Send the result as an image. If drawing fails, fall back to text."""
    try:
        png, tag = render_card_image(name, symbol, n, test)
        send_photo(png, f"{name} ({symbol}): {tag}")
    except Exception as e:
        print(f"Image failed ({e}), sending text instead")
        prefix = "🧪 <b>TEST CARD (not real data)</b>\n\n" if test else ""
        send_telegram(prefix + build_card(name, symbol, n))


# -------------------------------- MAIN --------------------------------
def main():
    if not BOT_TOKEN or not CHANNEL:
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL secret.")

    # TEST MODE: posts 4 made-up cards so you can check the channel works.
    # Does not touch NSE and does not change the bot's memory.
    if os.environ.get("TEST_MODE") == "1":
        samples = [
            ("Sample Excellent Ltd", "SAMPLE1", 1200e7, 1000e7, 200e7, 140e7),
            ("Sample Good Ltd",      "SAMPLE2", 1060e7, 1000e7, 105e7, 100e7),
            ("Sample Average Ltd",   "SAMPLE3", 1000e7, 1000e7, 100e7, 100e7),
            ("Sample Poor Ltd",      "SAMPLE4",  900e7, 1000e7,  60e7, 100e7),
        ]
        for name, sym, s, sly, p, ply in samples:
            n = dict(sales=s, sales_ly=sly, profit=p, profit_ly=ply,
                     mcap=8000e7, quarter=datetime(2026, 9, 30))
            send_card(name, sym, n, test=True)
            time.sleep(3)
        print("Test cards sent.")
        return

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
            send_card(p["name"], p["symbol"], n)
            print(f"{p['symbol']}: posted")
            done.add(key); del pending[key]
            time.sleep(3)
        except Exception as e:
            print(f"{p['symbol']}: Telegram failed: {e}")

    state.update(done=sorted(done), pending=pending)
    save_state(state)


if __name__ == "__main__":
    main()
