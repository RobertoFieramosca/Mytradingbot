#!/usr/bin/env python3
"""
TradingAlertRF — Roberto's personal trading assistant

Schedule (Europe/Stockholm):
  07:00 — Morning briefing
  16:00 — NY Open summary
  17:30 — EU Close summary
  22:00 — US Close summary
  + Real-time price velocity alerts
  + Hourly relevant news filter
  + /summary command via Telegram
"""

import os, json, time, threading, requests, schedule, anthropic, websocket
import yfinance as yf
from collections import deque, defaultdict
from datetime import datetime, date, timedelta
import pytz

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID         = os.environ.get("CHAT_ID")
FINNHUB_KEY     = os.environ.get("FINNHUB_KEY")
CLAUDE_KEY      = os.environ.get("CLAUDE_KEY")

STOCKHOLM_TZ    = pytz.timezone("Europe/Stockholm")
DROP_THRESHOLD  = 2.0
DAY_THRESHOLD   = 2.5
QUANTUM_STOCKS  = {"IONQ", "QBTS", "RGTI"}

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
    "ASMIY":    ("ASM International", "Semiconduttori"),
    "AMAT":     ("Applied Materials", "Semiconduttori"),
    "AVGO":     ("Broadcom",          "Semiconduttori"),
}
PORTFOLIO = {k: v[0] for k, v in PORTFOLIO_DATA.items()}

price_history:   dict = {s: deque(maxlen=500) for s in PORTFOLIO}
last_alert_time: dict = {}
ALERT_COOLDOWN        = 30 * 60
sent_news_ids:   set  = set()
last_update_id:  int  = 0

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print(f"Telegram error: {e}")

def poll_telegram_commands():
    """Check for /summary command from user."""
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=5"
        r = requests.get(url, timeout=10).json()
        for update in r.get("result", []):
            last_update_id = update["update_id"]
            text = update.get("message", {}).get("text", "")
            if text.strip().lower() in ("/summary", "/eu", "/chiusura"):
                send_telegram("Generando summary...")
                threading.Thread(target=eu_close_summary, daemon=True).start()
            elif text.strip().lower() == "/us":
                send_telegram("Generando US summary...")
                threading.Thread(target=us_close_summary, daemon=True).start()
            elif text.strip().lower() == "/morning":
                send_telegram("Generando briefing...")
                threading.Thread(target=morning_briefing, daemon=True).start()
    except Exception as e:
        print(f"Command poll error: {e}")

# ─── MARKET DATA ──────────────────────────────────────────────────────────────
def get_quote_batch(symbols: list) -> dict:
    result = {}
    try:
        tickers = yf.Tickers(" ".join(symbols))
        for symbol in symbols:
            try:
                fi = tickers.tickers[symbol].fast_info
                result[symbol] = {
                    "c":  round(float(fi.last_price), 4)     if fi.last_price     else 0,
                    "pc": round(float(fi.previous_close), 4) if fi.previous_close else 0,
                    "h":  round(float(fi.day_high), 4)        if fi.day_high       else 0,
                    "l":  round(float(fi.day_low), 4)         if fi.day_low        else 0,
                }
            except:
                result[symbol] = {}
    except Exception as e:
        print(f"Batch quote error: {e}")
    return result

def get_market_news() -> list:
    try:
        r = requests.get(f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}", timeout=10)
        return r.json()[:10]
    except:
        return []

def get_company_news(symbol: str) -> list:
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={yesterday}&to={today}&token={FINNHUB_KEY}",
            timeout=10)
        return r.json()[:5]
    except:
        return []

def get_earnings_calendar() -> list:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/calendar/earnings?from={tomorrow}&to={tomorrow}&token={FINNHUB_KEY}",
            timeout=10)
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

# ─── PORTFOLIO DATA BUILDER ───────────────────────────────────────────────────
def build_portfolio_context():
    """Fetch all quotes and build sector data, movers, extremes."""
    quotes      = get_quote_batch(list(PORTFOLIO_DATA.keys()))
    sector_data = defaultdict(list)
    big_movers  = []
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

            # Flag if current price is within 0.5% of today's high or low
            if h and abs(c - h) / h < 0.005:
                new_extremes.append(f"{name}: massimo di giornata {h:.2f}")
            elif l and abs(c - l) / l < 0.005:
                new_extremes.append(f"{name}: minimo di giornata {l:.2f}")
        else:
            sector_data[sector].append((name, None, f"{name}: N/A"))

    # Build sector lines
    sector_lines = []
    sector_avgs  = {}
    for sector, stocks in sector_data.items():
        valid = [(n, p, li) for n, p, li in stocks if p is not None]
        if valid:
            avg = sum(p for _, p, _ in valid) / len(valid)
            sector_avgs[sector] = avg
            trend = "▲" if avg > 0.3 else "▼" if avg < -0.3 else "—"
            details = ", ".join(li for _, _, li in stocks)
            sector_lines.append(f"{trend} {sector}: {details}")
        else:
            sector_lines.append(f"— {sector}: " + ", ".join(li for _, _, li in stocks))

    portfolio_str  = "\n".join(sector_lines)
    big_movers_str = "\n".join(big_movers) or "Nessun movimento > 2.5%"
    extremes_str   = "\n".join(new_extremes) or "Nessuno"
    sector_avg_str = "\n".join(
        f"  {s}: {a:+.2f}%"
        for s, a in sorted(sector_avgs.items(), key=lambda x: x[1], reverse=True)
    )

    return portfolio_str, big_movers_str, extremes_str, sector_avg_str

# ─── PRICE VELOCITY ALERTS ────────────────────────────────────────────────────
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
        (30*60, "ALLERTA GIALLA","🟡", "VERDE",        "🟢", not is_quantum),
    ]

    history = price_history[symbol]
    for window, dl, de, rl, re, active in levels:
        if not active:
            continue
        cutoff  = now - window
        wprices = [(t, p) for t, p in history if t >= cutoff]
        if len(wprices) < 2:
            continue
        ref  = wprices[0][1]
        pct  = (price - ref) / ref * 100
        mins = max(1, int((now - wprices[0][0]) / 60))

        if pct <= -DROP_THRESHOLD:
            send_telegram(f"{de} <b>{dl} — {name}</b>\n\n📉 {pct:+.1f}% in {mins} minuti\nDa {ref:.2f} → {price:.2f}")
            last_alert_time[symbol] = now
            return
        if pct >= DROP_THRESHOLD:
            send_telegram(f"{re} <b>{rl} — {name}</b>\n\n📈 +{pct:.1f}% in {mins} minuti\nDa {ref:.2f} → {price:.2f}")
            last_alert_time[symbol] = now
            return

# ─── WEBSOCKET ────────────────────────────────────────────────────────────────
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
    us = [s for s in PORTFOLIO if "." not in s]
    for s in us:
        ws.send(json.dumps({"type": "subscribe", "symbol": s}))
    print(f"WS subscribed: {us}")

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

# ─── EU STOCK POLLING ─────────────────────────────────────────────────────────
def poll_eu_stocks():
    eu = [s for s in PORTFOLIO if "." in s]
    quotes = get_quote_batch(eu)
    for s in eu:
        c = quotes.get(s, {}).get("c", 0)
        if c:
            process_price(s, c)

# ─── NEWS ALERTS ──────────────────────────────────────────────────────────────
def is_relevant_news(headline: str, summary: str) -> bool:
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=5,
            messages=[{"role": "user", "content": (
                "Rate this financial news 1-10 for investor relevance.\n"
                "8-10: CEO/CFO change, acquisition, bankruptcy, fraud, regulatory ban.\n"
                "6-7: earnings results, analyst upgrade/downgrade/reiteration, guidance, buyback, major contract.\n"
                "1-5: sector overview, generic commentary, minor product update.\n"
                f"Headline: {headline}\nSummary: {summary[:200]}\n"
                "Reply with ONE integer only."
            )}]
        )
        score = int(msg.content[0].text.strip())
        print(f"News score {score}/10: {headline[:50]}")
        return score >= 6
    except Exception as e:
        print(f"News filter error: {e}")
        return False

def check_news_alerts():
    for symbol, name in PORTFOLIO.items():
        for news in get_company_news(symbol):
            nid      = str(news.get("id") or news.get("datetime") or "")
            headline = news.get("headline", "")
            summary  = news.get("summary") or ""
            url      = news.get("url", "")
            if not nid or nid in sent_news_ids:
                continue
            sent_news_ids.add(nid)
            if is_relevant_news(headline, summary):
                send_telegram(f"📰 <b>NEWS — {name}</b>\n\n{headline}\n\n🔗 {url}")
                print(f"News sent: {name} — {headline}")
            time.sleep(0.5)

# ─── MORNING BRIEFING 07:00 ───────────────────────────────────────────────────
def morning_briefing():
    print("Generating morning briefing...")
    try:
        quotes = get_quote_batch(list(PORTFOLIO.keys()))
        lines  = []
        for symbol, name in PORTFOLIO.items():
            q = quotes.get(symbol, {})
            c, pc = q.get("c", 0), q.get("pc", 0)
            if c and pc:
                pct  = (c - pc) / pc * 100
                sign = "+" if pct >= 0 else ""
                lines.append(f"{name}: {c:.2f} ({sign}{pct:.1f}%)")
            else:
                lines.append(f"{name}: N/A")

        portfolio_str = "\n".join(lines)
        news_str      = "\n".join(f"- {n['headline']}" for n in get_market_news()[:6])
        today_str     = datetime.now(STOCKHOLM_TZ).strftime("%A %d %B %Y")

        prompt = f"""Sei l'assistente trading di Roberto. Oggi è {today_str}.
Briefing mattutino prima dell'apertura EU.

PORTAFOGLIO:
{portfolio_str}

NEWS RECENTI:
{news_str}

Messaggio Telegram max 300 parole. Tono professionale, da analista senior. Niente esclamazioni.
Struttura:
Chiusura Asia — cosa è successo stanotte
Apertura EU — cosa aspettarsi oggi
Portafoglio — solo movimenti o news rilevanti con numeri
Tema del Giorno — 1 tema caldo
Rischio — 1 cosa concreta da monitorare

In italiano. Solo i fatti."""

        send_telegram(f"<b>BRIEFING MATTUTINO</b>\n\n{claude_analyze(prompt)}")
        print("Morning briefing sent.")
    except Exception as e:
        print(f"Morning briefing error: {e}")

# ─── NY OPEN SUMMARY 16:00 ────────────────────────────────────────────────────
def ny_summary():
    print("Generating NY summary...")
    try:
        quotes = get_quote_batch(list(PORTFOLIO.keys()))
        lines  = []
        for symbol, name in PORTFOLIO.items():
            q = quotes.get(symbol, {})
            c, pc = q.get("c", 0), q.get("pc", 0)
            if c and pc:
                pct  = (c - pc) / pc * 100
                sign = "+" if pct >= 0 else ""
                lines.append(f"{name}: {c:.2f} ({sign}{pct:.1f}%)")
            else:
                lines.append(f"{name}: N/A")

        portfolio_str = "\n".join(lines)

        prompt = f"""NY ha aperto da 30 minuti. Summary rapido per Roberto.

PORTAFOGLIO:
{portfolio_str}

Messaggio max 200 parole. Professionale, asciutto.
NY Apertura — direzione mercato americano con dati
Portafoglio — movimenti rilevanti finora
Da Monitorare — cosa può muoversi nel pomeriggio NY

In italiano. Solo i fatti."""

        send_telegram(f"<b>NY OPEN</b>\n\n{claude_analyze(prompt)}")
        print("NY summary sent.")
    except Exception as e:
        print(f"NY summary error: {e}")

# ─── EU CLOSE SUMMARY 17:30 ───────────────────────────────────────────────────
def eu_close_summary():
    print("Generating EU close summary...")
    try:
        portfolio_str, big_movers_str, extremes_str, sector_avg_str = build_portfolio_context()

        earnings   = get_earnings_calendar()
        my_tickers = set(PORTFOLIO.keys())
        my_earn    = [e for e in earnings if e.get("symbol") in my_tickers]
        other_earn = [e for e in earnings[:10] if e.get("symbol") not in my_tickers]

        my_earn_str    = "\n".join(f"{e['symbol']} — EPS est: {e.get('epsEstimate','N/A')}" for e in my_earn) or "Nessuno"
        other_earn_str = "\n".join(f"• {e['symbol']}" for e in other_earn[:5]) or "N/A"
        news_str       = "\n".join(f"- {n['headline']}" for n in get_market_news()[:6])

        prompt = f"""Sei l'assistente trading di Roberto. Sono le 17:30, mercati EU chiusi.

PORTAFOGLIO PER SETTORE:
{portfolio_str}

MEDIE SETTORIALI:
{sector_avg_str}

MOVERS >±2.5%:
{big_movers_str}

MASSIMI/MINIMI DI GIORNATA:
{extremes_str}

NEWS:
{news_str}

EARNINGS DOMANI — TUOI:
{my_earn_str}

EARNINGS DOMANI — ALTRI:
{other_earn_str}

Scrivi UN messaggio Telegram, max 450 parole. Tono professionale da analista senior.
Usa <b>grassetto</b> per titoli con movimento >2.5% e per i titoli di sezione.
STRUTTURA FISSA in quest'ordine:

<b>Chiusura EU</b> — 2 righe sentiment generale con indici principali

<b>Trend Settoriali</b> — sempre presenti in quest'ordine con trend e media %:
  Lusso e Fashion | Tech | Semiconduttori | Quantum | Finance | Energia | Industriale

<b>Movers del Giorno</b> — titoli >2.5% in grassetto con causa

<b>Massimi/Minimi</b> — titoli vicini al massimo o minimo di giornata

<b>Earnings Domani</b> — tuoi titoli (valutare uscita?), poi altri importanti

<b>Stock da Guardare</b> — 1 titolo fuori portafoglio

<b>Domani</b> — 1 cosa concreta da monitorare

In italiano. Solo i fatti."""

        send_telegram(f"<b>CHIUSURA EU</b>\n\n{claude_analyze(prompt)}")
        print("EU close sent.")
    except Exception as e:
        print(f"EU close error: {e}")
        send_telegram(f"Errore EU close summary: {e}")

# ─── US CLOSE SUMMARY 22:00 ───────────────────────────────────────────────────
def us_close_summary():
    print("Generating US close summary...")
    try:
        portfolio_str, big_movers_str, extremes_str, sector_avg_str = build_portfolio_context()
        news_str = "\n".join(f"- {n['headline']}" for n in get_market_news()[:6])

        prompt = f"""Sono le 22:00 CET, Wall Street ha appena chiuso. Summary per Roberto.

PORTAFOGLIO PER SETTORE:
{portfolio_str}

MEDIE SETTORIALI:
{sector_avg_str}

MOVERS >±2.5%:
{big_movers_str}

MASSIMI/MINIMI:
{extremes_str}

NEWS:
{news_str}

Scrivi UN messaggio Telegram, max 400 parole. Tono professionale.
Usa <b>grassetto</b> per titoli con movimento >2.5%.
STRUTTURA FISSA:

<b>Chiusura US</b> — sentiment generale Wall Street, indici principali

<b>Trend Settoriali</b> — Lusso e Fashion | Tech | Semiconduttori | Quantum | Finance | Energia | Industriale

<b>Movers del Giorno</b> — titoli >2.5% in grassetto con causa

<b>Massimi/Minimi</b> — titoli vicini al max/min di giornata

<b>Takeaway</b> — 1-2 osservazioni chiave sulla sessione US

<b>Domani</b> — apertura EU, cosa monitorare

In italiano. Solo i fatti."""

        send_telegram(f"<b>CHIUSURA US</b>\n\n{claude_analyze(prompt)}")
        print("US close sent.")
    except Exception as e:
        print(f"US close error: {e}")
        send_telegram(f"Errore US close summary: {e}")

# ─── DAILY RESET ──────────────────────────────────────────────────────────────
def daily_reset():
    global sent_news_ids
    last_alert_time.clear()
    sent_news_ids = set()
    for s in price_history:
        price_history[s].clear()
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
        "<b>TradingAlertRF attivo</b>\n\n"
        "07:00 — Briefing mattutino\n"
        "16:00 — NY Open\n"
        "17:30 — Chiusura EU\n"
        "22:00 — Chiusura US\n"
        "Alert prezzi: real-time (±2%)\n"
        "Comandi: /summary /us /morning"
    )

    while True:
        schedule.run_pending()
        time.sleep(10)
