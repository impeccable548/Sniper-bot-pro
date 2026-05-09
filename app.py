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
from ultra_mode import UltraMode

bot_manager = BotManager(
    private_key=os.getenv("PRIVATE_KEY", ""),
    rpc_url=os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com"),
)

ultra = UltraMode(
    pump_sdk            = bot_manager.pump_sdk,
    rpc_client          = bot_manager.client,
    get_token_price_fn  = bot_manager.get_token_price,
    get_token_balance_fn= bot_manager.get_token_balance,
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


# ── Arm / Confirm (Enhancement 5) ────────────────────────────────────────────

@app.route("/api/arm", methods=["POST"])
def arm_position():
    try:
        d = request.json or {}
        token_address = (d.get("token_address") or "").strip()
        if not token_address:
            return jsonify({"error": "token_address required"}), 400
        config = {
            "buy_amount_sol":      float(d.get("buy_amount", 0.05)),
            "take_profit_percent": float(d.get("take_profit_percent", 75)),
            "stop_loss_percent":   float(d.get("stop_loss_percent", 15)),
            "trailing_stop_percent":float(d.get("trailing_stop_percent", 0)),
            "slippage":            float(d.get("slippage", 0.10)),
            "buy_priority_fee":    int(d.get("buy_priority_fee", 100_000)),
            "sell_priority_fee":   int(d.get("sell_priority_fee", 800_000)),
        }
        result = bot_manager.arm_position(token_address, config)
        return jsonify(result), (200 if result.get("success") else 400)
    except Exception as e:
        logger.exception("arm_position")
        return jsonify({"error": str(e)}), 500


@app.route("/api/confirm", methods=["POST"])
def confirm_position():
    try:
        d = request.json or {}
        token_address = (d.get("token_address") or "").strip()
        if not token_address:
            return jsonify({"error": "token_address required"}), 400
        result = bot_manager.confirm_position(token_address)
        return jsonify(result), (200 if result.get("success") else 400)
    except Exception as e:
        logger.exception("confirm_position")
        return jsonify({"error": str(e)}), 500


# ── Analytics (Enhancement 9) ─────────────────────────────────────────────────

@app.route("/api/analytics/slippage", methods=["GET"])
def analytics_slippage():
    try:
        with bot_manager._lock:
            all_pos = list(bot_manager.positions.values())
        slippages = [
            p.get("actual_slippage_pct", 0.0) for p in all_pos
            if p.get("actual_slippage_pct") is not None
        ]
        if not slippages:
            return jsonify({"count": 0, "avg": 0, "min": 0, "max": 0, "positions": []})
        return jsonify({
            "count": len(slippages),
            "avg":   round(sum(slippages) / len(slippages), 2),
            "min":   round(min(slippages), 2),
            "max":   round(max(slippages), 2),
            "positions": [
                {
                    "token":    p.get("token_address", "")[:16],
                    "slippage": p.get("actual_slippage_pct", 0),
                    "expected": p.get("expected_tokens", 0),
                    "actual":   p.get("actual_tokens", 0),
                }
                for p in all_pos
            ],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Log filtering (Enhancement 11) ───────────────────────────────────────────

@app.route("/api/logs/filter", methods=["POST"])
def filter_logs():
    try:
        d          = request.json or {}
        event_type = d.get("event_type")   # e.g. "BUY", "SELL", "TP_HIT", "SL_HIT", "MEV"
        level      = d.get("level")        # "info" | "warning" | "error"
        token      = d.get("token")        # partial address match
        limit      = int(d.get("limit", 100))

        logs = bot_manager.activity_log[:]
        if event_type:
            logs = [l for l in logs if l.get("event_type") == event_type.upper()]
        if level:
            logs = [l for l in logs if l.get("level") == level.lower()]
        if token:
            logs = [l for l in logs if token.lower() in l.get("message", "").lower()]

        return jsonify({"count": len(logs), "logs": logs[-limit:]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Health / stuck positions (Enhancement 12) ─────────────────────────────────

@app.route("/api/health/positions", methods=["GET"])
def health_positions():
    try:
        from datetime import datetime as _dt
        now    = _dt.now()
        stuck  = []
        active = []

        with bot_manager._lock:
            positions = list(bot_manager.positions.values())

        for pos in positions:
            if pos.get("status") != "active":
                continue
            active.append(pos)
            try:
                dt    = _dt.fromisoformat(pos.get("last_update", ""))
                age_s = (now - dt).total_seconds()
                if age_s > 300:
                    stuck.append({
                        "token":       pos.get("token_address", "")[:16],
                        "age_minutes": round(age_s / 60, 1),
                        "pnl_pct":     pos.get("pnl_percent", 0),
                    })
            except Exception:
                continue

        if stuck:
            status = "CRITICAL" if len(stuck) >= len(active) else "DEGRADED"
        else:
            status = "HEALTHY"

        return jsonify({
            "status":         status,
            "active_count":   len(active),
            "stuck_count":    len(stuck),
            "stuck_positions":stuck,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Ultra Mode ────────────────────────────────────────────────────────────────

@app.route("/api/ultra/start", methods=["POST"])
def ultra_start():
    try:
        d        = request.json or {}
        sub_mode = d.get("sub_mode", "mint")
        config   = {
            "sol_amount":    d.get("sol_amount"),
            "tp_pct":        d.get("tp_pct"),
            "sl_pct":        d.get("sl_pct"),
            "ts_pct":        d.get("ts_pct"),
            "slippage":      d.get("slippage"),
            "jito_tip_sol":  d.get("jito_tip_sol"),
            "max_mc_usd":    d.get("max_mc_usd"),
            "min_whale_sol": d.get("min_whale_sol"),
            "max_pump_pct":  d.get("max_pump_pct"),
            "whale_wallets": d.get("whale_wallets", []),
        }
        result = ultra.start(sub_mode, {k: v for k, v in config.items() if v is not None})
        return jsonify(result), (200 if result.get("success") else 400)
    except Exception as e:
        logger.exception("ultra_start")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ultra/stop", methods=["POST"])
def ultra_stop():
    try:
        return jsonify(ultra.stop())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ultra/status", methods=["GET"])
def ultra_status():
    try:
        return jsonify(ultra.get_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ultra/watchlist", methods=["POST"])
def ultra_watchlist():
    try:
        d       = request.json or {}
        wallets = d.get("wallets", [])
        return jsonify(ultra.save_watchlist(wallets))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Ultra Whale scan / approve (Enhancement 8.5-8.7) ─────────────────────────

@app.route("/api/ultra/whale/scan", methods=["POST"])
def ultra_whale_scan():
    try:
        d              = request.json or {}
        wallet_address = (d.get("wallet_address") or "").strip()
        if not wallet_address:
            return jsonify({"error": "wallet_address required"}), 400
        tokens = ultra.scan_whale_tokens(wallet_address)
        return jsonify({
            "success": True,
            "wallet":  wallet_address[:16] + "…",
            "count":   len(tokens),
            "tokens":  tokens,
        })
    except Exception as e:
        logger.exception("ultra_whale_scan")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ultra/whale/approve", methods=["POST"])
def ultra_whale_approve():
    try:
        d       = request.json or {}
        mint    = (d.get("token_address") or "").strip()
        action  = d.get("action", "approve")   # "approve" | "revoke"
        if not mint:
            return jsonify({"error": "token_address required"}), 400
        if action == "revoke":
            ultra.whale_approved_tokens.discard(mint)
            return jsonify({"success": True, "action": "revoked", "mint": mint[:16]})
        ultra.whale_approved_tokens.add(mint)
        return jsonify({
            "success":       True,
            "action":        "approved",
            "mint":          mint[:16],
            "approved_count":len(ultra.whale_approved_tokens),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ultra/whale/approved", methods=["GET"])
def ultra_whale_approved():
    try:
        return jsonify({
            "count":  len(ultra.whale_approved_tokens),
            "tokens": list(ultra.whale_approved_tokens),
        })
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


# ── Enhancement 10: Graceful shutdown signal handlers ────────────────────────

def _graceful_exit(signum, frame):
    logger.info(f"Signal {signum} received — shutting down gracefully…")
    bot_manager.shutdown_gracefully()
    raise SystemExit(0)


if __name__ == "__main__":
    import signal, subprocess
    signal.signal(signal.SIGTERM, _graceful_exit)
    signal.signal(signal.SIGINT,  _graceful_exit)

    port = int(os.environ.get("PORT", 5000))
    # Free the port if something is still holding it
    try:
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
        import time as _t; _t.sleep(0.5)
    except Exception:
        pass
    app.run(host="0.0.0.0", port=port, debug=False)
