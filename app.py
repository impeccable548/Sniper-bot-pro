# app.py - Sniper Pro Bot — Flask API
import os
import logging
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv, set_key, dotenv_values

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Bot manager (lazy-init so it picks up .env values) ────────────────────────
from bot_logic import BotManager, check_honeypot
import notifier

bot_manager = BotManager(
    private_key=os.getenv("PRIVATE_KEY", ""),
    rpc_url=os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com"),
)

ENV_FILE = ".env"

# ── UI ────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

# ── Bot control ───────────────────────────────────────────────────────────────

@app.route("/api/start", methods=["POST"])
def start_sniper():
    try:
        d = request.json or {}
        token_address       = (d.get("token_address") or "").strip()
        buy_amount          = d.get("buy_amount")
        take_profit_percent = d.get("take_profit_percent")
        stop_loss_percent   = d.get("stop_loss_percent", 15)
        trailing_stop_pct   = d.get("trailing_stop_percent", 0)
        slippage            = d.get("slippage", 0.10)
        buy_pf              = int(d.get("buy_priority_fee",  100_000))
        sell_pf             = int(d.get("sell_priority_fee", 800_000))
        skip_safety         = bool(d.get("skip_safety", False))
        mode                = d.get("mode", None)

        if not token_address:
            return jsonify({"error": "token_address required"}), 400
        if not buy_amount or float(buy_amount) <= 0:
            return jsonify({"error": "buy_amount must be > 0"}), 400
        if not take_profit_percent or float(take_profit_percent) <= 0:
            return jsonify({"error": "take_profit_percent must be > 0"}), 400

        result = bot_manager.start_bot(
            token_address         = token_address,
            buy_amount_sol        = float(buy_amount),
            take_profit_percent   = float(take_profit_percent),
            stop_loss_percent     = float(stop_loss_percent),
            trailing_stop_percent = float(trailing_stop_pct),
            slippage              = float(slippage),
            buy_priority_fee      = buy_pf,
            sell_priority_fee     = sell_pf,
            skip_safety           = skip_safety,
            mode                  = mode,
        )
        return jsonify(result), (200 if result.get("success") else 400)
    except Exception as e:
        logger.exception("start_sniper")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def stop_position():
    try:
        d = request.json or {}
        token_address = d.get("token_address")
        result = bot_manager.stop_position(token_address) if token_address else bot_manager.stop_all()
        return jsonify(result)
    except Exception as e:
        logger.exception("stop_position")
        return jsonify({"error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def get_status():
    try:
        return jsonify(bot_manager.get_status())
    except Exception as e:
        logger.exception("get_status")
        return jsonify({"error": str(e)}), 500


@app.route("/api/position/<token_address>", methods=["GET"])
def get_position(token_address):
    try:
        return jsonify(bot_manager.get_position_status(token_address))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Mode ──────────────────────────────────────────────────────────────────────

@app.route("/api/mode", methods=["POST"])
def set_mode():
    try:
        d    = request.json or {}
        mode = d.get("mode", "semi")
        bot_manager.set_mode(mode)
        return jsonify({"success": True, "mode": mode})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Safety check ──────────────────────────────────────────────────────────────

@app.route("/api/check", methods=["POST"])
def safety_check():
    try:
        d = request.json or {}
        token_address = (d.get("token_address") or "").strip()
        if not token_address:
            return jsonify({"error": "token_address required"}), 400
        result = bot_manager.run_safety_check(token_address)
        return jsonify(result)
    except Exception as e:
        logger.exception("safety_check")
        return jsonify({"error": str(e)}), 500


# ── Scanner ───────────────────────────────────────────────────────────────────

@app.route("/api/scan", methods=["GET"])
def get_scan_results():
    try:
        results = bot_manager.get_scan_results()
        return jsonify({"results": results, "count": len(results)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan/start", methods=["POST"])
def start_scanner():
    try:
        bot_manager._ensure_scanner()
        return jsonify({"success": True, "message": "Scanner started"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Config save/load ──────────────────────────────────────────────────────────

@app.route("/api/config/get", methods=["GET"])
def get_config():
    """Return config status — never returns actual secret values."""
    current = dotenv_values(ENV_FILE)
    env_merged = {**current, **{k: os.getenv(k, "") for k in current}}

    def masked(key):
        val = os.getenv(key, current.get(key, ""))
        if not val:
            return {"set": False, "preview": ""}
        return {"set": True, "preview": val[:4] + "…" + val[-2:] if len(val) > 6 else "***"}

    return jsonify({
        "PRIVATE_KEY":         masked("PRIVATE_KEY"),
        "RPC_URL":             {"set": bool(os.getenv("RPC_URL")),
                                "preview": os.getenv("RPC_URL", "")},
        "JITO_BLOCK_ENGINE_URL": {"set": bool(os.getenv("JITO_BLOCK_ENGINE_URL")),
                                  "preview": os.getenv("JITO_BLOCK_ENGINE_URL", "")},
        "TELEGRAM_BOT_TOKEN":  masked("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_CHAT_ID":    {"set": bool(os.getenv("TELEGRAM_CHAT_ID")),
                                "preview": os.getenv("TELEGRAM_CHAT_ID", "")},
        "AUTO_BUY_SOL":        {"set": True, "preview": os.getenv("AUTO_BUY_SOL", "0.05")},
        "AUTO_TP":             {"set": True, "preview": os.getenv("AUTO_TP", "75")},
        "AUTO_SL":             {"set": True, "preview": os.getenv("AUTO_SL", "15")},
        "AUTO_TS":             {"set": True, "preview": os.getenv("AUTO_TS", "20")},
    })


@app.route("/api/config/save", methods=["POST"])
def save_config():
    """
    Save config to .env file (server-side only — never exposed to frontend).
    Sensitive values are written to .env and never returned to the client.
    """
    try:
        d = request.json or {}

        allowed_keys = [
            "PRIVATE_KEY", "RPC_URL", "JITO_BLOCK_ENGINE_URL",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
            "AUTO_BUY_SOL", "AUTO_TP", "AUTO_SL", "AUTO_TS",
        ]

        saved = []
        for key in allowed_keys:
            val = d.get(key, "").strip()
            if val:
                set_key(ENV_FILE, key, val)
                os.environ[key] = val          # apply immediately
                saved.append(key)

        # Reload bot credentials if they changed
        if "PRIVATE_KEY" in saved or "RPC_URL" in saved:
            bot_manager.reload_config(
                private_key=os.getenv("PRIVATE_KEY", ""),
                rpc_url=os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com"),
            )

        return jsonify({"success": True, "saved": saved})
    except Exception as e:
        logger.exception("save_config")
        return jsonify({"error": str(e)}), 500


# ── Telegram test ─────────────────────────────────────────────────────────────

@app.route("/api/notify/test", methods=["POST"])
def test_notification():
    try:
        result = notifier.test_notification()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status":            "ok",
        "active_positions":  len(bot_manager.positions),
        "mode":              bot_manager.mode.value,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
