# app.py - Sniper Pro Bot — Flask API
from flask import Flask, render_template, request, jsonify
from bot_logic import BotManager, check_honeypot
import os
from dotenv import load_dotenv
import logging

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

bot_manager = BotManager(
    private_key=os.getenv('PRIVATE_KEY'),
    rpc_url=os.getenv('RPC_URL', 'https://api.mainnet-beta.solana.com')
)


@app.route('/')
def index():
    return render_template('index.html')


# ── Bot control ───────────────────────────────────────────────────────────────

@app.route('/api/start', methods=['POST'])
def start_sniper():
    try:
        data = request.json or {}
        token_address       = (data.get('token_address') or '').strip()
        buy_amount          = data.get('buy_amount')
        take_profit_percent = data.get('take_profit_percent')
        stop_loss_percent   = data.get('stop_loss_percent', 20)
        trailing_stop_pct   = data.get('trailing_stop_percent', 0)
        slippage            = data.get('slippage', 0.25)
        priority_fee        = data.get('priority_fee', 5_000_000)
        skip_safety         = bool(data.get('skip_safety', False))

        if not token_address:
            return jsonify({"error": "token_address is required"}), 400
        if not buy_amount or float(buy_amount) <= 0:
            return jsonify({"error": "buy_amount must be positive"}), 400
        if not take_profit_percent or float(take_profit_percent) <= 0:
            return jsonify({"error": "take_profit_percent must be positive"}), 400

        result = bot_manager.start_bot(
            token_address       = token_address,
            buy_amount_sol      = float(buy_amount),
            take_profit_percent = float(take_profit_percent),
            stop_loss_percent   = float(stop_loss_percent),
            trailing_stop_percent = float(trailing_stop_pct),
            slippage            = float(slippage),
            priority_fee        = float(priority_fee),
            skip_safety         = skip_safety,
        )
        code = 200 if result.get('success') else 400
        return jsonify(result), code

    except Exception as e:
        logger.exception("start_sniper error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/stop', methods=['POST'])
def stop_position():
    try:
        data = request.json or {}
        token_address = data.get('token_address')
        if token_address:
            result = bot_manager.stop_position(token_address)
        else:
            result = bot_manager.stop_all()
        return jsonify(result)
    except Exception as e:
        logger.exception("stop_position error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/status', methods=['GET'])
def get_status():
    try:
        return jsonify(bot_manager.get_status())
    except Exception as e:
        logger.exception("get_status error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/position/<token_address>', methods=['GET'])
def get_position(token_address):
    try:
        return jsonify(bot_manager.get_position_status(token_address))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Safety check ──────────────────────────────────────────────────────────────

@app.route('/api/check', methods=['POST'])
def safety_check():
    try:
        data = request.json or {}
        token_address = (data.get('token_address') or '').strip()
        if not token_address:
            return jsonify({"error": "token_address required"}), 400
        result = check_honeypot(token_address)
        return jsonify(result)
    except Exception as e:
        logger.exception("safety_check error")
        return jsonify({"error": str(e)}), 500


# ── Health ────────────────────────────────────────────────────────────────────

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "active_positions": len(bot_manager.positions)})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
