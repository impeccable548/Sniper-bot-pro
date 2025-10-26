# bot_logic.py - Sniper Pro Bot Trading Logic (Auto-Detect Bonding Curve)
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
import sys
import functools
import logging
import traceback

# Force all prints to flush immediately
print = functools.partial(print, flush=True)
try:
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    # Not all Python runtimes support reconfigure; ignore if unavailable
    pass

# Setup live logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

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
        try:
            if os.path.exists(POSITION_FILE):
                with open(POSITION_FILE, 'r') as f:
                    self.position = json.load(f)
                    logger.info(f"✅ Loaded existing position: {self.position['token_address'][:8]}...")
                    if self.position and not self.active:
                        self.active = True
                        self.start_monitoring()
        except Exception as e:
            logger.error(f"Error loading position: {e}")
            traceback.print_exc()

    def save_position(self):
        try:
            if self.position:
                with open(POSITION_FILE, 'w') as f:
                    json.dump(self.position, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving position: {e}")
            traceback.print_exc()

    def delete_position(self):
        try:
            if os.path.exists(POSITION_FILE):
                os.remove(POSITION_FILE)
            self.position = None
        except Exception as e:
            logger.error(f"Error deleting position: {e}")
            traceback.print_exc()

    def get_sol_price(self):
        try:
            response = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
                timeout=10
            )
            if response.status_code == 200:
                return response.json()["solana"]["usd"]
        except Exception as e:
            logger.warning(f"Error fetching SOL price: {e}")
        return 150  # Fallback

    def check_wallet_balance(self, token_mint):
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
            logger.error(f"Error checking balance: {e}")
            traceback.print_exc()
            return 0

    def get_token_price(self, bonding_curve):
        """
        Auto-detect token price from bonding curve account data.

        Scans the account data in 8-byte aligned chunks, tries multiple decimal pairings,
        and returns the first realistic (SOL in (0.01, 200) and token > 0) match.

        Returns: (price_in_sol: float, virtual_sol_reserves: float)
        """
        try:
            response = self.client.get_account_info(Pubkey.from_string(bonding_curve))
            if not response.value:
                logger.error("❌ No response value from RPC")
                return 0, 0
            if not response.value.data:
                logger.error("❌ No data in response")
                return 0, 0

            data_raw = response.value.data
            logger.debug(f"🔍 Raw data type: {type(data_raw)}")

            # Convert to raw bytes
            data = None
            try:
                if hasattr(data_raw, '__iter__') and not isinstance(data_raw, (str, bytes)):
                    # often returned as a tuple/list with base64 string as first element
                    first = data_raw[0]
                    if isinstance(first, str):
                        data = base64.b64decode(first)
                    elif isinstance(first, bytes):
                        data = first
                    else:
                        data = base64.b64decode(str(first))
                elif isinstance(data_raw, bytes):
                    data = data_raw
                elif isinstance(data_raw, str):
                    data = base64.b64decode(data_raw)
                else:
                    data = base64.b64decode(str(data_raw))

                if not data:
                    logger.error("❌ Failed to convert data to bytes")
                    return 0, 0

                logger.debug(f"✅ Data decoded: {len(data)} bytes")
                logger.debug(f"🔍 First 100 bytes (hex): {data[:100].hex()}")

            except Exception as decode_error:
                logger.error(f"❌ Error decoding data: {decode_error}")
                traceback.print_exc()
                return 0, 0

            if len(data) < 16:
                logger.error(f"❌ Data too short: {len(data)} bytes")
                return 0, 0

            # Candidate decimal pairs to try (sol_decimals, token_decimals).
            # Most bonding curves use sol ~ 9 decimals and tokens could be 6, 9, or other.
            decimal_candidates = [
                (9, 6),
                (9, 9),
                (9, 0),
                (0, 6),
                (0, 9)
            ]

            found_attempts = []

            # Scan aligned 8-byte windows - try every plausible sol/token pair
            # We'll step in 8-byte increments to respect 64-bit fields
            max_index = len(data) - 8
            for sol_start in range(0, max_index, 8):
                for token_start in range(0, max_index, 8):
                    if token_start == sol_start:
                        continue
                    # Avoid overlapping identical window checks if you want (not required)
                    try:
                        # Unpack raw 8-byte integers (unsigned little-endian)
                        sol_raw = struct.unpack('<Q', data[sol_start:sol_start+8])[0]
                        token_raw = struct.unpack('<Q', data[token_start:token_start+8])[0]
                    except Exception:
                        continue

                    for sol_dec, token_dec in decimal_candidates:
                        try:
                            virtual_sol_reserves = sol_raw / (10 ** sol_dec) if sol_dec >= 0 else float(sol_raw)
                            virtual_token_reserves = token_raw / (10 ** token_dec) if token_dec >= 0 else float(token_raw)

                            # Basic sanity checks
                            sol_ok = 0.01 < virtual_sol_reserves < 200
                            token_ok = virtual_token_reserves > 0

                            # Record attempt for debugging
                            attempt = {
                                "sol_window": (sol_start, sol_start+8),
                                "token_window": (token_start, token_start+8),
                                "sol_dec": sol_dec,
                                "token_dec": token_dec,
                                "sol_val": virtual_sol_reserves,
                                "token_val": virtual_token_reserves,
                                "sol_ok": sol_ok,
                                "token_ok": token_ok
                            }
                            found_attempts.append(attempt)

                            logger.debug(
                                f"🧪 Offsets SOL[{sol_start}:{sol_start+8}] TOKEN[{token_start}:{token_start+8}] "
                                f"decimals({sol_dec},{token_dec}) -> SOL={virtual_sol_reserves:.6f}, TOKEN={virtual_token_reserves:.2f}"
                            )

                            if sol_ok and token_ok:
                                price_in_sol = virtual_sol_reserves / virtual_token_reserves
                                logger.info(
                                    f"✅ Valid values found at SOL[{sol_start}:{sol_start+8}] TOKEN[{token_start}:{token_start+8}] "
                                    f"decimals({sol_dec},{token_dec}) -> price: {price_in_sol:.10f} SOL"
                                )
                                return price_in_sol, virtual_sol_reserves
                        except Exception as e:
                            # Skip any unpack/convert errors for this decimal pair
                            continue

            # If we reach here, no plausible pair found. Dump summary debug info (first N attempts)
            logger.error("❌ All scan attempts failed - no valid data found in bonding curve")
            # Log a short sample of attempts to avoid huge logs
            sample = found_attempts[:20]
            for a in sample:
                logger.debug(f"Attempt sample: sol_win={a['sol_window']} token_win={a['token_window']} "
                             f"decimals=({a['sol_dec']},{a['token_dec']}) sol={a['sol_val']:.6f} token={a['token_val']:.2f} "
                             f"valid={a['sol_ok'] and a['token_ok']}")
            return 0, 0

        except Exception as e:
            logger.error(f"❌ Fatal error getting price: {e}")
            traceback.print_exc()
            return 0, 0

    def format_number(self, num):
        if num >= 1_000_000_000:
            return f"${num/1_000_000_000:.2f}B"
        elif num >= 1_000_000:
            return f"${num/1_000_000:.2f}M"
        elif num >= 1_000:
            return f"${num/1_000:.2f}K"
        return f"${num:.2f}"

    def monitoring_loop(self):
        logger.info("🔄 Monitoring thread started")
        while self.active:
            try:
                if not self.position:
                    time.sleep(5)
                    continue

                current_balance = self.check_wallet_balance(self.position['token_address'])
                if current_balance == 0 and self.position['position_size'] > 0:
                    logger.warning("⚠️ Manual sell detected! Stopping bot...")
                    self.position['manual_sell_detected'] = True
                    self.save_position()
                    self.stop_bot()
                    break

                price_sol, curve_sol = self.get_token_price(self.position['bonding_curve'])
                if price_sol > 0:
                    sol_price_usd = self.get_sol_price()
                    current_price_usd = price_sol * sol_price_usd

                    self.position['current_price_sol'] = price_sol
                    self.position['current_price_usd'] = current_price_usd
                    self.position['bonding_curve_sol'] = curve_sol
                    self.position['last_update'] = datetime.now().isoformat()

                    entry_price = self.position['entry_price_usd']
                    pnl_percent = ((current_price_usd - entry_price) / entry_price) * 100
                    self.position['pnl_percent'] = pnl_percent

                    position_value = self.position['position_size'] * current_price_usd
                    self.position['position_value_usd'] = position_value
                    self.position['pnl_usd'] = position_value - (self.position['buy_amount_sol'] * sol_price_usd)

                    tp_target = self.position['take_profit_percent']
                    progress = min((pnl_percent / tp_target) * 100, 100) if tp_target > 0 else 0
                    self.position['tp_progress'] = max(progress, 0)

                    self.save_position()
                    logger.info(f"📊 Price: ${current_price_usd:.10f} | P&L: {pnl_percent:+.2f}% | Progress: {progress:.1f}%")

                time.sleep(5)

            except Exception as e:
                logger.error(f"Monitoring error: {e}")
                traceback.print_exc()
                time.sleep(5)

        logger.info("⛔ Monitoring thread stopped")

    def start_monitoring(self):
        if not self.monitoring_thread or not self.monitoring_thread.is_alive():
            self.monitoring_thread = threading.Thread(target=self.monitoring_loop, daemon=True)
            self.monitoring_thread.start()

    def start_bot(self, token_address, buy_amount_sol, take_profit_percent):
        """Start the sniper bot with full live logging"""
        debug_log = []
        try:
            if not self.wallet:
                error_msg = "Private key not configured"
                logger.error(f"❌ {error_msg}")
                return {"success": False, "error": error_msg, "debug_info": "No wallet found"}

            if self.active:
                error_msg = "Bot already running"
                logger.warning(f"❌ {error_msg}")
                return {"success": False, "error": error_msg, "debug_info": "Bot is already active"}

            logger.info("🚀 Starting Sniper Pro Bot...")
            logger.info(f"📍 Token: {token_address}")
            logger.info(f"💰 Amount: {buy_amount_sol} SOL")
            logger.info(f"🎯 TP: +{take_profit_percent}%")

            bonding_curve = self.pump_sdk.derive_bonding_curve(token_address)
            if not bonding_curve:
                error_msg = "Failed to derive bonding curve address"
                logger.error(f"❌ {error_msg}")
                return {"success": False, "error": error_msg, "debug_info": "\n".join(debug_log)}

            logger.info(f"📊 Bonding Curve: {bonding_curve}")

            price_sol, curve_sol = self.get_token_price(bonding_curve)
            logger.info(f"🔍 Price check: {price_sol} SOL, Curve SOL: {curve_sol}")

            if price_sol == 0:
                error_msg = "Could not fetch token price - token may not be on bonding curve"
                logger.error(f"❌ {error_msg}")
                return {"success": False, "error": error_msg, "debug_info": "\n".join(debug_log)}

            sol_price_usd = self.get_sol_price()
            entry_price_usd = price_sol * sol_price_usd
            logger.info(f"💲 Entry Price: ${entry_price_usd:.10f} ({price_sol:.10f} SOL)")

            logger.info("🔨 Executing buy transaction...")
            buy_result = self.pump_sdk.buy_token(
                token_mint=token_address,
                bonding_curve=bonding_curve,
                amount_sol=buy_amount_sol
            )

            if not buy_result.get('success'):
                error = buy_result.get('error', 'Buy failed')
                logger.error(f"❌ Buy failed: {error}")
                return {"success": False, "error": error, "debug_info": "\n".join(debug_log)}

            tx_signature = buy_result.get('signature')
            tokens_received = buy_result.get('tokens_received', buy_amount_sol / price_sol)
            logger.info(f"✅ Buy successful! TX: {tx_signature}")
            logger.info(f"💼 Received: {tokens_received:,.2f} tokens")

            tp_price_usd = entry_price_usd * (1 + take_profit_percent / 100)
            sl_price_usd = entry_price_usd * (1 + STOP_LOSS_PERCENT / 100)

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

            self.save_position()
            self.active = True
            self.start_time = time.time()
            self.start_monitoring()
            logger.info("🚀 Sniper Pro Bot is now ACTIVE!")

            return {
                "success": True,
                "tx_signature": tx_signature,
                "tokens_received": tokens_received,
                "entry_price": entry_price_usd,
                "debug_info": "\n".join(debug_log)
            }

        except Exception as e:
            error_msg = f"Error starting bot: {str(e)}"
            logger.error(f"❌ {error_msg}")
            traceback.print_exc()
            return {"success": False, "error": error_msg, "debug_info": "\n".join(debug_log)}

    def stop_bot(self):
        logger.info("⛔ Stopping bot...")
        self.active = False
        if self.monitoring_thread:
            self.monitoring_thread.join(timeout=2)

    def get_status(self):
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

        sol_price = self.get_sol_price()
        market_cap = self.position.get('position_size', 0) * self.position.get('current_price_usd', 0) * 1000

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