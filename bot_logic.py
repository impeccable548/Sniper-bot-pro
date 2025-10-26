# bot_logic.py - Sniper Pro Bot Trading Logic
import json
import time
import threading
import os
from datetime import datetime
import requests
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
import base58
import base64
import struct
from pump_sdk import PumpFunSDK

POSITION_FILE = 'position.json'
STOP_LOSS_PERCENT = -20

class BotManager:
    def __init__(self, private_key, rpc_url):
        self.private_key = private_key
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)
        self.active = False
        self.position = None
        self.monitoring_thread = None
        self.start_time = None
        
        # Initialize wallet
        if private_key:
            self.wallet = Keypair.from_bytes(base58.b58decode(private_key))
        else:
            self.wallet = None
        
        # Initialize Pump.fun SDK
        self.pump_sdk = PumpFunSDK(rpc_url, private_key)
        
        # Load existing position on startup
        self.load_position()
    
    def load_position(self):
        """Load saved position from file"""
        try:
            if os.path.exists(POSITION_FILE):
                with open(POSITION_FILE, 'r') as f:
                    self.position = json.load(f)
                    print(f"✅ Loaded existing position: {self.position['token_address'][:8]}...")
                    # Resume monitoring if position exists
                    if self.position and not self.active:
                        self.active = True
                        self.start_monitoring()
        except Exception as e:
            print(f"Error loading position: {e}")
    
    def save_position(self):
        """Save position to file"""
        try:
            if self.position:
                with open(POSITION_FILE, 'w') as f:
                    json.dump(self.position, f, indent=2)
        except Exception as e:
            print(f"Error saving position: {e}")
    
    def delete_position(self):
        """Delete saved position"""
        try:
            if os.path.exists(POSITION_FILE):
                os.remove(POSITION_FILE)
            self.position = None
        except Exception as e:
            print(f"Error deleting position: {e}")
    
    def get_sol_price(self):
        """Get SOL price in USD"""
        try:
            response = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
                timeout=10
            )
            if response.status_code == 200:
                return response.json()["solana"]["usd"]
        except:
            pass
        return 150  # Fallback
    
    def check_wallet_balance(self, token_mint):
        """Check wallet's token balance"""
        try:
            response = self.client.get_token_accounts_by_owner(
                self.wallet.pubkey(),
                {"mint": Pubkey.from_string(token_mint)}
            )
            
            if response.value:
                for account in response.value:
                    info = self.client.get_account_info(account.pubkey)
                    if info.value:
                        data = base64.b64decode(info.value.data[0])
                        amount = struct.unpack('<Q', data[64:72])[0]
                        decimals = data[44]
                        return amount / (10 ** decimals)
            return 0
        except Exception as e:
            print(f"Error checking balance: {e}")
            return 0
    
    def get_token_price(self, bonding_curve):
        """Get token price from bonding curve"""
        try:
            response = self.client.get_account_info(Pubkey.from_string(bonding_curve))
            if not response.value:
                print("❌ No response value from RPC")
                return 0, 0
            
            if not response.value.data:
                print("❌ No data in response")
                return 0, 0
            
            # Debug: Print data type
            data_raw = response.value.data
            print(f"🔍 Data type: {type(data_raw)}")
            
            # Handle different data formats
            data = None
            try:
                if hasattr(data_raw, '__iter__') and not isinstance(data_raw, (str, bytes)):
                    print("📦 Data is iterable (tuple/list)")
                    if len(data_raw) > 0:
                        first_element = data_raw[0]
                        if isinstance(first_element, str):
                            data = base64.b64decode(first_element)
                        elif isinstance(first_element, bytes):
                            data = first_element
                        else:
                            data = base64.b64decode(str(first_element))
                elif isinstance(data_raw, bytes):
                    print("📦 Data is already bytes")
                    data = data_raw
                elif isinstance(data_raw, str):
                    print("📦 Data is string")
                    data = base64.b64decode(data_raw)
                else:
                    print(f"📦 Unknown data format")
                    data = base64.b64decode(str(data_raw))
                
                if not data:
                    print("❌ Failed to convert data to bytes")
                    return 0, 0
                    
                print(f"✅ Data decoded: {len(data)} bytes")
                
                # Print first 100 bytes in hex for debugging
                hex_preview = data[:100].hex()
                print(f"🔍 First 100 bytes (hex): {hex_preview}")
                
            except Exception as decode_error:
                print(f"❌ Error decoding data: {decode_error}")
                return 0, 0
            
            # Parse bonding curve - try MANY different offset combinations
            if len(data) < 40:
                print(f"❌ Data too short: {len(data)} bytes")
                return 0, 0
            
            # Try multiple offset combinations
            offset_attempts = [
                # (sol_start, sol_end, token_start, token_end, sol_decimals, token_decimals)
                (16, 24, 8, 16, 9, 6),    # Original attempt
                (8, 16, 16, 24, 9, 6),    # Swapped
                (24, 32, 32, 40, 9, 6),   # Higher offsets
                (32, 40, 40, 48, 9, 6),   # Even higher
                (40, 48, 48, 56, 9, 6),   # Higher still
                (16, 24, 8, 16, 9, 9),    # Different decimals
                (8, 16, 16, 24, 9, 9),    # Swapped + different decimals
            ]
            
            for sol_start, sol_end, token_start, token_end, sol_dec, token_dec in offset_attempts:
                try:
                    if len(data) >= max(sol_end, token_end):
                        virtual_sol_reserves = struct.unpack('<Q', data[sol_start:sol_end])[0] / (10 ** sol_dec)
                        virtual_token_reserves = struct.unpack('<Q', data[token_start:token_end])[0] / (10 ** token_dec)
                        
                        print(f"🧪 Offsets [{sol_start}:{sol_end}] [{token_start}:{token_end}] decimals({sol_dec},{token_dec})")
                        print(f"   SOL: {virtual_sol_reserves:.6f}, Token: {virtual_token_reserves:.2f}")
                        
                        # Check if values make sense (SOL should be 0.1 to 100, tokens should be > 0)
                        if 0.01 < virtual_sol_reserves < 200 and virtual_token_reserves > 0:
                            price_in_sol = virtual_sol_reserves / virtual_token_reserves
                            print(f"✅ Valid values found! Price: {price_in_sol:.10f} SOL")
                            return price_in_sol, virtual_sol_reserves
                        else:
                            print(f"   ⚠️ Values don't look right, trying next offset...")
                except Exception as offset_error:
                    continue
            
            print("❌ All offset attempts failed - no valid data found")
            return 0, 0
            
        except Exception as e:
            print(f"❌ Fatal error getting price: {e}")
            import traceback
            traceback.print_exc()
            return 0, 0
    
    def format_number(self, num):
        """Format large numbers"""
        if num >= 1_000_000_000:
            return f"${num/1_000_000_000:.2f}B"
        elif num >= 1_000_000:
            return f"${num/1_000_000:.2f}M"
        elif num >= 1_000:
            return f"${num/1_000:.2f}K"
        return f"${num:.2f}"
    
    def monitoring_loop(self):
        """Background monitoring thread"""
        print("🔄 Monitoring thread started")
        
        while self.active:
            try:
                if not self.position:
                    time.sleep(5)
                    continue
                
                # Check if still holding tokens
                current_balance = self.check_wallet_balance(self.position['token_address'])
                
                # Detect manual sell
                if current_balance == 0 and self.position['position_size'] > 0:
                    print("⚠️ Manual sell detected! Stopping bot...")
                    self.position['manual_sell_detected'] = True
                    self.save_position()
                    self.stop_bot()
                    break
                
                # Get current price
                price_sol, curve_sol = self.get_token_price(self.position['bonding_curve'])
                
                if price_sol > 0:
                    sol_price_usd = self.get_sol_price()
                    current_price_usd = price_sol * sol_price_usd
                    
                    # Update position data
                    self.position['current_price_sol'] = price_sol
                    self.position['current_price_usd'] = current_price_usd
                    self.position['bonding_curve_sol'] = curve_sol
                    self.position['last_update'] = datetime.now().isoformat()
                    
                    # Calculate P&L
                    entry_price = self.position['entry_price_usd']
                    pnl_percent = ((current_price_usd - entry_price) / entry_price) * 100
                    self.position['pnl_percent'] = pnl_percent
                    
                    position_value = self.position['position_size'] * current_price_usd
                    self.position['position_value_usd'] = position_value
                    self.position['pnl_usd'] = position_value - (self.position['buy_amount_sol'] * sol_price_usd)
                    
                    # Calculate progress to TP
                    tp_target = self.position['take_profit_percent']
                    progress = min((pnl_percent / tp_target) * 100, 100) if tp_target > 0 else 0
                    self.position['tp_progress'] = max(progress, 0)
                    
                    # Save updated position
                    self.save_position()
                    
                    print(f"📊 Price: ${current_price_usd:.10f} | P&L: {pnl_percent:+.2f}% | Progress: {progress:.1f}%")
                
                time.sleep(5)  # Update every 5 seconds
                
            except Exception as e:
                print(f"Monitoring error: {e}")
                time.sleep(5)
        
        print("⛔ Monitoring thread stopped")
    
    def start_monitoring(self):
        """Start monitoring thread"""
        if not self.monitoring_thread or not self.monitoring_thread.is_alive():
            self.monitoring_thread = threading.Thread(target=self.monitoring_loop, daemon=True)
            self.monitoring_thread.start()
    
    def start_bot(self, token_address, buy_amount_sol, take_profit_percent):
        """Start the sniper bot"""
        debug_log = []
        try:
            if not self.wallet:
                return {"success": False, "error": "Private key not configured", "debug_info": "No wallet found"}
            
            if self.active:
                return {"success": False, "error": "Bot already running", "debug_info": "Bot is already active"}
            
            debug_log.append("🚀 Starting Sniper Pro Bot...")
            debug_log.append(f"📍 Token: {token_address}")
            debug_log.append(f"💰 Amount: {buy_amount_sol} SOL")
            debug_log.append(f"🎯 TP: +{take_profit_percent}%")
            
            print("\n".join(debug_log))
            
            # Derive bonding curve address
            bonding_curve = self.pump_sdk.derive_bonding_curve(token_address)
            if not bonding_curve:
                error = "Failed to derive bonding curve address"
                debug_log.append(f"❌ {error}")
                return {"success": False, "error": error, "debug_info": "\n".join(debug_log)}
            
            debug_log.append(f"📊 Bonding Curve: {bonding_curve}")
            print(f"📊 Bonding Curve: {bonding_curve}")
            
            # Get initial price
            price_sol, curve_sol = self.get_token_price(bonding_curve)
            debug_log.append(f"🔍 Price check: {price_sol} SOL, Curve SOL: {curve_sol}")
            
            if price_sol == 0:
                error = "Could not fetch token price - token may not be on bonding curve"
                debug_log.append(f"❌ {error}")
                return {"success": False, "error": error, "debug_info": "\n".join(debug_log)}
            
            sol_price_usd = self.get_sol_price()
            entry_price_usd = price_sol * sol_price_usd
            
            debug_log.append(f"💲 Entry Price: ${entry_price_usd:.10f} ({price_sol:.10f} SOL)")
            print(f"💲 Entry Price: ${entry_price_usd:.10f} ({price_sol:.10f} SOL)")
            
            # Execute buy
            debug_log.append("🔨 Executing buy transaction...")
            print("🔨 Executing buy transaction...")
            
            buy_result = self.pump_sdk.buy_token(
                token_mint=token_address,
                bonding_curve=bonding_curve,
                amount_sol=buy_amount_sol
            )
            
            if not buy_result.get('success'):
                error = buy_result.get('error', 'Buy failed')
                debug_log.append(f"❌ Buy failed: {error}")
                return {"success": False, "error": error, "debug_info": "\n".join(debug_log)}
            
            tx_signature = buy_result.get('signature')
            tokens_received = buy_result.get('tokens_received', buy_amount_sol / price_sol)
            
            debug_log.append(f"✅ Buy successful! TX: {tx_signature}")
            debug_log.append(f"💼 Received: {tokens_received:,.2f} tokens")
            print(f"✅ Buy successful! TX: {tx_signature}")
            print(f"💼 Received: {tokens_received:,.2f} tokens")
            
            # Calculate targets
            tp_price_usd = entry_price_usd * (1 + take_profit_percent / 100)
            sl_price_usd = entry_price_usd * (1 + STOP_LOSS_PERCENT / 100)
            
            # Create position
            self.position = {
                'token_address': token_address,
                'bonding_curve': bonding_curve,
                'buy_amount_sol': buy_amount_sol,
                'entry_price_sol': price_sol,
                'entry_price_usd': entry_price_usd,
                'current_price_sol': price_sol,
                'current_price_usd': entry_price_usd,
                'position_size': tokens_received,
                'position_value_usd': tokens_received * entry_price_usd,
                'take_profit_percent': take_profit_percent,
                'stop_loss_percent': STOP_LOSS_PERCENT,
                'tp_target_usd': tp_price_usd,
                'sl_target_usd': sl_price_usd,
                'pnl_usd': 0,
                'pnl_percent': 0,
                'tp_progress': 0,
                'bonding_curve_sol': curve_sol,
                'tx_signature': tx_signature,
                'start_time': datetime.now().isoformat(),
                'last_update': datetime.now().isoformat(),
                'manual_sell_detected': False
            }
            
            # Save position
            self.save_position()
            
            # Start monitoring
            self.active = True
            self.start_time = time.time()
            self.start_monitoring()
            
            return {
                "success": True,
                "tx_signature": tx_signature,
                "tokens_received": tokens_received,
                "entry_price": entry_price_usd,
                "debug_info": "\n".join(debug_log)
            }
            
        except Exception as e:
            error_msg = f"Error starting bot: {str(e)}"
            debug_log.append(f"❌ {error_msg}")
            print(f"❌ {error_msg}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": error_msg, "debug_info": "\n".join(debug_log)}
    
    def stop_bot(self):
        """Stop the bot"""
        print("⛔ Stopping bot...")
        self.active = False
        if self.monitoring_thread:
            self.monitoring_thread.join(timeout=2)
        # Keep position data for display
    
    def get_status(self):
        """Get current bot status"""
        if not self.position:
            return {
                "active": False,
                "entry_price": "$0.00000000",
                "current_price": "$0.00000000",
                "price_change_percent": 0,
                "pnl_usd": 0,
                "pnl_percent": 0,
                "position_size": 0,
                "position_value": 0,
                "tp_target": "$0.00000000",
                "sl_target": "$0.00000000",
                "tp_progress": 0,
                "bonding_curve_sol": 0,
                "market_cap": "$0",
                "status": "ready",
                "manual_sell_detected": False
            }
        
        # Calculate market cap
        sol_price = self.get_sol_price()
        market_cap = self.position.get('position_size', 0) * self.position.get('current_price_usd', 0) * 1000  # Rough estimate
        
        return {
            "active": self.active,
            "entry_price": f"${self.position.get('entry_price_usd', 0):.10f}",
            "current_price": f"${self.position.get('current_price_usd', 0):.10f}",
            "price_change_percent": self.position.get('pnl_percent', 0),
            "pnl_usd": self.position.get('pnl_usd', 0),
            "pnl_percent": self.position.get('pnl_percent', 0),
            "position_size": self.position.get('position_size', 0),
            "position_value": self.position.get('position_value_usd', 0),
            "tp_target": f"${self.position.get('tp_target_usd', 0):.10f}",
            "sl_target": f"${self.position.get('sl_target_usd', 0):.10f}",
            "tp_progress": self.position.get('tp_progress', 0),
            "bonding_curve_sol": self.position.get('bonding_curve_sol', 0),
            "market_cap": self.format_number(market_cap),
            "status": "active sniping" if self.active else "stopped",
            "manual_sell_detected": self.position.get('manual_sell_detected', False)
        }
    
    def is_active(self):
        """Check if bot is active"""
        return self.active