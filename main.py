#!/usr/bin/env python3
"""
TradingAlertRF — Roberto's personal trading assistant
Real-time price alerts via WebSocket + scheduled briefings via Telegram.

Alert system (drops only, measured from rolling price history):
  🚨 EMERGENCY   — drop >= 2% in <= 5 min
  🔴 ROSSA       — drop >= 2% in <= 10 min
  🟡 GIALLA      — drop >= 2% in <= 30 min
  Quantum stocks (IONQ, RGTI, QBTS): EMERGENCY only
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
DROP_THRESHOLD  = 2.0   # % drop to trigger any alert
RISE_THRESHOLD  = 3.0   # % rise to trigger opportunity alert

# Quantum stocks — EMERGENCY only, skip ROSSA and GIALLA
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

# Rolling price history: {symbol: deque of (unix_timestamp, price)}
price_history: dict = {s: deque(maxlen=500) for s in PORTFOLIO}

# Cooldown: track last alert time per symbol (30 min cooldown)
last_alert_time: dict = {}
ALERT_COOLDOWN  = 30 * 60  # seconds

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

# ─── CLAUDE ───────────────────────────────────────────────────────────────────
def claude_analyze(prompt: str) -> str:
    client = anthropic.Anthropic(api_key=CLAUDE_KEY)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text
    except Exception as e:
        return f"Errore analisi: {e}"

# ─── PRICE ALERT LOGIC ────────────────────────────────────────────────────────
def process_price(symbol: str, current_price: float):
    """Add price to history and check all alert levels."""
    if symbol not in PORTFOLIO:
        return

    now = time.time()
    price_history[symbol].append((now, current_price))

    # Cooldown check — don't spam alerts on same symbol
    last = last_alert_time.get(symbol, 0)
    if now - last < ALERT_COOLDOWN:
        return

    name       = PORTFOLIO[symbol]
    is_quantum = symbol in QUANTUM_STOCKS
    history    = price_history[symbol]

    # Alert levels: (window_seconds, label, emoji, active_for_quantum)
    levels = [
        (5  * 60, "EMERGENCY",    "🚨", True),
        (10 * 60, "ALLERTA ROSSA","🔴", not is_quantum),
        (30 * 60, "ALLERTA GIALLA","🟡", not is_quantum),
    ]

    for window_secs, label, emoji, active in levels:
        if not active:
            continue

        cutoff        = now - window_secs
        window_prices = [(t, p) for t, p in history if t >= cutoff]

        if len(window_prices) < 2:
            continue

        ref_price = window_prices[0][1]   # oldest price in the window
        pct       = (current_price - ref_price) / ref_price * 100
        minutes   = int((now - window_prices[0][0]) / 60)

        # DROP alert
        if pct <= -DROP_THRESHOLD:
            send_telegram(
                f"{emoji} <b>{label} — {name}</b>\n\n"
                f"📉 {pct:+.1f}% in {minutes} minuti\n"
                f"Da {ref_price:.2f} → {current_price:.2f}"
            )
            last_alert_time[symbol] = now
            print(f"{label}: {name} {pct:+.1f}% in {minutes}min")
            return  # Fire only the most severe level

        # RISE alert (single level, all stocks)
        if pct >= RISE_THRESHOLD and window_secs == 30 * 60:
            send_telegram(
                f"📈 <b>RIALZO — {name}</b>\n\n"
                f"+{pct:.1f}% in {minutes} minuti\n"
                f"Da {ref_price:.2f} → {current_price:.2f}"
            )
            last_alert_time[symbol] = now
            print(f"Rise alert: {name} +{pct:.1f}% in {minutes}min")
            return

# ─── WEBSOCKET (US stocks real-time) ──────────────────────────────────────────
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
    for symbol in eu_symbols:
        q = get_quote(symbol)
        c = q.get("c", 0)
        if c:
            process_price(symbol, c)

# ─── NEWS ALERTS (relevant only) ──────────────────────────────────────────────
RELEVANT_KEYWORDS = [
    "earnings", "revenue", "profit", "loss", "guidance", "outlook",
    "acquisition", "merger", "takeover", "buyout",
    "upgrade", "downgrade", "target price", "price target",
    "fda", "regulatory", "lawsuit", "investigation",
    "ceo", "cfo", "resign", "appoint",
    "dividend", "buyback", "recall", "fine", "penalty",
]

def check_news_alerts():
    for symbol, name in PORTFOLIO.items():
        for news in get_company_news(symbol):
            nid      = str(news.get("id") or news.get("datetime") or "")
            headline = news.get("headline", "")
            summary  = (news.get("summary") or "")
            url      = news.get("url", "")

            if not nid or nid in sent_news_ids:
                continue

            text = (headline + " " + summary).lower()
            if any(kw in text for kw in RELEVANT_KEYWORDS):
                sent_news_ids.add(nid)
                send_telegram(
                    f"📰 <b>NEWS — {name}</b>\n\n"
                    f"{headline}\n\n"
                    f"🔗 {url}"
                )
                print(f"News: {name} — {headline}")
                time.sleep(1)

# ─── MORNING BRIEFING (07:00 Stockholm) ───────────────────────────────────────
def morning_briefing():
    print("Generating morning briefing...")

    lines = []
    for symbol, name in PORTFOLIO.items():
        q = get_quote(symbol)
        c, pc = q.get("c", 0), q.get("pc", 0)
        if c and pc:
            pct = (c - pc) / pc * 100
            lines.append(f"{name}: {c:.2f} ({pct:+.1f}%)")

    portfolio_str = "\n".join(lines) or "Dati non disponibili"
    news_str      = "\n".join(f"- {n['headline']}" for n in get_market_news()[:6])
    today_str     = datetime.now(STOCKHOLM_TZ).strftime("%A %d %B %Y")

    prompt = f"""Sei l'assistente trading personale di Roberto, investitore attivo su mercati europei e globali.
Oggi è {today_str}. Genera un briefing mattutino conciso prima dell'apertura EU.

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

# ─── NY SUMMARY (16:00 Stockholm = 10:00 ET) ──────────────────────────────────
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

    portfolio_str = "\n".join(lines) or "Dati non disponibili"

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
    schedule.every(5).minutes.do(poll_eu_stocks)
    schedule.every(5).minutes.do(check_news_alerts)
    schedule.every().day.at("00:01").do(daily_reset)

    send_telegram(
        "🤖 <b>TradingAlertRF attivo!</b>\n\n"
        "🌅 Briefing: 07:00\n"
        "🗽 NY Summary: 16:00\n"
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
