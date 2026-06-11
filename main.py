#!/usr/bin/env python3
"""
TradingAlertRF — Roberto's personal trading assistant

Schedule (Europe/Stockholm):
  07:00 — Morning briefing
  16:00 — NY Open summary  
  17:30 — EU Close summary (EU stocks focus)
  22:00 — US Close summary (US stocks focus)

Real-time: WebSocket price alerts + hourly news filter
Commands: /summary /us /morning
"""

import os, json, time, threading, requests, schedule, anthropic, websocket
from collections import deque, defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
import pytz

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID          = os.environ.get("CHAT_ID")
FINNHUB_KEY      = os.environ.get("FINNHUB_KEY")
CLAUDE_KEY       = os.environ.get("CLAUDE_KEY")
TWELVEDATA_KEY   = os.environ.get("TWELVEDATA_KEY")

STOCKHOLM_TZ     = pytz.timezone("Europe/Stockholm")
DROP_THRESHOLD   = 2.0
DAY_THRESHOLD    = 2.5
QUANTUM_STOCKS   = {"IONQ", "QBTS", "RGTI"}
NEWS_IDS_FILE    = "/app/sent_news_ids.txt"

# EU stocks: Twelve Data format with exchange
# US stocks: plain ticker
PORTFOLIO_DATA = {
    "ASML":     ("ASML",              "Semiconduttori"),
    "MSFT":     ("Microsoft",         "Tech"),
    "GOOGL":    ("Alphabet",          "Tech"),
    "RMS.PA":   ("Hermès",            "Lusso & Fashion"),
    "AIR.PA":   ("Airbus",            "Industriale"),
    "MC.PA":    ("LVMH",              "Lusso & Fashion"),
    "UCG.MI":   ("UniCredit",         "Finance"),
    "EL.PA":    ("EssilorLuxottica",  "Lusso & Fashion"),
    "CDI.PA":   ("Christian Dior",    "Lusso & Fashion"),
    "ADYEN.AS": ("Adyen",             "Tech"),
    "DSY.PA":   ("Dassault Systèmes", "Tech"),
    "SAP":      ("SAP",               "Tech"),
    "ENGI.PA":  ("Engie",             "Energia"),
    "DTE.DE":   ("Deutsche Telekom",  "Telecom"),
    "ISP.MI":   ("Intesa Sanpaolo",   "Finance"),
    "SONY":     ("Sony",              "Consumer Tech"),
    "TSM":      ("Taiwan Semi",       "Semiconduttori"),
    "RACE":     ("Ferrari",           "Consumer"),
    "ONON":     ("ON Holding",        "Consumer"),
    "SU.PA":    ("Schneider Electric","Industriale"),
    "SPOT":     ("Spotify",           "Tech"),
    "IONQ":     ("IonQ",              "Quantum"),
    "QBTS":     ("D-Wave",            "Quantum"),
    "RGTI":     ("Rigetti",           "Quantum"),
    "ENR.DE":   ("Siemens Energy",    "Energia"),
    "ASM.AS":   ("ASM International", "Semiconduttori"),
    "AMAT":     ("Applied Materials", "Semiconduttori"),
    "AVGO":     ("Broadcom",          "Semiconduttori"),
}
PORTFOLIO = {k: v[0] for k, v in PORTFOLIO_DATA.items()}

# WebSocket uses plain tickers (Finnhub US real-time)
WS_SYMBOLS = [s for s in PORTFOLIO if "." not in s]

# Yahoo Finance and Finnhub use same .XX format — no mapping needed

price_history:   dict = {s: deque(maxlen=500) for s in PORTFOLIO}
last_alert_time: dict = {}
ALERT_COOLDOWN        = 30 * 60
last_update_id:  int  = 0

# ─── NEWS IDS PERSISTENCE ─────────────────────────────────────────────────────
def load_sent_news_ids() -> set:
    try:
        p = Path(NEWS_IDS_FILE)
        if p.exists():
            return set(p.read_text().strip().splitlines())
    except:
        pass
    return set()

def save_news_id(nid: str):
    try:
        with open(NEWS_IDS_FILE, "a") as f:
            f.write(nid + "\n")
    except:
        pass

sent_news_ids: set = load_sent_news_ids()

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=15
        )
    except Exception as e:
        print(f"Telegram error: {e}")

def poll_telegram_commands():
    global last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            f"?offset={last_update_id + 1}&timeout=3",
            timeout=8
        ).json()
        for update in r.get("result", []):
            last_update_id = update["update_id"]
            text = update.get("message", {}).get("text", "").strip().lower()
            if text in ("/summary", "/eu"):
                threading.Thread(target=eu_close_summary, daemon=True).start()
            elif text == "/us":
                threading.Thread(target=us_close_summary, daemon=True).start()
            elif text == "/morning":
                threading.Thread(target=morning_briefing, daemon=True).start()
            elif text == "/ny":
                threading.Thread(target=ny_summary, daemon=True).start()
    except Exception as e:
        print(f"Command poll error: {e}")

# ─── TWELVE DATA ──────────────────────────────────────────────────────────────
# ─── YFINANCE (all prices, correct native currency) ──────────────────────────
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone

def _fetch_yf_one(symbol: str) -> tuple:
    """
    Fetch one symbol using yfinance.
    - Intraday OHLC: period=1d interval=1m (only today, no timezone confusion)
    - Prev close: period=5d interval=1d (take second-to-last daily bar)
    Always returns native currency (EUR for EU, USD for US).
    """
    try:
        ticker = yf.Ticker(symbol)

        # Today's intraday bars only
        intraday = ticker.history(period="1d", interval="1m", auto_adjust=False)
        if intraday.empty:
            print(f"YF empty intraday for {symbol}")
            return (symbol, {})

        c = round(float(intraday["Close"].iloc[-1]), 4)
        h = round(float(intraday["High"].max()), 4)
        l = round(float(intraday["Low"].min()), 4)

        # Previous close from daily bars
        daily = ticker.history(period="5d", interval="1d", auto_adjust=False)
        if len(daily) >= 2:
            pc = round(float(daily["Close"].iloc[-2]), 4)
        else:
            pc = c

        print(f"YF OK {symbol}: c={c} h={h} l={l} pc={pc}")
        return (symbol, {"c": c, "pc": pc, "h": h, "l": l})
    except Exception as e:
        print(f"YF error {symbol}: {e}")
        return (symbol, {})

def get_quotes_yf(symbols: list) -> dict:
    """Parallel fetch via yfinance for all symbols."""
    result = {s: {} for s in symbols}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for sym, data in ex.map(_fetch_yf_one, symbols):
            result[sym] = data
    return result

# Aliases
def get_quotes_yahoo(symbols):      return get_quotes_yf(symbols)
def get_quotes_twelvedata(symbols): return get_quotes_yf(symbols)
def get_quotes_stooq(symbols):      return get_quotes_yf(symbols)

# ─── FINNHUB (news + earnings + WebSocket) ────────────────────────────────────
def get_market_news() -> list:
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}",
            timeout=10
        )
        return r.json()[:10]
    except:
        return []

def get_company_news(symbol: str) -> list:
    # Yahoo Finance uses same .XX format as Finnhub — no mapping needed
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news"
            f"?symbol={symbol}&from={yesterday}&to={today}&token={FINNHUB_KEY}",
            timeout=10
        )
        return r.json()[:5]
    except:
        return []

def get_earnings_calendar() -> list:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/calendar/earnings"
            f"?from={tomorrow}&to={tomorrow}&token={FINNHUB_KEY}",
            timeout=10
        )
        return r.json().get("earningsCalendar", [])
    except:
        return []

# ─── CLAUDE ───────────────────────────────────────────────────────────────────
def claude_analyze(prompt: str, max_tokens: int = 1000) -> str:
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text
    except Exception as e:
        return f"Errore analisi: {e}"

# ─── PORTFOLIO CONTEXT BUILDER ────────────────────────────────────────────────
def build_portfolio_context(quotes: dict):
    sector_data  = defaultdict(list)
    big_movers   = []
    new_extremes = []

    for symbol, (name, sector) in PORTFOLIO_DATA.items():
        q  = quotes.get(symbol, {})
        c  = q.get("c", 0)
        pc = q.get("pc", 0)
        h  = q.get("h", 0)
        l  = q.get("l", 0)

        if c and pc:
            pct  = (c - pc) / pc * 100
            sign = "+" if pct >= 0 else ""
            line = f"{name}: {c:.2f} ({sign}{pct:.1f}%)"
            sector_data[sector].append((name, pct, line))

            if abs(pct) >= DAY_THRESHOLD:
                tag = "SU" if pct > 0 else "GIU"
                big_movers.append(f"<b>{name}</b> {sign}{pct:.1f}% ({tag})")

            # max/min removed — too unreliable
        else:
            sector_data[sector].append((name, None, f"{name}: N/A"))

    # Sector lines with trend
    sector_lines = []
    sector_avgs  = {}
    SECTOR_ORDER = [
        "Lusso & Fashion", "Tech", "Semiconduttori", "Quantum",
        "Finance", "Energia", "Industriale", "Consumer Tech",
        "Consumer", "Telecom"
    ]
    for sector in SECTOR_ORDER:
        stocks = sector_data.get(sector, [])
        if not stocks:
            continue
        valid = [(n, p, li) for n, p, li in stocks if p is not None]
        if valid:
            avg   = sum(p for _, p, _ in valid) / len(valid)
            sector_avgs[sector] = avg
            trend = "▲" if avg > 0.3 else "▼" if avg < -0.3 else "—"
            dets  = "  |  ".join(li for _, _, li in stocks)
            sector_lines.append(f"{trend} <b>{sector}</b>  avg {avg:+.1f}%\n   {dets}")
        else:
            dets = "  |  ".join(li for _, _, li in stocks)
            sector_lines.append(f"— <b>{sector}</b>\n   {dets}")

    portfolio_str  = "\n".join(sector_lines)
    big_movers_str = "\n".join(big_movers) or "Nessun movimento > 2.5%"
    extremes_str   = "\n".join(new_extremes) or "—"

    return portfolio_str, big_movers_str

# ─── VELOCITY PRICE ALERTS ────────────────────────────────────────────────────
def process_price(symbol: str, price: float):
    if symbol not in PORTFOLIO:
        return
    now = time.time()
    price_history[symbol].append((now, price))
    if now - last_alert_time.get(symbol, 0) < ALERT_COOLDOWN:
        return

    name       = PORTFOLIO[symbol]
    is_quantum = symbol in QUANTUM_STOCKS
    levels = [
        (5*60,  "EMERGENCY",     "🚨", "ROCKET",      "🚀", True),
        (10*60, "ALLERTA ROSSA", "🔴", "VERDE FORTE", "💚", not is_quantum),
        (30*60, "ALLERTA GIALLA","🟡", "VERDE",       "🟢", not is_quantum),
    ]

    for window, dl, de, rl, re, active in levels:
        if not active:
            continue
        cutoff  = now - window
        wp      = [(t, p) for t, p in price_history[symbol] if t >= cutoff]
        if len(wp) < 2:
            continue
        ref  = wp[0][1]
        pct  = (price - ref) / ref * 100
        mins = max(1, int((now - wp[0][0]) / 60))

        if pct <= -DROP_THRESHOLD:
            send_telegram(f"{de} <b>{dl} — {name}</b>\n\n📉 {pct:+.1f}% in {mins} min\nDa {ref:.2f} → {price:.2f}")
            last_alert_time[symbol] = now
            return
        if pct >= DROP_THRESHOLD:
            send_telegram(f"{re} <b>{rl} — {name}</b>\n\n📈 +{pct:.1f}% in {mins} min\nDa {ref:.2f} → {price:.2f}")
            last_alert_time[symbol] = now
            return

# ─── WEBSOCKET (Finnhub, US real-time) ────────────────────────────────────────
def on_ws_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("type") == "trade":
            for t in data.get("data", []):
                s, p = t.get("s"), t.get("p")
                if s and p:
                    process_price(s, p)
    except Exception as e:
        print(f"WS msg error: {e}")

def on_ws_open(ws):
    for s in WS_SYMBOLS:
        ws.send(json.dumps({"type": "subscribe", "symbol": s}))
    print(f"WS subscribed: {WS_SYMBOLS}")

def on_ws_close(ws, *args):
    print("WS closed, reconnecting in 15s...")
    time.sleep(15)
    start_ws()

def start_ws():
    ws = websocket.WebSocketApp(
        f"wss://ws.finnhub.io?token={FINNHUB_KEY}",
        on_open=on_ws_open, on_message=on_ws_message,
        on_error=lambda ws, e: print(f"WS err: {e}"),
        on_close=on_ws_close,
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ─── EU STOCK POLLING (Twelve Data, every 5 min) ──────────────────────────────
def poll_eu_stocks():
    """Real-time EU price polling using fast_info.last_price — near real-time."""
    eu = [s for s in PORTFOLIO if "." in s]
    for symbol in eu:
        try:
            price = yf.Ticker(symbol).fast_info.last_price
            if price and price > 0:
                process_price(symbol, float(price))
                print(f"Poll {symbol}: {price}")
        except Exception as e:
            print(f"Poll error {symbol}: {e}")
        time.sleep(0.1)

# ─── NEWS ALERTS ──────────────────────────────────────────────────────────────
def is_relevant_news(headline: str, summary: str) -> bool:
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=5,
            messages=[{"role": "user", "content": (
                "Rate this financial news 1-10 for investor relevance.\n"
                "8-10: CEO/CFO change, acquisition, bankruptcy, fraud, regulatory ban.\n"
                "6-7: earnings results, analyst upgrade/downgrade/reiteration, guidance, buyback, major contract.\n"
                "1-5: sector overview, generic commentary, minor update.\n"
                f"Headline: {headline}\nSummary: {summary[:200]}\n"
                "Reply with ONE integer only."
            )}]
        )
        score = int(msg.content[0].text.strip())
        print(f"News {score}/10: {headline[:50]}")
        return score >= 6
    except Exception as e:
        print(f"News filter error: {e}")
        return False

def check_news_alerts():
    for symbol in PORTFOLIO:
        for news in get_company_news(symbol):
            nid      = str(news.get("id") or news.get("datetime") or "")
            headline = news.get("headline", "")
            summary  = news.get("summary") or ""
            url      = news.get("url", "")
            if not nid or nid in sent_news_ids:
                continue
            sent_news_ids.add(nid)
            save_news_id(nid)
            if is_relevant_news(headline, summary):
                name = PORTFOLIO[symbol]
                send_telegram(f"📰 <b>NEWS — {name}</b>\n\n{headline}\n\n🔗 {url}")
                print(f"News sent: {name} — {headline}")
            time.sleep(0.3)

# ─── MORNING BRIEFING 07:00 ───────────────────────────────────────────────────
def morning_briefing():
    print("Generating morning briefing...")
    try:
        quotes = get_quotes_twelvedata(list(PORTFOLIO_DATA.keys()))
        lines  = []
        for symbol, (name, _) in PORTFOLIO_DATA.items():
            q = quotes.get(symbol, {})
            c, pc = q.get("c", 0), q.get("pc", 0)
            if c and pc:
                pct  = (c - pc) / pc * 100
                sign = "+" if pct >= 0 else ""
                lines.append(f"{name}: {c:.2f} ({sign}{pct:.1f}%)")
            else:
                lines.append(f"{name}: N/A")

        portfolio_str = "\n".join(lines)
        news_str      = "\n".join(f"- {n['headline']}" for n in get_market_news()[:5])
        today_str     = datetime.now(STOCKHOLM_TZ).strftime("%A %d %B %Y")

        prompt = f"""Assistente trading di Roberto. Oggi {today_str}. Briefing pre-apertura EU.

PORTAFOGLIO (variazione da chiusura precedente):
{portfolio_str}

NEWS:
{news_str}

Max 300 parole. Professionale, asciutto. Struttura:

<b>Asia Overnight</b> — cosa è successo stanotte, indici chiave
<b>Apertura EU</b> — cosa aspettarsi oggi, sentiment
<b>Portafoglio</b> — solo titoli con movimenti o news rilevanti
<b>Tema</b> — 1 tema o settore caldo oggi
<b>Rischio</b> — 1 cosa concreta da monitorare

In italiano. Solo fatti e numeri."""

        send_telegram(f"<b>BRIEFING — {today_str}</b>\n\n{claude_analyze(prompt)}")
        print("Morning briefing sent.")
    except Exception as e:
        print(f"Morning briefing error: {e}")
        send_telegram(f"Errore briefing mattutino: {e}")

# ─── NY OPEN 16:00 ────────────────────────────────────────────────────────────
def ny_summary():
    print("Generating NY summary...")
    try:
        # Focus: US stocks only
        us_symbols = [s for s in PORTFOLIO_DATA if ":" not in s]
        quotes     = get_quotes_twelvedata(us_symbols)

        lines = []
        for sym in us_symbols:
            name = PORTFOLIO_DATA[sym][0]
            q    = quotes.get(sym, {})
            c, pc = q.get("c", 0), q.get("pc", 0)
            if c and pc:
                pct  = (c - pc) / pc * 100
                sign = "+" if pct >= 0 else ""
                lines.append(f"{name}: {c:.2f} ({sign}{pct:.1f}%)")
            else:
                lines.append(f"{name}: N/A")

        us_str   = "\n".join(lines)
        news_str = "\n".join(f"- {n['headline']}" for n in get_market_news()[:4])

        prompt = f"""NY apre da 30 minuti. Briefing rapido per Roberto, focus mercato americano.

TITOLI US:
{us_str}

NEWS:
{news_str}

Max 180 parole. Professionale e conciso.

<b>NY Open</b> — direzione S&P500, Nasdaq, Dow con dati
<b>I tuoi US</b> — chi si muove di più e perché (solo i rilevanti)
<b>Watch</b> — 1 cosa da seguire nel pomeriggio americano

In italiano. Solo fatti."""

        send_telegram(f"<b>NY OPEN — 16:00</b>\n\n{claude_analyze(prompt)}")
        print("NY summary sent.")
    except Exception as e:
        print(f"NY summary error: {e}")
        send_telegram(f"Errore NY summary: {e}")

# ─── EU CLOSE 17:30 ───────────────────────────────────────────────────────────
def eu_close_summary():
    print("Generating EU close summary...")
    try:
        # All stocks, EU focus
        quotes = get_quotes_twelvedata(list(PORTFOLIO_DATA.keys()))
        portfolio_str, big_movers_str = build_portfolio_context(quotes)

        earnings       = get_earnings_calendar()
        my_tickers_fh  = set(PORTFOLIO.keys())
        my_earn        = [e for e in earnings if e.get("symbol") in my_tickers_fh]
        other_earn     = [e for e in earnings[:10] if e.get("symbol") not in my_tickers_fh]
        my_earn_str    = "\n".join(f"⚠️ {e['symbol']} — EPS est: {e.get('epsEstimate','N/A')}" for e in my_earn) or "Nessuno"
        other_earn_str = "\n".join(f"• {e['symbol']}" for e in other_earn[:5]) or "—"
        news_str       = "\n".join(f"- {n['headline']}" for n in get_market_news()[:5])

        prompt = f"""Sei l'assistente trading di Roberto. Sono le 17:30, mercati EU appena chiusi.

PORTAFOGLIO PER SETTORE:
{portfolio_str}

MOVERS >±2.5%:
{big_movers_str}

NEWS:
{news_str}

EARNINGS DOMANI — TUOI: {my_earn_str}
EARNINGS DOMANI — ALTRI: {other_earn_str}

Max 420 parole. Professionale, da analista senior. <b>Grassetto</b> per titoli >2.5% e nomi sezione.
STRUTTURA FISSA:

<b>Chiusura EU</b> — CAC40, DAX, FTSE MIB: sentiment e dati

<b>Trend Settoriali</b> — in ordine, con trend e media %:
Lusso e Fashion | Tech | Semiconduttori | Quantum | Finance | Energia | Industriale | Telecom

<b>Movers del Giorno</b> — titoli >2.5% in grassetto con causa

<b>Earnings Domani</b> — tuoi titoli (valutare uscita?), poi altri importanti

<b>News Rilevanti</b> — 2-3 notizie che hanno mosso i mercati oggi

<b>Stock da Guardare</b> — 1 titolo fuori portafoglio con motivazione

<b>Domani</b> — 1 cosa concreta da monitorare

In italiano. Solo fatti e numeri."""

        send_telegram(f"<b>CHIUSURA EU — 17:30</b>\n\n{claude_analyze(prompt)}")
        print("EU close sent.")
    except Exception as e:
        print(f"EU close error: {e}")
        send_telegram(f"Errore EU close: {e}")

# ─── US CLOSE 22:00 ───────────────────────────────────────────────────────────
def us_close_summary():
    print("Generating US close summary...")
    try:
        # All stocks, US focus
        quotes = get_quotes_twelvedata(list(PORTFOLIO_DATA.keys()))
        portfolio_str, big_movers_str = build_portfolio_context(quotes)
        news_str = "\n".join(f"- {n['headline']}" for n in get_market_news()[:5])

        prompt = f"""Sei l'assistente trading di Roberto. Sono le 22:00, Wall Street ha chiuso.

PORTAFOGLIO PER SETTORE:
{portfolio_str}

MOVERS >±2.5%:
{big_movers_str}

NEWS:
{news_str}

Max 380 parole. Professionale. <b>Grassetto</b> per titoli >2.5% e nomi sezione.
STRUTTURA FISSA:

<b>Chiusura US</b> — S&P500, Nasdaq, Dow: sentiment e dati

<b>Trend Settoriali</b> — focus US, in ordine:
Tech | Semiconduttori | Quantum | Consumer Tech | Consumer | Finance

<b>Movers del Giorno</b> — titoli >2.5% in grassetto con causa

<b>News Rilevanti</b> — 2-3 notizie che hanno mosso Wall Street oggi

<b>Stock da Guardare</b> — 1 titolo fuori portafoglio con motivazione

<b>Takeaway</b> — 1-2 osservazioni chiave sulla sessione

<b>Domani EU</b> — cosa aspettarsi all'apertura europea

In italiano. Solo fatti e numeri."""

        send_telegram(f"<b>CHIUSURA US — 22:00</b>\n\n{claude_analyze(prompt)}")
        print("US close sent.")
    except Exception as e:
        print(f"US close error: {e}")
        send_telegram(f"Errore US close: {e}")

# ─── DAILY RESET ──────────────────────────────────────────────────────────────
def daily_reset():
    last_alert_time.clear()
    for s in price_history:
        price_history[s].clear()
    # Keep sent_news_ids in memory (already persisted on disk)
    print("Daily reset done.")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Starting TradingAlertRF bot...")

    start_ws()

    schedule.every().day.at("07:00").do(morning_briefing)
    schedule.every().day.at("16:00").do(ny_summary)
    schedule.every().day.at("17:30").do(eu_close_summary)
    schedule.every().day.at("22:00").do(us_close_summary)
    schedule.every(5).minutes.do(poll_eu_stocks)
    schedule.every(1).hours.do(check_news_alerts)
    schedule.every(1).minutes.do(poll_telegram_commands)
    schedule.every().day.at("00:01").do(daily_reset)

    send_telegram(
        "<b>TradingAlertRF — online</b>\n\n"
        "07:00  Briefing mattutino\n"
        "16:00  NY Open\n"
        "17:30  Chiusura EU\n"
        "22:00  Chiusura US\n\n"
        "Alert: real-time ±2% (velocità)\n"
        "Comandi: /morning /ny /summary /us"
    )

    while True:
        schedule.run_pending()
        time.sleep(10)
