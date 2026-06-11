#!/usr/bin/env python3
"""
TradingAlertRF — Roberto's personal trading assistant

Schedule:
  07:00 — Morning briefing (Asia overnight, EU open, portfolio, theme of the day)
  16:00 — NY Summary (30 min after Wall Street open)
  17:30 — EU Close Summary (detailed end-of-day report)

Real-time alerts:
  🚨 EMERGENCY   — drop >= 2% in <= 5 min  (all stocks)
  🔴 ROSSA       — drop >= 2% in <= 10 min (non-quantum)
  🟡 GIALLA      — drop >= 2% in <= 30 min (non-quantum)
  📈 RIALZO      — rise >= 3% in 30 min    (all stocks)
  Quantum (IONQ, RGTI, QBTS): EMERGENCY only

  📰 Relevant company news: continuous
"""

import os
import json
import time
import threading
import requests
import schedule
import anthropic
import websocket
import yfinance as yf
from collections import deque
from datetime import datetime, date, timedelta
import pytz

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID         = os.environ.get("CHAT_ID")
FINNHUB_KEY     = os.environ.get("FINNHUB_KEY")
CLAUDE_KEY      = os.environ.get("CLAUDE_KEY")

STOCKHOLM_TZ    = pytz.timezone("Europe/Stockholm")
DROP_THRESHOLD  = 2.0   # % drop to trigger velocity alert
DAY_THRESHOLD   = 2.5   # % daily move to highlight in summary

QUANTUM_STOCKS  = {"IONQ", "QBTS", "RGTI"}

# Roberto's portfolio — Finnhub symbol: (display name, sector)
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

# Flat name lookup for compatibility
PORTFOLIO = {k: v[0] for k, v in PORTFOLIO_DATA.items()}

# Rolling price history for velocity alerts
price_history: dict  = {s: deque(maxlen=500) for s in PORTFOLIO}
last_alert_time: dict = {}
ALERT_COOLDOWN        = 30 * 60  # 30 min between alerts per symbol

sent_news_ids: set = set()

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ─── FINNHUB REST ──────────────────────────────────────────────────────────────
def get_quote(symbol: str) -> dict:
    """Get current quote using yfinance — works for all global markets."""
    try:
        fi = yf.Ticker(symbol).fast_info
        return {
            "c":  round(float(fi.last_price), 4)      if fi.last_price      else 0,
            "pc": round(float(fi.previous_close), 4)  if fi.previous_close  else 0,
            "h":  round(float(fi.day_high), 4)         if fi.day_high        else 0,
            "l":  round(float(fi.day_low), 4)          if fi.day_low         else 0,
        }
    except Exception as e:
        print(f"yfinance error for {symbol}: {e}")
        return {}

def get_quote_batch(symbols: list) -> dict:
    """Batch fetch quotes for multiple symbols — more efficient."""
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
    url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
    try:
        return requests.get(url, timeout=10).json()[:10]
    except:
        return []

def get_company_news(symbol: str) -> list:
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    url = (
        f"https://finnhub.io/api/v1/company-news"
        f"?symbol={symbol}&from={yesterday}&to={today}&token={FINNHUB_KEY}"
    )
    try:
        return requests.get(url, timeout=10).json()[:5]
    except:
        return []

def get_earnings_calendar() -> list:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    url = (
        f"https://finnhub.io/api/v1/calendar/earnings"
        f"?from={tomorrow}&to={tomorrow}&token={FINNHUB_KEY}"
    )
    try:
        data = requests.get(url, timeout=10).json()
        return data.get("earningsCalendar", [])
    except:
        return []

def get_52w_data(symbol: str) -> dict:
    """Get 52-week high/low via quote endpoint (h = high of day, l = low)."""
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}"
    try:
        return requests.get(url, timeout=10).json()
    except:
        return {}

# ─── CLAUDE ───────────────────────────────────────────────────────────────────
def claude_analyze(prompt: str) -> str:
    client = anthropic.Anthropic(api_key=CLAUDE_KEY)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text
    except Exception as e:
        return f"Errore analisi: {e}"

# ─── VELOCITY PRICE ALERTS ────────────────────────────────────────────────────
def process_price(symbol: str, current_price: float):
    if symbol not in PORTFOLIO:
        return

    now = time.time()
    price_history[symbol].append((now, current_price))

    # Cooldown check
    if now - last_alert_time.get(symbol, 0) < ALERT_COOLDOWN:
        return

    name       = PORTFOLIO[symbol]
    is_quantum = symbol in QUANTUM_STOCKS
    history    = price_history[symbol]

    # (window_secs, drop_label, drop_emoji, rise_label, rise_emoji, active_for_quantum)
    levels = [
        (5  * 60, "EMERGENCY",     "🚨", "ROCKET",      "🚀", True),
        (10 * 60, "ALLERTA ROSSA", "🔴", "VERDE FORTE", "💚", not is_quantum),
        (30 * 60, "ALLERTA GIALLA","🟡", "VERDE",        "🟢", not is_quantum),
    ]

    for window_secs, drop_label, drop_emoji, rise_label, rise_emoji, active in levels:
        if not active:
            continue

        cutoff        = now - window_secs
        window_prices = [(t, p) for t, p in history if t >= cutoff]

        if len(window_prices) < 2:
            continue

        ref_price = window_prices[0][1]
        pct       = (current_price - ref_price) / ref_price * 100
        minutes   = max(1, int((now - window_prices[0][0]) / 60))

        if pct <= -DROP_THRESHOLD:
            send_telegram(
                f"{drop_emoji} <b>{drop_label} — {name}</b>\n\n"
                f"📉 {pct:+.1f}% in {minutes} minuti\n"
                f"Da {ref_price:.2f} → {current_price:.2f}"
            )
            last_alert_time[symbol] = now
            print(f"{drop_label}: {name} {pct:+.1f}% in {minutes}min")
            return

        if pct >= DROP_THRESHOLD:
            send_telegram(
                f"{rise_emoji} <b>{rise_label} — {name}</b>\n\n"
                f"📈 +{pct:.1f}% in {minutes} minuti\n"
                f"Da {ref_price:.2f} → {current_price:.2f}"
            )
            last_alert_time[symbol] = now
            print(f"{rise_label}: {name} +{pct:.1f}% in {minutes}min")
            return

# ─── WEBSOCKET (US stocks) ────────────────────────────────────────────────────
def on_ws_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("type") == "trade":
            for trade in data.get("data", []):
                symbol = trade.get("s")
                price  = trade.get("p")
                if symbol and price:
                    process_price(symbol, price)
    except Exception as e:
        print(f"WS error: {e}")

def on_ws_open(ws):
    us_symbols = [s for s in PORTFOLIO if "." not in s]
    for symbol in us_symbols:
        ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
    print(f"WebSocket subscribed: {us_symbols}")

def on_ws_close(ws, *args):
    print("WebSocket closed. Reconnecting in 15s...")
    time.sleep(15)
    start_websocket_thread()

def start_websocket_thread():
    ws = websocket.WebSocketApp(
        f"wss://ws.finnhub.io?token={FINNHUB_KEY}",
        on_open=on_ws_open,
        on_message=on_ws_message,
        on_error=lambda ws, e: print(f"WS error: {e}"),
        on_close=on_ws_close,
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ─── EU STOCKS POLLING (every 5 min) ──────────────────────────────────────────
def poll_eu_stocks():
    eu_symbols = [s for s in PORTFOLIO if "." in s]
    quotes = get_quote_batch(eu_symbols)
    for symbol in eu_symbols:
        c = quotes.get(symbol, {}).get("c", 0)
        if c:
            process_price(symbol, c)

# ─── NEWS ALERTS ──────────────────────────────────────────────────────────────
def is_breaking_news(headline: str, summary: str) -> bool:
    """Use Claude to determine if a news item is truly breaking and market-moving.
    Returns True only for high-impact news (score >= 8/10)."""
    client = anthropic.Anthropic(api_key=CLAUDE_KEY)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": (
                    "You are a financial news filter for an active investor. "
                    "Rate this news item 1-10 for relevance to an investor holding this stock.\n\n"
                    "Score 8-10: CEO/CFO change, acquisition announced, bankruptcy, fraud, "
                    "regulatory ban, sanctions, major lawsuit outcome.\n"
                    "Score 6-7: earnings results (beat or miss), analyst upgrade or downgrade, "
                    "analyst reiterates rating, guidance revision, dividend change, "
                    "share buyback announced, major contract won or lost.\n"
                    "Score 1-5: generic sector overview, market commentary, background article, "
                    "minor product update, conference attendance, awards.\n\n"
                    f"Headline: {headline}\n"
                    f"Summary: {summary[:300]}\n\n"
                    "Reply with ONLY a single integer 1-10. Nothing else."
                )
            }]
        )
        score = int(msg.content[0].text.strip())
        print(f"News score {score}/10: {headline[:60]}")
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

            # Mark as seen immediately to avoid re-processing
            sent_news_ids.add(nid)

            # Claude filters: only truly breaking news passes
            if is_breaking_news(headline, summary):
                send_telegram(
                    f"🚨 <b>BREAKING NEWS — {name}</b>\n\n"
                    f"{headline}\n\n"
                    f"🔗 {url}"
                )
                print(f"Breaking news sent: {name} — {headline}")

            time.sleep(0.5)  # avoid hammering the API

# ─── MORNING BRIEFING 07:00 ───────────────────────────────────────────────────
def morning_briefing():
    print("Generating morning briefing...")

    quotes    = get_quote_batch(list(PORTFOLIO.keys()))
    lines     = []
    for symbol, name in PORTFOLIO.items():
        q = quotes.get(symbol, {})
        c, pc = q.get("c", 0), q.get("pc", 0)
        if c and pc:
            pct = (c - pc) / pc * 100
            lines.append(f"{name}: {c:.2f} ({pct:+.1f}%)")
        else:
            lines.append(f"{name}: N/A")

    portfolio_str = "\n".join(lines) or "N/A"
    news_str      = "\n".join(f"- {n['headline']}" for n in get_market_news()[:6])
    today_str     = datetime.now(STOCKHOLM_TZ).strftime("%A %d %B %Y")

    prompt = f"""Sei l'assistente trading personale di Roberto, investitore attivo.
Oggi è {today_str}. Briefing mattutino prima dell'apertura EU.

PORTAFOGLIO (var. da chiusura precedente):
{portfolio_str}

NEWS RECENTI:
{news_str}

Messaggio Telegram max 300 parole. Tono professionale, asciutto, come un buon analista.
Niente emoji eccessive — al massimo una per sezione titolo. Niente esclamazioni.

ASIA OVERNIGHT — cosa è successo stanotte
APERTURA EU — cosa aspettarsi oggi
PORTAFOGLIO — solo movimenti o news rilevanti, con i numeri
TEMA DEL GIORNO — 1 tema o titolo caldo
RISCHIO — 1 cosa concreta da monitorare

In italiano. Nessun fronzolo."""

    send_telegram(f"🌅 <b>BRIEFING MATTUTINO</b>\n\n{claude_analyze(prompt)}")
    print("Morning briefing sent.")

# ─── NY SUMMARY 16:00 ─────────────────────────────────────────────────────────
def ny_summary():
    print("Generating NY summary...")

    quotes = get_quote_batch(list(PORTFOLIO.keys()))
    lines  = []
    for symbol, name in PORTFOLIO.items():
        q = quotes.get(symbol, {})
        c, pc = q.get("c", 0), q.get("pc", 0)
        if c and pc:
            pct = (c - pc) / pc * 100
            icon = "+" if pct > 0 else ""
            lines.append(f"{name}: {c:.2f} ({icon}{pct:.1f}%)")
        else:
            lines.append(f"{name}: N/A")

    portfolio_str = "\n".join(lines) or "N/A"

    prompt = f"""Sono le 16:00 CET, NY ha aperto da 30 minuti. Summary rapido per Roberto.

PORTAFOGLIO:
{portfolio_str}

Messaggio Telegram max 200 parole. Tono professionale e asciutto, niente emoji eccessive.

NY APERTURA — direzione del mercato americano con i dati
PORTAFOGLIO — movimenti rilevanti finora con percentuali
DA MONITORARE — cosa può muoversi nel pomeriggio NY

In italiano. Solo i fatti."""

    send_telegram(f"🗽 <b>NY SUMMARY</b>\n\n{claude_analyze(prompt)}")
    print("NY summary sent.")

# ─── EU CLOSE SUMMARY 17:30 ───────────────────────────────────────────────────
def eu_close_summary():
    print("Generating EU close summary...")

    # Collect portfolio data grouped by sector
    from collections import defaultdict
    sector_data = defaultdict(list)
    big_movers  = []
    all_lines   = []

    quotes = get_quote_batch(list(PORTFOLIO_DATA.keys()))
    for symbol, (name, sector) in PORTFOLIO_DATA.items():
        q  = quotes.get(symbol, {})
        c  = q.get("c", 0)
        pc = q.get("pc", 0)

        if c and pc:
            pct  = (c - pc) / pc * 100
            icon = "+" if pct > 0 else ""
            line = f"{name}: {c:.2f} ({icon}{pct:.1f}%)"
            sector_data[sector].append((name, pct, line))
            all_lines.append(line)
            if abs(pct) >= DAY_THRESHOLD:
                direction = "SU" if pct > 0 else "GIU"
                big_movers.append(f"<b>{name}</b> {icon}{pct:.1f}% ({direction})")
        else:
            sector_data[sector].append((name, None, f"{name}: N/A"))
            all_lines.append(f"{name}: N/A")

    # Build sector summary
    sector_lines = []
    for sector, stocks in sector_data.items():
        valid = [(n, p, l) for n, p, l in stocks if p is not None]
        if valid:
            avg = sum(p for _, p, _ in valid) / len(valid)
            trend = "▲" if avg > 0.3 else "▼" if avg < -0.3 else "—"
            details = ", ".join(l for _, _, l in stocks)
            sector_lines.append(f"{trend} {sector}: {details}")
        else:
            details = ", ".join(l for _, _, l in stocks)
            sector_lines.append(f"— {sector}: {details}")

    portfolio_str = "\n".join(sector_lines)

    # Earnings tomorrow
    earnings = get_earnings_calendar()
    portfolio_tickers = set(PORTFOLIO.keys())
    relevant_earnings = [
        e for e in earnings
        if e.get("symbol") in portfolio_tickers
    ]
    other_earnings = [
        e for e in earnings[:10]
        if e.get("symbol") not in portfolio_tickers
    ]

    earnings_portfolio_str = "\n".join(
        f"⚠️ {e['symbol']} — EPS est: {e.get('epsEstimate', 'N/A')}"
        for e in relevant_earnings
    ) or "Nessuno dei tuoi titoli"

    earnings_other_str = "\n".join(
        f"• {e['symbol']}"
        for e in other_earnings[:5]
    ) or "N/A"

    # Market news
    news_str = "\n".join(f"- {n['headline']}" for n in get_market_news()[:6])

    big_movers_str = "\n".join(big_movers) or "Nessun movimento superiore al 2.5%"

    # Build sector averages
    sector_avgs = {}
    for sector, stocks in sector_data.items():
        valid_pcts = [p for _, p, _ in stocks if p is not None]
        if valid_pcts:
            sector_avgs[sector] = sum(valid_pcts) / len(valid_pcts)

    sector_avg_str = "\n".join(
        f"  {sector}: {avg:+.2f}%"
        for sector, avg in sorted(sector_avgs.items(), key=lambda x: x[1], reverse=True)
    )

    prompt = f"""Sei l'assistente trading di Roberto. Sono le 17:30, i mercati europei hanno chiuso.

PORTAFOGLIO PER SETTORE (dati reali):
{portfolio_str}

MEDIE SETTORIALI GIORNALIERE:
{sector_avg_str}

MOVERS >±2.5%:
{big_movers_str}

NEWS DEL GIORNO:
{news_str}

EARNINGS CALL DOMANI — TUOI TITOLI:
{earnings_portfolio_str}

EARNINGS CALL DOMANI — ALTRI:
{earnings_other_str}

Scrivi UN messaggio Telegram (max 450 parole). Tono professionale, da analista senior.
Usa <b>grassetto</b> per titoli con movimento >2.5% e per i nomi sezione.
STRUTTURA FISSA — rispetta sempre questo ordine:

<b>Chiusura EU</b> — 2 righe sentiment generale mercati europei oggi

<b>Trend Settoriali</b> — SEMPRE presenti, SEMPRE in questo ordine, con trend e numeri:
  Lusso e Fashion (Hermes, LVMH, Dior, EssilorLuxottica)
  Tech (Microsoft, Alphabet, SAP, Dassault, Adyen, Spotify)
  Semiconduttori (ASML, ASM, TSMC, AMAT, Broadcom)
  Quantum (IonQ, D-Wave, Rigetti)
  Finance (UniCredit, Intesa)
  Energia (Engie, Siemens Energy)
  Industriale (Airbus, Schneider)
  Per ogni settore: trend su/giu/flat + media % + titolo piu rilevante

<b>Movers del Giorno</b> — solo titoli >2.5%, in grassetto, con causa in una riga

<b>Earnings Domani</b> — titoli di Roberto (suggerisci se valutare uscita), poi altri importanti

<b>Stock da Guardare</b> — 1 titolo fuori portafoglio interessante

<b>Domani</b> — 1 cosa concreta da monitorare

In italiano. Solo i fatti."""

    send_telegram(f"🇪🇺 <b>CHIUSURA EU</b>\n\n{claude_analyze(prompt)}")

    # Separate message for 52w extremes if any
    if new_extremes:
        send_telegram("🏆 <b>NUOVI ESTREMI</b>\n\n" + "\n".join(new_extremes))

    print("EU close summary sent.")

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

    start_websocket_thread()

    eu_close_summary()  # TEMP: run once on startup, remove tomorrow

    schedule.every().day.at("07:00").do(morning_briefing)
    schedule.every().day.at("16:00").do(ny_summary)
    schedule.every().day.at("17:30").do(eu_close_summary)
    schedule.every(5).minutes.do(poll_eu_stocks)
    schedule.every(1).hours.do(check_news_alerts)
    schedule.every().day.at("00:01").do(daily_reset)

    send_telegram(
        "🤖 <b>TradingAlertRF attivo!</b>\n\n"
        "🌅 Briefing mattutino: 07:00\n"
        "🗽 NY Summary: 16:00\n"
        "🇪🇺 Chiusura EU: 17:30\n"
        "🚨 Emergency: ≥2% in ≤5 min\n"
        "🔴 Rossa: ≥2% in ≤10 min\n"
        "🟡 Gialla: ≥2% in ≤30 min\n"
        "📈 Rialzo: ≥3% in 30 min\n"
        "📰 News rilevanti: continuo\n"
        "⚛️ Quantum (IONQ/RGTI/QBTS): solo Emergency"
    )

    while True:
        schedule.run_pending()
        time.sleep(10)
