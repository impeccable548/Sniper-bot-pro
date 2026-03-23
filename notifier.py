# notifier.py - Telegram notification support
import requests
import logging
import os

logger = logging.getLogger(__name__)


def _send(token: str, chat_id: str, text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=8
        )
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return False


def notify(message: str) -> bool:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    return _send(token, chat_id, message)


def notify_buy(token_address: str, amount_sol: float, entry_price_usd: float,
               tp_pct: float, sl_pct: float, tx_sig: str):
    msg = (
        f"⚡ <b>SNIPER PRO — BUY EXECUTED</b>\n\n"
        f"🪙 Token: <code>{token_address[:16]}…</code>\n"
        f"💰 Amount: <b>{amount_sol} SOL</b>\n"
        f"📈 Entry: <b>${entry_price_usd:.10f}</b>\n"
        f"🎯 Take Profit: +{tp_pct}%\n"
        f"🛑 Stop Loss: -{sl_pct}%\n"
        f"🔗 TX: <code>{tx_sig[:20]}…</code>"
    )
    return notify(msg)


def notify_sell(token_address: str, reason: str, pnl_pct: float,
                pnl_usd: float, exit_price_usd: float, tx_sig: str = ""):
    emoji = "🎯" if pnl_pct >= 0 else "🛑"
    sign  = "+" if pnl_pct >= 0 else ""
    msg = (
        f"{emoji} <b>SNIPER PRO — {reason.upper()}</b>\n\n"
        f"🪙 Token: <code>{token_address[:16]}…</code>\n"
        f"📊 Exit Price: <b>${exit_price_usd:.10f}</b>\n"
        f"💵 P&L: <b>{sign}{pnl_pct:.2f}% ({sign}${pnl_usd:.2f})</b>\n"
        + (f"🔗 TX: <code>{tx_sig[:20]}…</code>" if tx_sig else "")
    )
    return notify(msg)


def notify_safety_fail(token_address: str, score: int, warnings: list):
    top = warnings[0] if warnings else "Unknown risk"
    msg = (
        f"⛔ <b>SAFETY CHECK BLOCKED BUY</b>\n\n"
        f"🪙 Token: <code>{token_address[:16]}…</code>\n"
        f"🛡️ Safety Score: <b>{score}/100</b>\n"
        f"⚠️ {top}"
    )
    return notify(msg)


def test_notification() -> dict:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return {"success": False, "error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"}
    ok = _send(token, chat_id, "✅ <b>Sniper Pro</b> — Test notification. Bot is connected!")
    return {"success": ok, "error": None if ok else "Send failed — check your bot token and chat ID"}
