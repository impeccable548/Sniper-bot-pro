# app.py - Sniper Pro Bot Main Flask Application
from flask import Flask, render_template, request, jsonify
from bot_logic import BotManager
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Initialize bot manager
bot_manager = BotManager(
    private_key=os.getenv('PRIVATE_KEY'),
    rpc_url=os.getenv('RPC_URL', 'https://api.mainnet-beta.solana.com')
)

@app.route('/')
def index():
    """Serve the main UI"""
    return render_template('index.html')

@app.route('/api/start', methods=['POST'])
def start_sniper():
    """Start the sniper bot"""
    try:
        data = request.json
        token_address = data.get('token_address')
        buy_amount = data.get('buy_amount')
        take_profit_percent = data.get('take_profit_percent')
        
        if not token_address or not buy_amount or not take_profit_percent:
            return jsonify({"error": "Missing required fields"}), 400
        
        # Validate inputs
        if buy_amount <= 0:
            return jsonify({"error": "Buy amount must be positive"}), 400
        
        if take_profit_percent <= 0:
            return jsonify({"error": "Take profit must be positive"}), 400
        
        # Start the bot
        result = bot_manager.start_bot(
            token_address=token_address,
            buy_amount_sol=buy_amount,
            take_profit_percent=take_profit_percent
        )
        
        if result.get('success'):
            return jsonify({
                "success": True,
                "message": "Sniper bot started successfully!",
                "tx_signature": result.get('tx_signature')
            })
        else:
            return jsonify({"error": result.get('error', 'Unknown error')}), 400
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/status', methods=['GET'])
def get_status():
    """Get current bot status"""
    try:
        status = bot_manager.get_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/stop', methods=['POST'])
def stop_bot():
    """Stop the bot"""
    try:
        bot_manager.stop_bot()
        return jsonify({
            "success": True,
            "message": "Bot stopped successfully"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint for Render"""
    return jsonify({
        "status": "healthy",
        "bot_active": bot_manager.is_active()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)