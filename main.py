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

# Roberto's portfolio — Finnhub symbol: display name
PORTFOLIO = {
    "ASML":     "ASML",
    "MSFT":     "Microsoft",
    "GOOGL":    "Alphabet",
    "RMS.PA":   "Hermès",
    "AIR.PA":   "Airbus",
    "MC.PA":    "LVMH",
    "UCG.MI":   "UniCredit",
    "EL.PA":    "EssilorLuxottica",
    "CDI.PA":   "Christian Dior",
    "ADYEN.AS": "Adyen",
    "DSY.PA":   "Dassault Systèmes",
    "SAP":      "SAP",
    "ENGI.PA":  "Engie",
    "DTE.DE":   "Deutsche Telekom",
    "ISP.MI":   "Intesa Sanpaolo",
    "SONY":     "Sony",
    "TSM":      "Taiwan Semi",
    "RACE":     "Ferrari",
    "ONON":     "ON Holding",
    "SU.PA":    "Schneider Electric",
    "SPOT":     "Spotify",
    "IONQ":     "IonQ",
    "QBTS":     "D-Wave",
    "RGTI":     "Rigetti",
    "ENR.DE":   "Siemens Energy",
    "ASMIY":    "ASM International",
    "AMAT":     "Applied Materials",
    "AVGO":     "Broadcom",
}

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
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}"
    try:
        return requests.get(url, timeout=10).json()
    except:
        return {}

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
            model="claude-sonnet-4-20250514",
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
    for symbol in PORTFOLIO:
        if "." in symbol:
            q = get_quote(symbol)
            c = q.get("c", 0)
            if c:
                process_price(symbol, c)

# ─── NEWS ALERTS ──────────────────────────────────────────────────────────────
def is_breaking_news(headline: str, summary: str) -> bool:
    """Use Claude to determine if a news item is truly breaking and market-moving.
    Returns True only for high-impact news (score >= 8/10)."""
    client = anthropic.Anthropic(api_key=CLAUDE_KEY)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
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

    lines = []
    for symbol, name in PORTFOLIO.items():
        q = get_quote(symbol)
        c, pc = q.get("c", 0), q.get("pc", 0)
        if c and pc:
            pct = (c - pc) / pc * 100
            lines.append(f"{name}: {c:.2f} ({pct:+.1f}%)")

    portfolio_str = "\n".join(lines) or "N/A"
    news_str      = "\n".join(f"- {n['headline']}" for n in get_market_news()[:6])
    today_str     = datetime.now(STOCKHOLM_TZ).strftime("%A %d %B %Y")

    prompt = f"""Sei l'assistente trading personale di Roberto, investitore attivo.
Oggi è {today_str}. Briefing mattutino prima dell'apertura EU.

PORTAFOGLIO (var. da chiusura precedente):
{portfolio_str}

NEWS RECENTI:
{news_str}

Messaggio Telegram max 300 parole:
🌏 ASIA OVERNIGHT — cosa è successo stanotte
🇪🇺 APERTURA EU — cosa aspettarsi
📊 PORTAFOGLIO — solo movimenti o news rilevanti
🔥 TEMA DEL GIORNO — 1 tema caldo
⚠️ RISCHIO — 1 cosa da monitorare

Diretto, professionale, in italiano."""

    send_telegram(f"🌅 <b>BRIEFING MATTUTINO</b>\n\n{claude_analyze(prompt)}")
    print("Morning briefing sent.")

# ─── NY SUMMARY 16:00 ─────────────────────────────────────────────────────────
def ny_summary():
    print("Generating NY summary...")

    lines = []
    for symbol, name in PORTFOLIO.items():
        q = get_quote(symbol)
        c, pc = q.get("c", 0), q.get("pc", 0)
        if c and pc:
            pct = (c - pc) / pc * 100
            icon = "📈" if pct > 0 else "📉" if pct < 0 else "➡️"
            lines.append(f"{icon} {name}: {c:.2f} ({pct:+.1f}%)")

    portfolio_str = "\n".join(lines) or "N/A"

    prompt = f"""Sono le 16:00 CET, NY ha aperto da 30 minuti. Summary rapido per Roberto.

PORTAFOGLIO:
{portfolio_str}

Messaggio Telegram max 200 parole:
🗽 NY APERTURA — direzione del mercato americano
📊 PORTAFOGLIO — movimenti rilevanti finora
👀 DA MONITORARE — cosa può muoversi nel pomeriggio NY

In italiano, diretto, concreto."""

    send_telegram(f"🗽 <b>NY SUMMARY</b>\n\n{claude_analyze(prompt)}")
    print("NY summary sent.")

# ─── EU CLOSE SUMMARY 17:30 ───────────────────────────────────────────────────
def eu_close_summary():
    print("Generating EU close summary...")

    # Collect portfolio data
    portfolio_data = []
    big_movers     = []   # > DAY_THRESHOLD %
    new_extremes   = []   # 52w high/low

    for symbol, name in PORTFOLIO.items():
        q   = get_quote(symbol)
        c   = q.get("c", 0)
        pc  = q.get("pc", 0)
        h   = q.get("h", 0)   # today's high
        l   = q.get("l", 0)   # today's low
        h52 = q.get("t", 0)   # Finnhub doesn't give 52w directly; use as timestamp placeholder

        if not c or not pc:
            continue

        pct  = (c - pc) / pc * 100
        icon = "📈" if pct > 0 else "📉" if pct < 0 else "➡️"
        portfolio_data.append(f"{icon} {name}: {c:.2f} ({pct:+.1f}%)")

        # Flag big daily movers
        if abs(pct) >= DAY_THRESHOLD:
            flag = "🔺" if pct > 0 else "🔻"
            big_movers.append(f"{flag} <b>{name}</b> {pct:+.1f}%")

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

    portfolio_str  = "\n".join(portfolio_data) or "N/A"
    big_movers_str = "\n".join(big_movers) or "Nessun movimento superiore al 2.5%"

    prompt = f"""Sei l'assistente trading di Roberto. Sono le 17:30, i mercati europei hanno chiuso.

PERFORMANCE PORTAFOGLIO OGGI:
{portfolio_str}

MOVIMENTI SIGNIFICATIVI (>±2.5%):
{big_movers_str}

NEWS DEL GIORNO:
{news_str}

EARNINGS CALL DOMANI — TUOI TITOLI:
{earnings_portfolio_str}

EARNINGS CALL DOMANI — ALTRI TITOLI DI RILIEVO:
{earnings_other_str}

Scrivi UN messaggio Telegram (max 400 parole) con queste sezioni:

🇪🇺 CHIUSURA EU — sentiment generale dei mercati europei oggi
🔺🔻 MOVERS DEL GIORNO — titoli con movimento >2.5%, in grassetto, con breve spiegazione
📰 NEWS CHIAVE — 2-3 notizie che hanno mosso i mercati oggi
⚠️ EARNINGS DOMANI — avvisa Roberto se ha titoli con earnings call domani (suggerisci se valutare uscita), poi altri titoli importanti
💡 STOCK DA GUARDARE — 1-2 titoli interessanti fuori portafoglio da tenere d'occhio
🎯 TAKEAWAY — 1-2 osservazioni chiave sulla giornata
👀 DOMANI — 1 cosa da monitorare all'apertura

In italiano, diretto, concreto. I movimenti >2.5% in grassetto."""

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
