# TradingAlertRF Bot

Bot personale di trading che invia alert real-time e briefing via Telegram.

## Cosa ricevi

| Orario | Messaggio |
|--------|-----------|
| 07:00 | 🌅 Briefing mattutino (Asia overnight, apertura EU, portafoglio, tema del giorno) |
| 16:00 | 🗽 NY Summary (30 min dopo apertura Wall Street) |
| Real-time | 🚨🔴🟡 Alert prezzi per movimenti bruschi |
| Continuo | 📰 News rilevanti sui tuoi titoli |

## Sistema alert prezzi

Basato sulla **velocità** del movimento, non sul valore assoluto:

| Alert | Soglia | Finestra |
|-------|--------|----------|
| 🚨 EMERGENCY | ≥ -2% | ≤ 5 minuti |
| 🔴 ALLERTA ROSSA | ≥ -2% | ≤ 10 minuti |
| 🟡 ALLERTA GIALLA | ≥ -2% | ≤ 30 minuti |
| 📈 RIALZO | ≥ +3% | 30 minuti |

**Quantum stocks** (IONQ, RGTI, QBTS): solo EMERGENCY — troppo volatili per gli altri livelli.

Cooldown: 30 minuti per simbolo — niente spam.

## Copertura

- **US stocks** (ASML, MSFT, GOOGL, TSM, SPOT, IONQ...): prezzi real-time via WebSocket
- **EU stocks** (Hermès, Airbus, LVMH, Ferrari, Adyen...): polling ogni 5 minuti

## Setup su Railway

### 1. Variabili d'ambiente
Vai su Railway → progetto → Variables → Add Variable:

| Nome | Dove trovarlo |
|------|---------------|
| `TELEGRAM_TOKEN` | @BotFather su Telegram |
| `CHAT_ID` | Il tuo chat ID Telegram |
| `FINNHUB_KEY` | finnhub.io → Dashboard |
| `CLAUDE_KEY` | console.anthropic.com → API Keys |

### 2. Deploy
Railway detecta automaticamente Python dal `requirements.txt`.
Il `Procfile` dice a Railway di avviarlo come worker (non web server).

## Modificare la configurazione

**Soglia alert** — cambia in `main.py`:
```python
DROP_THRESHOLD = 2.0   # % calo per triggerare alert
RISE_THRESHOLD = 3.0   # % rialzo per triggerare alert
```

**Orari briefing**:
```python
schedule.every().day.at("07:00").do(morning_briefing)
schedule.every().day.at("16:00").do(ny_summary)
```

**Aggiungere un titolo al portafoglio**:
```python
PORTFOLIO = {
    ...
    "TICKER": "Nome Display",
}
```

**Aggiungere un quantum stock** (solo Emergency):
```python
QUANTUM_STOCKS = {"IONQ", "QBTS", "RGTI", "NUOVO_TICKER"}
```
