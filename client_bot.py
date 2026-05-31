import os
import time
import hmac
import hashlib
import requests
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ─────────────────────────────────────────────
# CONFIG — set as Railway environment variables
# ─────────────────────────────────────────────
BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("CLIENT_BOT_TOKEN", "")
SYMBOL             = os.environ.get("SYMBOL", "BTCUSDT")
LEVERAGE           = int(os.environ.get("LEVERAGE", "60"))
FUTURES_BASE       = "https://fapi.binance.com"
UK_TZ              = ZoneInfo("Europe/London")

# Chat ID is captured automatically on first /start message
authorised_chat_id = None

# ─────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────
def send_telegram(chat_id: str, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"[Telegram] Error: {e}")


def get_updates(offset: int = None):
    params = {"timeout": 30, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params=params, timeout=35
        )
        return resp.json().get("result", [])
    except Exception as e:
        print(f"[Telegram] Poll error: {e}")
        return []


# ─────────────────────────────────────────────
# BINANCE HELPERS
# ─────────────────────────────────────────────
def sign(params: dict) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    params["signature"] = hmac.new(
        BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    return params


def binance_get(path: str, params: dict = None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params = sign(params)
    resp = requests.get(
        f"{FUTURES_BASE}{path}",
        params=params,
        headers={"X-MBX-APIKEY": BINANCE_API_KEY},
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()


def get_balance() -> float:
    data = binance_get("/fapi/v2/account")
    return float(data.get("totalWalletBalance", 0))


def get_open_position():
    data = binance_get("/fapi/v2/positionRisk", {"symbol": SYMBOL})
    for pos in data:
        if float(pos.get("positionAmt", 0)) != 0:
            return pos
    return None


def get_mark_price() -> float:
    resp = requests.get(
        f"{FUTURES_BASE}/fapi/v1/premiumIndex",
        params={"symbol": SYMBOL}, timeout=10
    )
    resp.raise_for_status()
    return float(resp.json()["markPrice"])


def get_todays_pnl() -> float:
    """Sum realised PnL from income records today (UK time)."""
    uk_now = datetime.now(UK_TZ)
    start_of_day = uk_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start_of_day.astimezone(timezone.utc).timestamp() * 1000)
    try:
        data = binance_get("/fapi/v1/income", {
            "incomeType": "REALIZED_PNL",
            "startTime": start_ms,
            "limit": 1000
        })
        return sum(float(x["income"]) for x in data)
    except Exception:
        return 0.0


def get_recent_trades(limit: int = 10):
    """Fetch recent closed trades."""
    try:
        data = binance_get("/fapi/v1/userTrades", {"symbol": SYMBOL, "limit": limit})
        return data
    except Exception:
        return []


# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────
def handle_status(chat_id: str):
    try:
        position = get_open_position()
        mark = get_mark_price()

        if position:
            entry = float(position["entryPrice"])
            amt   = float(position["positionAmt"])
            side  = "LONG 🟢" if amt > 0 else "SHORT 🔴"
            liq   = float(position.get("liquidationPrice", 0))

            send_telegram(chat_id,
                f"<b>Open Position: {side}</b>\n\n"
                f"Symbol: {SYMBOL}\n"
                f"Entry:  ${entry:,.2f}\n"
                f"Mark:   ${mark:,.2f}\n"
                f"Liquidation: ${liq:,.2f}"
            )
        else:
            send_telegram(chat_id, "📊 No open position")
    except Exception as e:
        send_telegram(chat_id, f"❌ Status error: {e}")


def handle_pnl(chat_id: str):
    try:
        pnl = get_todays_pnl()
        uk_now = datetime.now(UK_TZ).strftime("%d %b %Y")
        emoji = "✅" if pnl >= 0 else "❌"
        send_telegram(chat_id,
            f"{emoji} <b>Today's PnL ({uk_now})</b>\n\n"
            f"Realised: ${pnl:+.2f}"
        )
    except Exception as e:
        send_telegram(chat_id, f"❌ PnL error: {e}")


def handle_balance(chat_id: str):
    try:
        balance = get_balance()
        send_telegram(chat_id,
            f"💰 <b>Account Balance</b>\n\n"
            f"${balance:,.2f} USDT"
        )
    except Exception as e:
        send_telegram(chat_id, f"❌ Balance error: {e}")


def handle_history(chat_id: str):
    try:
        trades = get_recent_trades(10)
        if not trades:
            send_telegram(chat_id, "No recent trades found.")
            return

        lines = ["<b>Recent Trades</b>\n"]
        seen = set()
        count = 0
        for t in reversed(trades):
            trade_id = t.get("orderId")
            if trade_id in seen:
                continue
            seen.add(trade_id)
            side    = "🟢 LONG" if t["side"] == "BUY" else "🔴 SHORT"
            pnl     = float(t.get("realizedPnl", 0))
            price   = float(t["price"])
            qty     = float(t["qty"])
            ts      = datetime.fromtimestamp(t["time"] / 1000, tz=UK_TZ).strftime("%d/%m %H:%M")
            emoji   = "✅" if pnl > 0 else "❌" if pnl < 0 else "➖"
            lines.append(f"{emoji} {side} | ${price:,.0f} | PnL: ${pnl:+.2f} | {ts}")
            count += 1
            if count >= 8:
                break

        send_telegram(chat_id, "\n".join(lines))
    except Exception as e:
        send_telegram(chat_id, f"❌ History error: {e}")


# ─────────────────────────────────────────────
# MAIN POLL LOOP
# ─────────────────────────────────────────────
def poll_loop():
    global authorised_chat_id
    offset = None
    print("[Client Bot] Polling started...")

    while True:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text    = msg.get("text", "").strip().lower()

            if not chat_id or not text:
                continue

            # First contact — register chat ID
            if authorised_chat_id is None:
                authorised_chat_id = chat_id
                print(f"[Client Bot] Registered chat ID: {chat_id}")
                send_telegram(chat_id,
                    "👋 <b>Welcome to your stats bot!</b>\n\n"
                    "Commands:\n"
                    "/status — current position"
                )
                continue

            # Only respond to the registered user
            if chat_id != authorised_chat_id:
                send_telegram(chat_id, "❌ Unauthorised.")
                continue

            cmd = text.split()[0].replace("/", "").split("@")[0]

            if cmd == "start":
                send_telegram(chat_id,
                    "Commands:\n"
                    "/status — current position"
                )
            elif cmd == "status":
                handle_status(chat_id)
            else:
                send_telegram(chat_id, "Use /status to check the current position.")

        time.sleep(1)


if __name__ == "__main__":
    poll_loop()
