# bot_logic.py - Sniper Pro Bot — Full Trading Engine
import json
import time
import threading
import os
import base64
import struct
import logging
import traceback
import functools
import sys
from datetime import datetime

import requests
import base58
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from pump_sdk import PumpFunSDK

# ── logging ───────────────────────────────────────────────────────────────────
print = functools.partial(print, flush=True)
try:
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

POSITIONS_FILE = 'positions.json'
MAX_LOG_ENTRIES = 200


# ── Safety checks ─────────────────────────────────────────────────────────────

def check_honeypot(token_address: str) -> dict:
    """
    Run multiple rug/honeypot checks via free public APIs.
    Returns {"safe": bool, "warnings": [str], "score": int (0-100, 100=safe)}
    """
    warnings = []
    score = 100

    # 1. RugCheck.xyz
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report/summary", timeout=8)
        if r.status_code == 200:
            data = r.json()
            risks = data.get("risks", [])
            for risk in risks:
                name  = risk.get("name", "")
                level = risk.get("level", "")
                score_val = risk.get("score", 0)
                if level in ("danger", "warn"):
                    warnings.append(f"[RugCheck] {name} ({level})")
                    score -= score_val if score_val else 20
    except Exception as e:
        logger.warning(f"RugCheck API error: {e}")

    # 2. Check token metadata via Pump.fun API
    try:
        r = requests.get(f"https://pump.fun/api/token/{token_address}", timeout=6)
        if r.status_code == 200:
            data = r.json()
            if not data.get("twitter") and not data.get("telegram") and not data.get("website"):
                warnings.append("No social links (twitter/telegram/website)")
                score -= 10
            dev_hold = data.get("creator_token_holdings_percent", 0)
            if dev_hold > 20:
                warnings.append(f"Dev holds {dev_hold:.1f}% of supply")
                score -= 15
    except Exception as e:
        logger.warning(f"Pump.fun metadata check error: {e}")

    # 3. Dexscreener liquidity check
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=8)
        if r.status_code == 200:
            data = r.json()
            pairs = data.get("pairs") or []
            if pairs:
                liq = pairs[0].get("liquidity", {}).get("usd", 0) or 0
                if liq < 500:
                    warnings.append(f"Very low liquidity: ${liq:.0f}")
                    score -= 20
    except Exception as e:
        logger.warning(f"DexScreener check error: {e}")

    score = max(0, min(100, score))
    safe  = score >= 40 and not any("danger" in w.lower() for w in warnings)
    return {"safe": safe, "warnings": warnings, "score": score}


def fetch_sol_price() -> float:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            timeout=8
        )
        if r.status_code == 200:
            return float(r.json()["solana"]["usd"])
    except Exception:
        pass
    try:
        r = requests.get("https://price.jup.ag/v4/price?ids=SOL", timeout=6)
        if r.status_code == 200:
            return float(r.json()["data"]["SOL"]["price"])
    except Exception:
        pass
    return 150.0


# ── Position data class ───────────────────────────────────────────────────────

class Position:
    def __init__(self, d: dict):
        self.__dict__.update(d)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# ── BotManager ────────────────────────────────────────────────────────────────

class BotManager:
    def __init__(self, private_key: str, rpc_url: str):
        self.private_key = private_key
        self.rpc_url     = rpc_url
        self.client      = Client(rpc_url)

        self.wallet = None
        if private_key:
            try:
                self.wallet = Keypair.from_bytes(base58.b58decode(private_key))
                logger.info(f"Wallet loaded: {self.wallet.pubkey()}")
            except Exception as e:
                logger.error(f"Failed to load wallet: {e}")

        self.pump_sdk = PumpFunSDK(rpc_url, private_key)

        # positions: {token_address: dict}
        self.positions: dict[str, dict] = {}
        self._lock = threading.Lock()

        # activity log visible in UI
        self.activity_log: list[dict] = []

        # monitoring
        self._monitor_thread = None
        self._running = False

        self._load_positions()
        self._ensure_monitor_running()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load_positions(self):
        if os.path.exists(POSITIONS_FILE):
            try:
                with open(POSITIONS_FILE) as f:
                    self.positions = json.load(f)
                logger.info(f"Loaded {len(self.positions)} position(s) from disk")
            except Exception as e:
                logger.error(f"Error loading positions: {e}")

    def _save_positions(self):
        try:
            with open(POSITIONS_FILE, 'w') as f:
                json.dump(self.positions, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving positions: {e}")

    # ── logging ──────────────────────────────────────────────────────────────

    def _log(self, message: str, level: str = "info"):
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": message,
            "level": level
        }
        self.activity_log.append(entry)
        if len(self.activity_log) > MAX_LOG_ENTRIES:
            self.activity_log.pop(0)
        getattr(logger, level, logger.info)(message)

    # ── price reading ─────────────────────────────────────────────────────────

    def get_token_price(self, bonding_curve: str):
        """Returns (price_in_sol, virtual_sol_reserves) or (0, 0) on failure."""
        try:
            resp = self.client.get_account_info(Pubkey.from_string(bonding_curve))
            if not resp.value or not resp.value.data:
                return 0, 0

            raw = resp.value.data
            if isinstance(raw, (list, tuple)):
                first = raw[0]
                data = base64.b64decode(first) if isinstance(first, str) else bytes(first)
            elif isinstance(raw, bytes):
                data = raw
            else:
                data = base64.b64decode(str(raw))

            if len(data) < 16:
                return 0, 0

            best = None
            for s in range(0, len(data) - 8, 8):
                for t in range(0, len(data) - 8, 8):
                    if s == t:
                        continue
                    try:
                        sr = struct.unpack('<Q', data[s:s+8])[0]
                        tr = struct.unpack('<Q', data[t:t+8])[0]
                        sv = sr / 1e9
                        tv = tr / 1e6
                        if 0.1 < sv < 500 and tv > 0:
                            p = sv / tv
                            if best is None or abs(sv - 30) < abs(best[1] - 30):
                                best = (p, sv)
                    except Exception:
                        continue

            return best if best else (0, 0)
        except Exception as e:
            logger.error(f"get_token_price error: {e}")
            return 0, 0

    def get_token_balance(self, token_mint: str) -> int:
        """Returns raw token balance (lamports / base units)."""
        try:
            resp = self.client.get_token_accounts_by_owner(
                self.wallet.pubkey(),
                {"mint": Pubkey.from_string(token_mint)}
            )
            if not resp.value:
                return 0
            for acct in resp.value:
                info = self.client.get_account_info(acct.pubkey)
                if info.value:
                    d = info.value.data
                    raw = base64.b64decode(d[0]) if isinstance(d, (list, tuple)) else d
                    return struct.unpack('<Q', raw[64:72])[0]
            return 0
        except Exception as e:
            logger.error(f"get_token_balance error: {e}")
            return 0

    def get_sol_balance(self) -> float:
        """Returns SOL balance of the wallet."""
        try:
            if not self.wallet:
                return 0.0
            resp = self.client.get_balance(self.wallet.pubkey())
            return resp.value / 1e9
        except Exception:
            return 0.0

    # ── safety ───────────────────────────────────────────────────────────────

    def run_safety_check(self, token_address: str) -> dict:
        self._log(f"Running safety check on {token_address[:8]}…")
        result = check_honeypot(token_address)
        if result["warnings"]:
            for w in result["warnings"]:
                self._log(f"⚠ {w}", "warning")
        self._log(f"Safety score: {result['score']}/100 {'✅ SAFE' if result['safe'] else '⛔ RISKY'}")
        return result

    # ── buy ───────────────────────────────────────────────────────────────────

    def start_bot(self, token_address: str, buy_amount_sol: float,
                  take_profit_percent: float, stop_loss_percent: float,
                  trailing_stop_percent: float = 0.0,
                  slippage: float = 0.25, priority_fee: float = 5_000_000,
                  skip_safety: bool = False):

        if not self.wallet:
            return {"success": False, "error": "Private key not configured in environment"}

        if token_address in self.positions:
            return {"success": False, "error": "Already tracking this token"}

        sol_bal = self.get_sol_balance()
        if sol_bal < buy_amount_sol + 0.01:
            return {"success": False, "error": f"Insufficient SOL. Have {sol_bal:.4f}, need {buy_amount_sol+0.01:.4f}"}

        # Safety check
        safety = {"safe": True, "warnings": [], "score": 100}
        if not skip_safety:
            safety = self.run_safety_check(token_address)
            if not safety["safe"]:
                return {
                    "success": False,
                    "error": "Safety check failed — token appears risky",
                    "safety": safety
                }

        # Derive bonding curve
        self.pump_sdk.slippage     = slippage
        self.pump_sdk.priority_fee = int(priority_fee)
        bonding_curve = self.pump_sdk.derive_bonding_curve(token_address)
        if not bonding_curve:
            return {"success": False, "error": "Could not derive bonding curve"}

        self._log(f"Bonding curve: {bonding_curve}")

        price_sol, curve_sol = self.get_token_price(bonding_curve)
        if price_sol == 0:
            return {"success": False, "error": "Could not read token price (not on bonding curve?)"}

        sol_usd         = fetch_sol_price()
        entry_price_usd = price_sol * sol_usd
        self._log(f"Entry price: ${entry_price_usd:.10f} | SOL: {price_sol:.10f}")

        self._log(f"Executing buy: {buy_amount_sol} SOL → {token_address[:8]}…")
        buy_result = self.pump_sdk.buy_token(token_address, bonding_curve, buy_amount_sol)
        if not buy_result.get("success"):
            err = buy_result.get("error", "Buy transaction failed")
            self._log(f"Buy failed: {err}", "error")
            return {"success": False, "error": err}

        tx_sig = buy_result.get("signature", "")
        self._log(f"✅ Buy confirmed: {tx_sig[:16]}…")

        # read actual on-chain balance
        time.sleep(3)
        raw_balance = self.get_token_balance(token_address)
        decimals    = 6
        tokens_recv = raw_balance / (10 ** decimals) if raw_balance > 0 else buy_amount_sol / price_sol

        tp_price = entry_price_usd * (1 + take_profit_percent / 100)
        sl_price = entry_price_usd * (1 - abs(stop_loss_percent) / 100)

        pos = {
            "token_address":      token_address,
            "bonding_curve":      bonding_curve,
            "buy_amount_sol":     buy_amount_sol,
            "entry_price_sol":    price_sol,
            "entry_price_usd":    entry_price_usd,
            "current_price_usd":  entry_price_usd,
            "position_size_raw":  raw_balance,
            "position_size":      tokens_recv,
            "take_profit_pct":    take_profit_percent,
            "stop_loss_pct":      abs(stop_loss_percent),
            "trailing_stop_pct":  trailing_stop_percent,
            "tp_target_usd":      tp_price,
            "sl_target_usd":      sl_price,
            "highest_price_usd":  entry_price_usd,
            "pnl_usd":            0.0,
            "pnl_percent":        0.0,
            "tp_progress":        0.0,
            "bonding_curve_sol":  curve_sol,
            "tx_signature":       tx_sig,
            "start_time":         datetime.now().isoformat(),
            "last_update":        datetime.now().isoformat(),
            "status":             "active",
            "safety_score":       safety.get("score", 100),
            "safety_warnings":    safety.get("warnings", []),
            "slippage":           slippage,
            "priority_fee":       int(priority_fee),
        }

        with self._lock:
            self.positions[token_address] = pos
            self._save_positions()

        self._ensure_monitor_running()
        self._log(f"🚀 Position opened: {tokens_recv:,.2f} tokens @ ${entry_price_usd:.10f}")

        return {
            "success": True,
            "tx_signature": tx_sig,
            "tokens_received": tokens_recv,
            "entry_price_usd": entry_price_usd,
            "tp_target_usd": tp_price,
            "sl_target_usd": sl_price,
            "safety": safety,
        }

    # ── sell ──────────────────────────────────────────────────────────────────

    def _execute_sell(self, token_address: str, reason: str):
        """Execute a sell for a position and clean it up."""
        with self._lock:
            pos = self.positions.get(token_address)
        if not pos:
            return

        self._log(f"Selling {token_address[:8]}… Reason: {reason}", "warning")

        raw = self.get_token_balance(token_address)
        if raw == 0:
            self._log("Balance is 0, skipping sell (already sold?)", "warning")
            with self._lock:
                self.positions.pop(token_address, None)
                self._save_positions()
            return

        bc = pos["bonding_curve"]
        sl = pos.get("slippage", 0.25)
        pf = pos.get("priority_fee", 5_000_000)
        self.pump_sdk.slippage     = sl
        self.pump_sdk.priority_fee = int(pf)

        result = self.pump_sdk.sell_token(token_address, bc, raw)

        if result.get("success"):
            sig = result.get("signature", "")
            self._log(f"✅ Sell confirmed [{reason}]: {sig[:16]}…")
        else:
            err = result.get("error", "unknown")
            self._log(f"⛔ Sell failed [{reason}]: {err}", "error")

        with self._lock:
            self.positions.pop(token_address, None)
            self._save_positions()

    def stop_position(self, token_address: str):
        """Manually stop and sell a specific position."""
        if token_address not in self.positions:
            return {"success": False, "error": "Position not found"}
        threading.Thread(target=self._execute_sell, args=(token_address, "Manual"), daemon=True).start()
        return {"success": True, "message": "Sell order submitted"}

    def stop_all(self):
        """Stop all active positions."""
        tokens = list(self.positions.keys())
        for t in tokens:
            self._execute_sell(t, "Manual stop-all")
        return {"success": True, "stopped": len(tokens)}

    # ── monitoring loop ───────────────────────────────────────────────────────

    def _ensure_monitor_running(self):
        if not self._monitor_thread or not self._monitor_thread.is_alive():
            self._running = True
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="monitor"
            )
            self._monitor_thread.start()

    def _monitor_loop(self):
        logger.info("Monitor thread started")
        sol_price_cache = {"price": 150.0, "ts": 0}

        while self._running:
            try:
                if not self.positions:
                    time.sleep(3)
                    continue

                # Refresh SOL price every 60 s
                if time.time() - sol_price_cache["ts"] > 60:
                    sol_price_cache["price"] = fetch_sol_price()
                    sol_price_cache["ts"]    = time.time()
                sol_usd = sol_price_cache["price"]

                to_sell = []

                with self._lock:
                    tokens = list(self.positions.keys())

                for token_address in tokens:
                    with self._lock:
                        pos = self.positions.get(token_address)
                    if not pos or pos.get("status") != "active":
                        continue

                    price_sol, curve_sol = self.get_token_price(pos["bonding_curve"])
                    if price_sol == 0:
                        continue

                    current_usd = price_sol * sol_usd
                    entry_usd   = pos["entry_price_usd"]
                    pnl_pct     = (current_usd - entry_usd) / entry_usd * 100
                    pnl_usd     = (current_usd - entry_usd) * pos["position_size"]

                    # Update trailing stop
                    highest = max(pos.get("highest_price_usd", entry_usd), current_usd)
                    tp_target = pos["tp_target_usd"]
                    sl_target = pos["sl_target_usd"]

                    trailing_pct = pos.get("trailing_stop_pct", 0)
                    if trailing_pct > 0 and current_usd > entry_usd:
                        # dynamic SL rises with price
                        trailing_sl = highest * (1 - trailing_pct / 100)
                        if trailing_sl > sl_target:
                            sl_target = trailing_sl
                            pos["sl_target_usd"] = sl_target
                            self._log(f"Trailing SL updated → ${sl_target:.10f}")

                    position_value = pos["position_size"] * current_usd
                    tp_progress = max(0, min(100, (pnl_pct / pos["take_profit_pct"]) * 100)) if pos["take_profit_pct"] > 0 else 0

                    with self._lock:
                        pos.update({
                            "current_price_usd":  current_usd,
                            "bonding_curve_sol":  curve_sol,
                            "pnl_usd":            pnl_usd,
                            "pnl_percent":        pnl_pct,
                            "tp_progress":        tp_progress,
                            "position_value_usd": position_value,
                            "highest_price_usd":  highest,
                            "sl_target_usd":      sl_target,
                            "last_update":        datetime.now().isoformat(),
                        })
                        self._save_positions()

                    logger.info(
                        f"[{token_address[:8]}] ${current_usd:.10f} | "
                        f"PnL {pnl_pct:+.2f}% | TP@{tp_target:.10f} SL@{sl_target:.10f}"
                    )

                    # Trigger TP
                    if current_usd >= tp_target:
                        self._log(f"🎯 TAKE PROFIT hit @ ${current_usd:.10f} (+{pnl_pct:.2f}%)")
                        to_sell.append((token_address, "Take Profit"))

                    # Trigger SL
                    elif current_usd <= sl_target:
                        self._log(f"🛑 STOP LOSS hit @ ${current_usd:.10f} ({pnl_pct:.2f}%)", "warning")
                        to_sell.append((token_address, "Stop Loss"))

                for token_address, reason in to_sell:
                    with self._lock:
                        if token_address in self.positions:
                            self.positions[token_address]["status"] = "selling"
                    self._execute_sell(token_address, reason)

                time.sleep(4)

            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)
                time.sleep(5)

        logger.info("Monitor thread stopped")

    # ── status ────────────────────────────────────────────────────────────────

    def is_active(self) -> bool:
        return bool(self.positions)

    def get_status(self) -> dict:
        with self._lock:
            positions_list = [dict(p) for p in self.positions.values()]

        sol_bal = self.get_sol_balance()
        total_pnl = sum(p.get("pnl_usd", 0) for p in positions_list)
        total_val = sum(p.get("position_value_usd", 0) for p in positions_list)

        return {
            "active":         bool(positions_list),
            "positions":      positions_list,
            "position_count": len(positions_list),
            "total_pnl_usd":  total_pnl,
            "total_value_usd":total_val,
            "sol_balance":    sol_bal,
            "wallet_address": str(self.wallet.pubkey()) if self.wallet else None,
            "activity_log":   self.activity_log[-50:],
        }

    def get_position_status(self, token_address: str) -> dict:
        with self._lock:
            pos = self.positions.get(token_address)
        if not pos:
            return {"error": "Position not found"}
        return dict(pos)
