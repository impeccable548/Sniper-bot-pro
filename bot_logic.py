# bot_logic.py - Sniper Pro Bot — Full Engine (Demo / Semi-Auto / Full-Auto)
import json, time, threading, os, base64, struct, logging, sys, functools
from datetime import datetime
from enum import Enum

import requests, base58
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from pump_sdk import PumpFunSDK
from scanner import scan_pump_fun_new, scan_dexscreener_new_pairs, full_token_scan
import notifier

# ── Logging ───────────────────────────────────────────────────────────────────
print = functools.partial(print, flush=True)
try:
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

POSITIONS_FILE = "positions.json"
MAX_LOG        = 300


class Mode(str, Enum):
    DEMO      = "demo"       # Paper trading — no real transactions
    SEMI_AUTO = "semi"       # Manual buy trigger, auto TP/SL
    FULL_AUTO = "full"       # Fully autonomous scan + buy + sell


# ── Safety / honeypot check (external APIs) ───────────────────────────────────

def check_honeypot(token_address: str) -> dict:
    warnings, score = [], 100

    # 1. RugCheck
    try:
        r = requests.get(
            f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report/summary", timeout=8
        )
        if r.status_code == 200:
            for risk in r.json().get("risks", []):
                level = risk.get("level", "")
                if level in ("danger", "warn"):
                    warnings.append(f"[RugCheck] {risk.get('name','')} ({level})")
                    score -= risk.get("score", 20)
    except Exception:
        pass

    # 2. Pump.fun metadata
    try:
        r = requests.get(f"https://pump.fun/api/token/{token_address}", timeout=6)
        if r.status_code == 200:
            d = r.json()
            if not any([d.get("twitter"), d.get("telegram"), d.get("website")]):
                warnings.append("No social links")
                score -= 10
            dev_hold = d.get("creator_token_holdings_percent", 0)
            if dev_hold > 20:
                warnings.append(f"Dev holds {dev_hold:.1f}% of supply")
                score -= 15
    except Exception:
        pass

    # 3. DexScreener liquidity
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=8
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs") or []
            if pairs:
                liq = (pairs[0].get("liquidity") or {}).get("usd", 0) or 0
                if liq < 500:
                    warnings.append(f"Very low liquidity: ${liq:.0f}")
                    score -= 20
    except Exception:
        pass

    score = max(0, min(100, score))
    return {
        "safe":     score >= 40 and not any("danger" in w.lower() for w in warnings),
        "warnings": warnings,
        "score":    score,
    }


def fetch_sol_price() -> float:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"}, timeout=8,
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


# ── BotManager ────────────────────────────────────────────────────────────────

class BotManager:
    def __init__(self, private_key: str, rpc_url: str):
        self.private_key = private_key
        self.rpc_url     = rpc_url
        self.mode        = Mode.SEMI_AUTO
        self._reinit_client_and_sdk()

        self.positions:    dict[str, dict] = {}
        self.activity_log: list[dict]      = []
        # Scanner results (for full-auto and the scanner tab)
        self.scan_results: list[dict]      = []
        self._lock = threading.Lock()

        # Thread handles
        self._monitor_thread = None
        self._scanner_thread = None
        self._running        = False
        self._scan_running   = False

        self._load_positions()
        self._ensure_monitor()

    def _reinit_client_and_sdk(self):
        self.client  = Client(self.rpc_url)
        self.wallet  = None
        if self.private_key:
            try:
                self.wallet = Keypair.from_bytes(base58.b58decode(self.private_key))
                logger.info(f"Wallet: {self.wallet.pubkey()}")
            except Exception as e:
                logger.error(f"Wallet load failed: {e}")
        self.pump_sdk = PumpFunSDK(
            rpc_url=self.rpc_url,
            private_key=self.private_key,
            slippage=float(os.getenv("DEFAULT_SLIPPAGE", "0.10")),
            buy_priority_fee=int(os.getenv("BUY_PRIORITY_FEE", "100000")),
            sell_priority_fee=int(os.getenv("SELL_PRIORITY_FEE", "800000")),
            use_jito=True,
        )

    def reload_config(self, private_key: str = None, rpc_url: str = None):
        """Hot-reload credentials without restarting the process."""
        if private_key:
            self.private_key = private_key
        if rpc_url:
            self.rpc_url = rpc_url
        self._reinit_client_and_sdk()
        self._log("⚙️ Configuration reloaded")

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_positions(self):
        if os.path.exists(POSITIONS_FILE):
            try:
                with open(POSITIONS_FILE) as f:
                    self.positions = json.load(f)
                logger.info(f"Loaded {len(self.positions)} positions")
            except Exception as e:
                logger.error(f"Load positions: {e}")

    def _save_positions(self):
        try:
            with open(POSITIONS_FILE, "w") as f:
                json.dump(self.positions, f, indent=2)
        except Exception as e:
            logger.error(f"Save positions: {e}")

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, msg: str, level: str = "info"):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "message": msg, "level": level}
        self.activity_log.append(entry)
        if len(self.activity_log) > MAX_LOG:
            self.activity_log.pop(0)
        getattr(logger, level, logger.info)(msg)

    # ── Price reading ─────────────────────────────────────────────────────────

    def get_token_price(self, bonding_curve: str):
        try:
            resp = self.client.get_account_info(Pubkey.from_string(bonding_curve))
            if not resp.value or not resp.value.data:
                return 0, 0
            raw = resp.value.data
            if isinstance(raw, (list, tuple)):
                data = base64.b64decode(raw[0]) if isinstance(raw[0], str) else bytes(raw[0])
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
                        sr = struct.unpack("<Q", data[s:s+8])[0]
                        tr = struct.unpack("<Q", data[t:t+8])[0]
                        sv, tv = sr / 1e9, tr / 1e6
                        if 0.1 < sv < 500 and tv > 0:
                            p = sv / tv
                            if best is None or abs(sv - 30) < abs(best[1] - 30):
                                best = (p, sv)
                    except Exception:
                        continue
            return best if best else (0, 0)
        except Exception as e:
            logger.error(f"get_token_price: {e}")
            return 0, 0

    def get_token_balance(self, token_mint: str) -> int:
        try:
            resp = self.client.get_token_accounts_by_owner(
                self.wallet.pubkey(), {"mint": Pubkey.from_string(token_mint)}
            )
            if not resp.value:
                return 0
            for acct in resp.value:
                info = self.client.get_account_info(acct.pubkey)
                if info.value:
                    d = info.value.data
                    raw = base64.b64decode(d[0]) if isinstance(d, (list, tuple)) else d
                    return struct.unpack("<Q", raw[64:72])[0]
            return 0
        except Exception as e:
            logger.error(f"get_token_balance: {e}")
            return 0

    def get_sol_balance(self) -> float:
        try:
            if not self.wallet:
                return 0.0
            return self.client.get_balance(self.wallet.pubkey()).value / 1e9
        except Exception:
            return 0.0

    # ── Safety ────────────────────────────────────────────────────────────────

    def run_safety_check(self, token_address: str) -> dict:
        self._log(f"🔍 Safety check: {token_address[:8]}…")
        # External API checks
        ext = check_honeypot(token_address)
        # On-chain checks (mint authority, top holders)
        bc  = self.pump_sdk.derive_bonding_curve(token_address)
        chain = full_token_scan(self.client, token_address, bc)

        # Merge results
        warnings = ext["warnings"] + chain["warnings"]
        score    = min(ext["score"], chain["score"])
        safe     = score >= 40 and ext["safe"] and chain["safe"]

        for w in warnings:
            self._log(w, "warning")
        self._log(f"Safety score: {score}/100 {'✅' if safe else '⛔'}")
        return {
            "safe": safe, "warnings": warnings, "score": score,
            "mint_auth_disabled":   chain.get("mint_auth_disabled", False),
            "freeze_auth_disabled": chain.get("freeze_auth_disabled", False),
            "top10_pct":            chain.get("top10_pct", 0.0),
            "concentrated":         chain.get("concentrated", False),
        }

    # ── Mode management ───────────────────────────────────────────────────────

    def set_mode(self, mode_str: str):
        try:
            self.mode = Mode(mode_str)
            self._log(f"Mode set to: {self.mode.value.upper()}")
            if self.mode == Mode.FULL_AUTO:
                self._ensure_scanner()
            else:
                self._scan_running = False
        except ValueError:
            self._log(f"Unknown mode: {mode_str}", "error")

    # ── Buy ───────────────────────────────────────────────────────────────────

    def start_bot(self, token_address: str, buy_amount_sol: float,
                  take_profit_percent: float, stop_loss_percent: float,
                  trailing_stop_percent: float = 0.0,
                  slippage: float = 0.10,
                  buy_priority_fee: int  = 100_000,
                  sell_priority_fee: int = 800_000,
                  skip_safety: bool = False,
                  mode: str = None) -> dict:

        effective_mode = Mode(mode) if mode else self.mode

        if not self.wallet and effective_mode != Mode.DEMO:
            return {"success": False, "error": "Private key not configured"}

        if token_address in self.positions:
            return {"success": False, "error": "Already tracking this token"}

        # Balance check (skip in demo)
        if effective_mode != Mode.DEMO:
            sol_bal = self.get_sol_balance()
            if sol_bal < buy_amount_sol + 0.01:
                return {"success": False, "error": f"Insufficient SOL ({sol_bal:.4f} available)"}

        # Safety check
        safety = {"safe": True, "warnings": [], "score": 100}
        if not skip_safety:
            safety = self.run_safety_check(token_address)
            if not safety["safe"]:
                notifier.notify_safety_fail(token_address, safety["score"], safety["warnings"])
                return {"success": False, "error": "Safety check failed", "safety": safety}

        # Derive bonding curve
        self.pump_sdk.slippage          = slippage
        self.pump_sdk.buy_priority_fee  = buy_priority_fee
        self.pump_sdk.sell_priority_fee = sell_priority_fee
        bonding_curve = self.pump_sdk.derive_bonding_curve(token_address)
        if not bonding_curve:
            return {"success": False, "error": "Could not derive bonding curve"}

        price_sol, curve_sol = self.get_token_price(bonding_curve)
        if price_sol == 0:
            return {"success": False, "error": "Cannot read token price (not on bonding curve?)"}

        sol_usd         = fetch_sol_price()
        entry_price_usd = price_sol * sol_usd

        # ── Execute or simulate buy ──
        tx_sig = "DEMO-TX"
        tokens_recv = buy_amount_sol / price_sol

        if effective_mode != Mode.DEMO:
            self._log(f"💸 Executing buy: {buy_amount_sol} SOL → {token_address[:8]}…")
            buy_result = self.pump_sdk.buy_token(
                token_address, bonding_curve, buy_amount_sol, buy_priority_fee
            )
            if not buy_result.get("success"):
                err = buy_result.get("error", "Buy failed")
                self._log(f"Buy failed: {err}", "error")
                return {"success": False, "error": err}
            tx_sig = buy_result.get("signature", "")
            time.sleep(3)
            raw = self.get_token_balance(token_address)
            if raw > 0:
                tokens_recv = raw / 1e6
        else:
            self._log(f"👻 DEMO: Simulated buy {buy_amount_sol} SOL → {token_address[:8]}…")

        tp_price = entry_price_usd * (1 + take_profit_percent / 100)
        sl_price = entry_price_usd * (1 - abs(stop_loss_percent) / 100)

        pos = {
            "token_address":       token_address,
            "bonding_curve":       bonding_curve,
            "buy_amount_sol":      buy_amount_sol,
            "entry_price_sol":     price_sol,
            "entry_price_usd":     entry_price_usd,
            "current_price_usd":   entry_price_usd,
            "position_size":       tokens_recv,
            "position_size_raw":   int(tokens_recv * 1e6),
            "take_profit_pct":     take_profit_percent,
            "stop_loss_pct":       abs(stop_loss_percent),
            "trailing_stop_pct":   trailing_stop_percent,
            "tp_target_usd":       tp_price,
            "sl_target_usd":       sl_price,
            "highest_price_usd":   entry_price_usd,
            "pnl_usd":             0.0,
            "pnl_percent":         0.0,
            "tp_progress":         0.0,
            "position_value_usd":  tokens_recv * entry_price_usd,
            "bonding_curve_sol":   curve_sol,
            "tx_signature":        tx_sig,
            "start_time":          datetime.now().isoformat(),
            "last_update":         datetime.now().isoformat(),
            "status":              "active",
            "mode":                effective_mode.value,
            "safety_score":        safety.get("score", 100),
            "safety_warnings":     safety.get("warnings", []),
            "buy_priority_fee":    buy_priority_fee,
            "sell_priority_fee":   sell_priority_fee,
            "slippage":            slippage,
        }

        with self._lock:
            self.positions[token_address] = pos
            self._save_positions()

        self._ensure_monitor()
        self._log(f"{'👻 DEMO' if effective_mode==Mode.DEMO else '🚀'} Position opened: {tokens_recv:,.0f} tokens @ ${entry_price_usd:.10f}")
        notifier.notify_buy(token_address, buy_amount_sol, entry_price_usd,
                            take_profit_percent, abs(stop_loss_percent), tx_sig)

        return {
            "success": True, "tx_signature": tx_sig,
            "tokens_received": tokens_recv, "entry_price_usd": entry_price_usd,
            "tp_target_usd": tp_price, "sl_target_usd": sl_price,
            "safety": safety, "mode": effective_mode.value,
        }

    # ── Sell ─────────────────────────────────────────────────────────────────

    def _execute_sell(self, token_address: str, reason: str):
        with self._lock:
            pos = self.positions.get(token_address)
        if not pos:
            return

        is_demo = pos.get("mode") == Mode.DEMO.value
        self._log(f"{'👻 DEMO ' if is_demo else ''}Selling {token_address[:8]}… [{reason}]", "warning")

        pnl_pct = pos.get("pnl_percent", 0)
        pnl_usd = pos.get("pnl_usd", 0)
        exit_px = pos.get("current_price_usd", 0)
        tx_sig  = "DEMO-SELL"

        if not is_demo:
            raw = self.get_token_balance(token_address)
            if raw == 0:
                self._log("Balance is 0 — skipping sell", "warning")
                with self._lock:
                    self.positions.pop(token_address, None)
                    self._save_positions()
                return

            self.pump_sdk.sell_priority_fee = pos.get("sell_priority_fee", 800_000)
            self.pump_sdk.slippage          = pos.get("slippage", 0.10)
            result = self.pump_sdk.sell_token(
                token_address, pos["bonding_curve"], raw, close_account=True
            )
            if result.get("success"):
                tx_sig = result.get("signature", "")
                self._log(f"✅ Sell confirmed [{reason}]: {tx_sig[:16]}…")
            else:
                err = result.get("error", "unknown")
                self._log(f"⛔ Sell failed [{reason}]: {err}", "error")
        else:
            self._log(f"👻 DEMO sell executed @ ${exit_px:.10f}")

        notifier.notify_sell(token_address, reason, pnl_pct, pnl_usd, exit_px, tx_sig)

        with self._lock:
            self.positions.pop(token_address, None)
            self._save_positions()

    def stop_position(self, token_address: str) -> dict:
        if token_address not in self.positions:
            return {"success": False, "error": "Position not found"}
        threading.Thread(target=self._execute_sell, args=(token_address, "Manual"), daemon=True).start()
        return {"success": True, "message": "Sell submitted"}

    def stop_all(self) -> dict:
        tokens = list(self.positions.keys())
        for t in tokens:
            self._execute_sell(t, "Manual stop-all")
        return {"success": True, "stopped": len(tokens)}

    # ── Monitor loop ──────────────────────────────────────────────────────────

    def _ensure_monitor(self):
        if not self._monitor_thread or not self._monitor_thread.is_alive():
            self._running = True
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="monitor"
            )
            self._monitor_thread.start()

    def _monitor_loop(self):
        logger.info("Monitor started")
        sol_price_cache = {"price": 150.0, "ts": 0}

        while self._running:
            try:
                if not self.positions:
                    time.sleep(3)
                    continue

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
                    highest  = max(pos.get("highest_price_usd", entry_usd), current_usd)
                    sl_target = pos["sl_target_usd"]
                    tp_target = pos["tp_target_usd"]
                    trail_pct = pos.get("trailing_stop_pct", 0)

                    if trail_pct > 0 and current_usd > entry_usd:
                        new_sl = highest * (1 - trail_pct / 100)
                        if new_sl > sl_target:
                            sl_target = new_sl
                            self._log(f"Trailing SL → ${sl_target:.10f}")

                    pos_val  = pos["position_size"] * current_usd
                    tp_prog  = max(0, min(100, (pnl_pct / pos["take_profit_pct"]) * 100)) if pos["take_profit_pct"] > 0 else 0

                    with self._lock:
                        pos.update({
                            "current_price_usd":  current_usd,
                            "bonding_curve_sol":  curve_sol,
                            "pnl_usd":            pnl_usd,
                            "pnl_percent":        pnl_pct,
                            "tp_progress":        tp_prog,
                            "position_value_usd": pos_val,
                            "highest_price_usd":  highest,
                            "sl_target_usd":      sl_target,
                            "last_update":        datetime.now().isoformat(),
                        })
                        self._save_positions()

                    logger.info(f"[{token_address[:8]}] ${current_usd:.10f} | {pnl_pct:+.2f}%")

                    if current_usd >= tp_target:
                        self._log(f"🎯 TP hit @ ${current_usd:.10f} (+{pnl_pct:.2f}%)")
                        to_sell.append((token_address, "Take Profit"))
                    elif current_usd <= sl_target:
                        self._log(f"🛑 SL hit @ ${current_usd:.10f} ({pnl_pct:.2f}%)", "warning")
                        to_sell.append((token_address, "Stop Loss"))

                for addr, reason in to_sell:
                    with self._lock:
                        if addr in self.positions:
                            self.positions[addr]["status"] = "selling"
                    self._execute_sell(addr, reason)

                time.sleep(4)

            except Exception as e:
                logger.error(f"Monitor loop: {e}", exc_info=True)
                time.sleep(5)

        logger.info("Monitor stopped")

    # ── Auto-scanner loop (Full-Auto mode) ────────────────────────────────────

    def _ensure_scanner(self):
        if not self._scanner_thread or not self._scanner_thread.is_alive():
            self._scan_running = True
            self._scanner_thread = threading.Thread(
                target=self._scanner_loop, daemon=True, name="scanner"
            )
            self._scanner_thread.start()
            label = "Full-Auto" if self.mode == Mode.FULL_AUTO else "Scanner"
            self._log(f"🔍 {label} started — scanning real-time token launches")

    def _scanner_loop(self):
        """
        Real-time scanner: Pump.fun newest + DexScreener newest pairs.
        - Resets the seen-set every hour so tokens can be re-evaluated after launch
        - Runs for SEMI_AUTO (scan only) and FULL_AUTO (scan + auto-buy)
        - Skips all modes below SEMI_AUTO
        """
        logger.info("Scanner loop started")
        scanned_tokens: set = set()
        last_reset = time.time()

        while self._scan_running:
            try:
                if self.mode not in (Mode.FULL_AUTO, Mode.SEMI_AUTO):
                    time.sleep(10)
                    continue

                # Reset seen-set every 60 min so tokens don't expire permanently
                if time.time() - last_reset > 3600:
                    scanned_tokens.clear()
                    last_reset = time.time()
                    logger.info("Scanner: seen-set cleared (hourly reset)")

                # ── Fetch real-time tokens ──
                pump_tokens = scan_pump_fun_new(limit=50)   # Pump.fun newest (last 2h)
                dex_tokens  = scan_dexscreener_new_pairs(limit=30)  # DexScreener newest
                all_tokens  = pump_tokens + dex_tokens

                fresh = [
                    t for t in all_tokens
                    if t.get("token_address") and t["token_address"] not in scanned_tokens
                ]

                if not fresh:
                    self._log(f"🔍 Scanner: all {len(all_tokens)} tokens already seen — waiting…")
                    time.sleep(30)
                    continue

                logger.info(f"Scanner: {len(fresh)} fresh tokens to evaluate (from {len(all_tokens)} total)")

                new_results = []
                for token in fresh[:12]:   # max 12 per cycle to avoid RPC hammering
                    addr = token.get("token_address", "")
                    if not addr or len(addr) < 32:
                        continue
                    scanned_tokens.add(addr)

                    # On-chain safety checks
                    bc    = self.pump_sdk.derive_bonding_curve(addr)
                    chain = full_token_scan(self.client, addr, bc)

                    token.update({
                        "safety_score":    chain["score"],
                        "safety_warnings": chain["warnings"],
                        "mint_auth_ok":    chain.get("mint_auth_disabled", False),
                        "top10_pct":       chain.get("top10_pct", 0.0),
                        "passes_filters":  chain["safe"],
                        "bonding_curve":   bc,
                        "scan_ts":         datetime.now().strftime("%H:%M:%S"),
                    })
                    new_results.append(token)

                    age_str = f"{token.get('age_min',0):.0f}min ago" if token.get("age_min") else ""
                    self._log(
                        f"🔍 {token.get('symbol','?')} ({addr[:8]}…) | "
                        f"Score {chain['score']}/100 | {age_str} | "
                        f"{'✅ PASS' if chain['safe'] else '⛔ FAIL'}"
                    )

                    # Auto-buy in Full-Auto mode
                    if (self.mode == Mode.FULL_AUTO
                            and chain["safe"]
                            and addr not in self.positions
                            and self.wallet):

                        sol_bal     = self.get_sol_balance()
                        auto_amount = float(os.getenv("AUTO_BUY_SOL", "0.05"))

                        if sol_bal >= auto_amount + 0.01:
                            self._log(f"🤖 Full-Auto: Buying {addr[:8]}… (score {chain['score']}/100)")
                            self.start_bot(
                                token_address         = addr,
                                buy_amount_sol        = auto_amount,
                                take_profit_percent   = float(os.getenv("AUTO_TP", "75")),
                                stop_loss_percent     = float(os.getenv("AUTO_SL", "15")),
                                trailing_stop_percent = float(os.getenv("AUTO_TS", "20")),
                                slippage              = 0.10,
                                buy_priority_fee      = 100_000,
                                sell_priority_fee     = 800_000,
                                skip_safety           = True,
                            )

                with self._lock:
                    # Newest results at the top, cap at 200
                    combined = new_results + self.scan_results
                    # Deduplicate by address
                    seen_addr, deduped = set(), []
                    for r in combined:
                        a = r.get("token_address", "")
                        if a and a not in seen_addr:
                            seen_addr.add(a)
                            deduped.append(r)
                    self.scan_results = deduped[:200]

                time.sleep(20)   # scan every 20s

            except Exception as e:
                logger.error(f"Scanner loop: {e}", exc_info=True)
                time.sleep(30)

        logger.info("Scanner stopped")

    # ── Status ────────────────────────────────────────────────────────────────

    def is_active(self) -> bool:
        return bool(self.positions)

    def get_status(self) -> dict:
        with self._lock:
            positions_list = [dict(p) for p in self.positions.values()]

        total_pnl = sum(p.get("pnl_usd", 0) for p in positions_list)
        total_val = sum(p.get("position_value_usd", 0) for p in positions_list)

        return {
            "active":         bool(positions_list),
            "mode":           self.mode.value,
            "positions":      positions_list,
            "position_count": len(positions_list),
            "total_pnl_usd":  total_pnl,
            "total_value_usd":total_val,
            "sol_balance":    self.get_sol_balance(),
            "wallet_address": str(self.wallet.pubkey()) if self.wallet else None,
            "activity_log":   self.activity_log[-60:],
            "scan_results":   self.scan_results[:30],
        }

    def get_scan_results(self) -> list:
        with self._lock:
            return list(self.scan_results)

    def get_position_status(self, token_address: str) -> dict:
        with self._lock:
            pos = self.positions.get(token_address)
        return dict(pos) if pos else {"error": "Not found"}
