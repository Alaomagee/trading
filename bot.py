# ===============================
# PHASE 4 — PRODUCTION WSGI (GUNICORN) + CLEAN ARCHITECTURE
# ===============================
# What changed in Phase 4:
# ✔ Removed ngrok (production does NOT use it)
# ✔ Flask app exposed as WSGI app for gunicorn
# ✔ Trading loop runs in background thread safely
# ✔ Webhook URL is STATIC (set once)
# ✔ Clear separation: config / app / trading
#
# HOW YOU RUN THIS IN PRODUCTION:
# 1) Expose server with real HTTPS (Cloudflare, Nginx, Railway, Render, VPS)
# 2) Set webhook ONCE:
#    https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://YOUR_DOMAIN/<TOKEN>
# 3) Start bot:
#    gunicorn -w 1 -b 0.0.0.0:5000 bot:app

print("MEXC BOT STARTING — PHASE 4 (PRODUCTION)")

import os
import time
import hmac
import hashlib
import threading
import sqlite3
import requests
from urllib.parse import urlencode
from dotenv import load_dotenv
from flask import Flask, request

# ===============================
# 1. CONFIG
# ===============================
load_dotenv()

API_KEY = os.getenv("MEXC_API_KEY")
SECRET_KEY = os.getenv("MEXC_SECRET_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.mexc.com"

INTERVAL = "1m"
CANDLE_LIMIT = 5
CHECK_INTERVAL = 20
PRICE_CHECK_INTERVAL = 5
SELL_TIMEOUT = 7 * 60
TRAIL_PERCENT = 0.15

BUY_USDT_AMOUNT = 1.0
TARGET_MULTIPLIER = 10.0
bot_running = True

# ===============================
# 2. DATABASE
# ===============================
conn = sqlite3.connect("positions.db", check_same_thread=False)
cur = conn.cursor()
cur.execute(
    """
    CREATE TABLE IF NOT EXISTS positions (
        symbol TEXT PRIMARY KEY,
        qty REAL,
        entry_price REAL,
        entry_time REAL,
        high_price REAL
    )
    """
)
conn.commit()

# ===============================
# 3. TELEGRAM
# ===============================

def send_telegram(text, chat_id=None):
    if not chat_id:
        chat_id = CHAT_ID
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10
        )
    except Exception as e:
        print("Telegram error:", e)

# ===============================
# 4. MEXC HELPERS
# ===============================

def sign(params):
    q = urlencode(params)
    return hmac.new(SECRET_KEY.encode(), q.encode(), hashlib.sha256).hexdigest()


def mexc_get(path, params=None):
    try:
        r = requests.get(BASE_URL + path, params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def mexc_post(path, params):
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = sign(params)
    headers = {"X-MEXC-APIKEY": API_KEY}
    return requests.post(BASE_URL + path, params=params, headers=headers).json()

# ===============================
# 5. TRADING ACTIONS
# ===============================

def market_buy(symbol):
    data = mexc_post("/api/v3/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": BUY_USDT_AMOUNT
    })

    if "orderId" not in data:
        send_telegram(f"❌ BUY FAILED {symbol}\n{data}")
        return

    qty = float(data.get("executedQty", 0))
    price = float(data["fills"][0]["price"])

    cur.execute(
        "INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?, ?)",
        (symbol, qty, price, time.time(), price)
    )
    conn.commit()

    send_telegram(f"🟢 BUY EXECUTED\n{symbol}\nQty: {qty}\nPrice: {price}")


def market_sell(symbol, reason):
    row = cur.execute(
        "SELECT qty FROM positions WHERE symbol=?", (symbol,)
    ).fetchone()
    if not row:
        return

    qty = row[0]

    data = mexc_post("/api/v3/order", {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty
    })

    cur.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
    conn.commit()

    send_telegram(f"🔴 SELL EXECUTED ({reason})\n{symbol}\n{data}")

# ===============================
# 6. FLASK APP (WSGI)
# ===============================
app = Flask(__name__)

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    global bot_running, BUY_USDT_AMOUNT, TARGET_MULTIPLIER

    update = request.get_json(silent=True)
    if not update or "message" not in update:
        return "OK"

    msg = update["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    if text == "/help":
        send_telegram(
            "🤖 COMMANDS\n"
            "/status\n"
            "/positions\n"
            "/pause /resume\n"
            "/setbuy <amt>\n"
            "/settarget <x>",
            chat_id
        )

    elif text == "/status":
        count = cur.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        send_telegram(
            f"Running: {bot_running}\n"
            f"Buy: {BUY_USDT_AMOUNT} USDT\n"
            f"Target: {TARGET_MULTIPLIER}x\n"
            f"Positions: {count}",
            chat_id
        )

    elif text == "/positions":
        rows = cur.execute("SELECT * FROM positions").fetchall()
        if not rows:
            send_telegram("No open positions", chat_id)
        else:
            out = "📊 OPEN POSITIONS\n"
            for s, q, e, t, h in rows:
                out += f"{s} | qty={q} | entry={e}\n"
            send_telegram(out, chat_id)

    elif text == "/pause":
        bot_running = False
        send_telegram("⏸ Bot paused", chat_id)

    elif text == "/resume":
        bot_running = True
        send_telegram("▶️ Bot resumed", chat_id)

    elif text.startswith("/setbuy"):
        try:
            BUY_USDT_AMOUNT = float(text.split()[1])
            send_telegram(f"Buy amount set to {BUY_USDT_AMOUNT}", chat_id)
        except:
            send_telegram("Usage: /setbuy 5", chat_id)

    elif text.startswith("/settarget"):
        try:
            TARGET_MULTIPLIER = float(text.split()[1])
            send_telegram(f"Target set to {TARGET_MULTIPLIER}x", chat_id)
        except:
            send_telegram("Usage: /settarget 10", chat_id)

    return "OK"

# ===============================
# 7. BACKGROUND TRADER
# ===============================

def trading_loop():
    known = set()
    send_telegram("✅ PHASE 4 BOT LIVE (PRODUCTION)")

    while True:
        if not bot_running:
            time.sleep(2)
            continue

        info = mexc_get("/api/v3/exchangeInfo")
        if not info:
            time.sleep(5)
            continue

        symbols = {
            s["symbol"] for s in info["symbols"]
            if s["status"] == "TRADING" and s["symbol"].endswith("USDT")
        }

        new = symbols - known
        known = symbols

        for symbol in new:
            candles = mexc_get("/api/v3/klines", {
                "symbol": symbol,
                "interval": INTERVAL,
                "limit": CANDLE_LIMIT
            })

            if candles and all(c[1] == c[4] for c in candles):
                market_buy(symbol)

        rows = cur.execute("SELECT * FROM positions").fetchall()
        for s, q, entry, t, high in rows:
            price = float(mexc_get("/api/v3/ticker/price", {"symbol": s})["price"])

            if price > high:
                cur.execute("UPDATE positions SET high_price=? WHERE symbol=?", (price, s))
                conn.commit()
                high = price

            if price >= entry * TARGET_MULTIPLIER:
                market_sell(s, "TARGET")
            elif price <= high * (1 - TRAIL_PERCENT):
                market_sell(s, "TRAIL")
            elif time.time() - t >= SELL_TIMEOUT:
                market_sell(s, "TIMEOUT")

        time.sleep(CHECK_INTERVAL)

# ===============================
# 8. THREAD START (gunicorn-safe)
# ===============================
threading.Thread(target=trading_loop, daemon=True).start()
