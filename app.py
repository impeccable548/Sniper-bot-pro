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
        print("=" * 60)
        print("🔥 API START CALLED")
        print("=" * 60)
        
        data = request.json
        token_address = data.get('token_address')
        buy_amount = data.get('buy_amount')
        take_profit_percent = data.get('take_profit_percent')
        
        print(f"📊 Received: token={token_address}, amount={buy_amount}, tp={take_profit_percent}")
        
        if not token_address or not buy_amount or not take_profit_percent:
            return jsonify({"error": "Missing required fields"}), 400
        
        # Validate inputs
        if buy_amount <= 0:
            return jsonify({"error": "Buy amount must be positive"}), 400
        
        if take_profit_percent <= 0:
            return jsonify({"error": "Take profit must be positive"}), 400
        
        print(f"🚀 Calling bot_manager.start_bot()...")
        
        # Start the bot with full error catching
        try:
            result = bot_manager.start_bot(
                token_address=token_address,
                buy_amount_sol=buy_amount,
                take_profit_percent=take_profit_percent
            )
            print(f"📊 Bot result: {result}")
        except Exception as bot_error:
            print(f"❌ Bot start failed with exception: {bot_error}")
            import traceback
            traceback.print_exc()
            return jsonify({
                "error": f"Bot start exception: {str(bot_error)}",
                "debug_info": traceback.format_exc()
            }), 500
        
        if result.get('success'):
            print(f"✅ Success! Returning response...")
            return jsonify({
                "success": True,
                "message": "Sniper bot started successfully!",
                "tx_signature": result.get('tx_signature'),
                "debug_info": result.get('debug_info', '')
            })
        else:
            error_msg = result.get('error', 'Unknown error')
            print(f"❌ Bot returned error: {error_msg}")
            return jsonify({
                "error": error_msg,
                "debug_info": result.get('debug_info', '')
            }), 400
            
    except Exception as e:
        error_msg = str(e)
        print(f"❌ CRITICAL API ERROR: {error_msg}")
        import traceback
        traceback.print_exc()
        
        # Make sure we ALWAYS return valid JSON
        return jsonify({
            "error": f"Critical error: {error_msg}",
            "traceback": traceback.format_exc()
        }), 500

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