# bot_logic.py - Sniper Pro Bot Trading Logic (Enhanced Debugging)
import sys
import functools
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
import traceback
from pump_sdk import PumpFunSDK
import logging

# -------------------- CONFIG --------------------
POSITION_FILE = 'position.json'
STOP_LOSS_PERCENT = -20
DEBUG_FLUSH = True  # Ensure all prints appear live

# -------------------- PRINT & LOGGING --------------------
# Force print to flush immediately
if DEBUG_FLUSH:
    print = functools.partial(print, flush=True)
    sys.stderr.reconfigure(line_buffering=True)

# Setup logger
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# -------------------- BOT CLASS --------------------
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

    # -------------------- POSITION MANAGEMENT --------------------
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

    # -------------------- PRICE & BALANCE --------------------
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
            logger.warning(f"Failed fetching SOL price: {e}")
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

    # -------------------- TOKEN PRICE --------------------
    def get_token_price(self, bonding_curve):
        try:
            response = self.client.get_account_info(Pubkey.from_string(bonding_curve))
            if not response.value or not response.value.data:
                logger.warning("❌ No response data from RPC")
                return 0, 0

            data_raw = response.value.data
            logger.debug(f"🔍 Data type: {type(data_raw)}")

            # Convert to bytes
            data = None
            try:
                if hasattr(data_raw, '__iter__') and not isinstance(data_raw, (str, bytes)):
                    first_element = data_raw[0]
                    if isinstance(first_element, str):
                        data = base64.b64decode(first_element)
                    elif isinstance(first_element, bytes):
                        data = first_element
                    else:
                        data = base64.b64decode(str(first_element))
                elif isinstance(data_raw, bytes):
                    data = data_raw
                elif isinstance(data_raw, str):
                    data = base64.b64decode(data_raw)
                else:
                    data = base64.b64decode(str(data_raw))

                if not data:
                    logger.warning("❌ Failed to convert data to bytes")
                    return 0, 0

                logger.debug(f"✅ Data decoded: {len(data)} bytes")
                logger.debug(f"🔍 First 100 bytes (hex): {data[:100].hex()}")
            except Exception as decode_error:
                logger.error(f"❌ Error decoding data: {decode_error}")
                traceback.print_exc()
                return 0, 0

            if len(data) < 40:
                logger.warning(f"❌ Data too short: {len(data)} bytes")
                return 0, 0

            # Try multiple offset combinations
            offset_attempts = [
                (16, 24, 8, 16, 9, 6),
                (8, 16, 16, 24, 9, 6),
                (24, 32, 32, 40, 9, 6),
                (32, 40, 40, 48, 9, 6),
                (40, 48, 48, 56, 9, 6),
                (16, 24, 8, 16, 9, 9),
                (8, 16, 16, 24, 9, 9),
            ]

            for sol_start, sol_end, token_start, token_end, sol_dec, token_dec in offset_attempts:
                try:
                    if len(data) >= max(sol_end, token_end):
                        virtual_sol_reserves = struct.unpack('<Q', data[sol_start:sol_end])[0] / (10 ** sol_dec)
                        virtual_token_reserves = struct.unpack('<Q', data[token_start:token_end])[0] / (10 ** token_dec)

                        logger.debug(f"🧪 Offsets [{sol_start}:{sol_end}] [{token_start}:{token_end}] decimals({sol_dec},{token_dec})")
                        logger.debug(f"   SOL: {virtual_sol_reserves:.6f}, Token: {virtual_token_reserves:.2f}")

                        if 0.01 < virtual_sol_reserves < 200 and virtual_token_reserves > 0:
                            price_in_sol = virtual_sol_reserves / virtual_token_reserves
                            logger.debug(f"✅ Valid values found! Price: {price_in_sol:.10f} SOL")
                            return price_in_sol, virtual_sol_reserves
                        else:
                            logger.debug(f"   ⚠️ Values don't look right, trying next offset...")
                except Exception as offset_error:
                    continue

            logger.warning("❌ All offset attempts failed - no valid data found")
            return 0, 0

        except Exception as e:
            logger.error(f"❌ Fatal error getting price: {e}")
            traceback.print_exc()
            return 0, 0

    # -------------------- OTHER FUNCTIONS --------------------
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

                    self.position.update({
                        'current_price_sol': price_sol,
                        'current_price_usd': current_price_usd,
                        'bonding_curve_sol': curve_sol,
                        'last_update': datetime.now().isoformat()
                    })

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

    # -------------------- BOT CONTROL --------------------
    # The rest of start_bot, stop_bot, get_status, is_active remain functionally the same
    # Just replace all print(...) calls with logger.info/debug as appropriate