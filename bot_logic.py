# bot_logic.py - Sniper Pro Bot — Full Engine (Demo / Semi-Auto / Full-Auto)
import json, time, threading, os, base64, struct, logging, sys, functools, math, random
from datetime import datetime
from enum import Enum

import requests, base58
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from pump_sdk import PumpFunSDK, detect_sandwich_attack
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
    DEMO      = "demo"
    SEMI_AUTO = "semi"
    FULL_AUTO = "full"


# ── External safety / honeypot check ─────────────────────────────────────────

def check_honeypot(token_address: str) -> dict:
    warnings, score = [], 100

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


# ── Enhancement 1: Jupiter price cross-validation ────────────────────────────

def validate_price_with_jupiter(mint: str, bonding_curve_price: float) -> tuple[bool, float]:
    """
    Cross-check bonding-curve parsed price against Jupiter aggregator.
    If divergence >15%, use Jupiter price as the authoritative value.
    Returns (is_valid, final_price).
    """
    try:
        r = requests.get(
            "https://price.jup.ag/v4/price",
            params={"ids": mint}, timeout=5,
        )
        if r.status_code == 200:
            data      = r.json().get("data", {}).get(mint, {})
            jup_price = float(data.get("price", 0) or 0)
            if jup_price > 0 and bonding_curve_price > 0:
                divergence = abs(bonding_curve_price - jup_price) / jup_price
                if divergence > 0.15:
                    logger.warning(
                        f"Jupiter price divergence {divergence:.1%} — "
                        f"bonding curve: ${bonding_curve_price:.10f}, "
                        f"Jupiter: ${jup_price:.10f} — using Jupiter"
                    )
                    return False, jup_price
                return True, bonding_curve_price
    except Exception:
        pass
    return True, bonding_curve_price


# ── Enhancement 2: Dynamic slippage ──────────────────────────────────────────

def calculate_dynamic_slippage(market_cap_usd: float, age_min: float) -> float:
    """
    Adaptive slippage tiers based on market cap and token age.
    Micro-caps / brand-new tokens need more headroom.
    """
    if market_cap_usd < 5_000 and age_min < 30:
        return 0.25   # brand-new micro-cap
    if market_cap_usd < 50_000 and age_min < 120:
        return 0.15   # growing token
    return 0.08       # established — tight


# ── Enhancement 4: Position size scaling ─────────────────────────────────────

def calculate_buy_size(safety_score: int, requested_sol: float) -> float:
    """Scale position size by safety confidence score."""
    if safety_score >= 80:
        return requested_sol
    if safety_score >= 60:
        return round(requested_sol * 0.70, 4)
    if safety_score >= 40:
        return round(requested_sol * 0.30, 4)
    return 0.0   # below 40 — block


# ── Enhancement 6: Realistic demo simulation ─────────────────────────────────

def simulate_realistic_price_move(last_price: float, trend: str,
                                  entry_price: float) -> tuple[float, str]:
    """
    Log-normal price walk with trend states.
    Distribution: 60% neutral, 20% pump, 20% dump.
    Rare: 1% moon shot, 2% rug/crash.
    Returns (new_price, new_trend).
    """
    r = random.random()
    if r < 0.01:   # moon
        return min(last_price * random.uniform(1.5, 3.0), entry_price * 5.0), "pump"
    if r < 0.03:   # crash
        return max(last_price * random.uniform(0.10, 0.40), entry_price * 0.05), "dump"

    # Trend state transitions
    t = random.random()
    if trend == "neutral":
        if t < 0.20:   trend = "pump"
        elif t < 0.40: trend = "dump"
    elif trend == "pump":
        if t < 0.15:   trend = "neutral"
        elif t < 0.20: trend = "dump"
    else:  # dump
        if t < 0.15:   trend = "neutral"
        elif t < 0.20: trend = "pump"

    drift = {"pump": 0.04, "dump": -0.05, "neutral": 0.0}[trend]
    sigma = {"pump": 0.04, "dump": 0.04,  "neutral": 0.03}[trend]
    move  = math.exp(random.gauss(drift, sigma)) - 1
    return max(last_price * (1 + move), entry_price * 0.01), trend


# ── BotManager ────────────────────────────────────────────────────────────────

class BotManager:
    def __init__(self, private_key: str, rpc_url: str):
        self.private_key = private_key
        self.rpc_url     = rpc_url
        self.mode        = Mode.SEMI_AUTO
        self._reinit_client_and_sdk()

        self.positions:    dict[str, dict] = {}
        self.activity_log: list[dict]      = []
        self.scan_results: list[dict]      = []
        self._lock = threading.Lock()

        # Enhancement 5: Arm/confirm system
        self.pending_confirmations: dict = {}
        self._confirmation_lock          = threading.Lock()

        # Enhancement 12: Stuck position tracking
        self._last_stuck_check = 0.0

        # Enhancement 6: Demo price trend per position
        self._demo_trends: dict = {}

        self._monitor_thread = None
        self._scanner_thread = None
        self._running        = False
        self._scan_running   = False

        self._load_positions()
        self._ensure_monitor()

    def _reinit_client_and_sdk(self):
        self.client = Client(self.rpc_url)
        self.wallet = None
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

                # Enhancement 10: Orphan detection — real positions from previous session
                orphans = [
                    addr for addr, pos in self.positions.items()
                    if pos.get("status") == "active" and pos.get("mode") != "demo"
                ]
                if orphans:
                    logger.warning(f"⚠️ {len(orphans)} orphaned real position(s) resumed from last session")
                    for addr in orphans:
                        notifier.notify(
                            f"⚠️ <b>ORPHANED POSITION RESUMED</b>\n"
                            f"🪙 <code>{addr[:16]}…</code>\n"
                            f"Bot restarted — monitoring resumed automatically."
                        )
            except Exception as e:
                logger.error(f"Load positions: {e}")

    def _save_positions(self):
        try:
            with open(POSITIONS_FILE, "w") as f:
                json.dump(self.positions, f, indent=2)
        except Exception as e:
            logger.error(f"Save positions: {e}")

    # ── Logging (Enhancement 11: event_type) ─────────────────────────────────

    def _log(self, msg: str, level: str = "info", event_type: str = None):
        entry = {
            "time":       datetime.now().strftime("%H:%M:%S"),
            "message":    msg,
            "level":      level,
            "event_type": event_type,
        }
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
                    d   = info.value.data
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
        self._log(f"🔍 Safety check: {token_address[:8]}…", event_type="SAFETY_CHECK")
        ext   = check_honeypot(token_address)
        bc    = self.pump_sdk.derive_bonding_curve(token_address)
        chain = full_token_scan(self.client, token_address, bc)

        warnings = ext["warnings"] + chain["warnings"]
        score    = min(ext["score"], chain["score"])
        safe     = score >= 40 and ext["safe"] and chain["safe"]

        for w in warnings:
            self._log(w, "warning", "SAFETY_CHECK")
        self._log(f"Safety score: {score}/100 {'✅' if safe else '⛔'}", event_type="SAFETY_CHECK")
        return {
            "safe": safe, "warnings": warnings, "score": score,
            "mint_auth_disabled":   chain.get("mint_auth_disabled", False),
            "freeze_auth_disabled": chain.get("freeze_auth_disabled", False),
            "mint_authority":       chain.get("mint_authority"),
            "freeze_authority":     chain.get("freeze_authority"),
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

    # ── Enhancement 5: Arm / Confirm (2-step Semi-Auto) ───────────────────────

    def arm_position(self, token_address: str, config: dict) -> dict:
        """Run safety check and arm position. User has 30s to confirm."""
        if not self.wallet:
            return {"success": False, "error": "Private key not configured"}
        if token_address in self.positions:
            return {"success": False, "error": "Already tracking this token"}

        safety = self.run_safety_check(token_address)
        if not safety["safe"]:
            notifier.notify_safety_fail(token_address, safety["score"], safety["warnings"])
            return {"success": False, "error": "Safety check failed", "safety": safety}

        expires_at = time.time() + 30
        with self._confirmation_lock:
            self.pending_confirmations[token_address] = {
                "config":     config,
                "safety":     safety,
                "expires_at": expires_at,
                "armed_at":   datetime.now().isoformat(),
            }

        entry_est = 0.0
        try:
            bc = self.pump_sdk.derive_bonding_curve(token_address)
            p, _ = self.get_token_price(bc)
            entry_est = p * fetch_sol_price()
        except Exception:
            pass

        self._log(f"🔐 Position ARMED: {token_address[:8]}… — confirm within 30s", event_type="BUY")
        return {
            "success":    True,
            "status":     "ARMED — confirm within 30 seconds",
            "expires_at": expires_at,
            "safety":     safety,
            "entry_est":  entry_est,
        }

    def confirm_position(self, token_address: str) -> dict:
        """Execute buy for an armed position if still within 30s window."""
        with self._confirmation_lock:
            pending = self.pending_confirmations.get(token_address)

        if not pending:
            return {"success": False, "error": "No armed position for this token"}
        if time.time() > pending["expires_at"]:
            with self._confirmation_lock:
                self.pending_confirmations.pop(token_address, None)
            return {"success": False, "error": "Confirmation window expired (30s)"}

        with self._confirmation_lock:
            self.pending_confirmations.pop(token_address, None)

        cfg = pending["config"]
        return self.start_bot(
            token_address         = token_address,
            buy_amount_sol        = cfg.get("buy_amount_sol", 0.05),
            take_profit_percent   = cfg.get("take_profit_percent", 75),
            stop_loss_percent     = cfg.get("stop_loss_percent", 15),
            trailing_stop_percent = cfg.get("trailing_stop_percent", 0),
            slippage              = cfg.get("slippage", 0.10),
            buy_priority_fee      = cfg.get("buy_priority_fee", 100_000),
            sell_priority_fee     = cfg.get("sell_priority_fee", 800_000),
            skip_safety           = True,  # already checked during arm
        )

    # ── Enhancement 12: Stuck position alerts ────────────────────────────────

    def check_stuck_positions(self):
        """Alert via Telegram if any active position hasn't been updated in 5+ minutes."""
        now = datetime.now()
        for addr, pos in self.positions.items():
            if pos.get("status") != "active":
                continue
            try:
                last_upd = pos.get("last_update", "")
                dt       = datetime.fromisoformat(last_upd)
                age_s    = (now - dt).total_seconds()
                if age_s > 300:
                    self._log(
                        f"⚠️ STUCK: {addr[:8]}… not updated for {age_s/60:.1f}min",
                        "warning", "STUCK"
                    )
                    notifier.notify(
                        f"⚠️ <b>STUCK POSITION ALERT</b>\n"
                        f"🪙 <code>{addr[:16]}…</code>\n"
                        f"⏱ Last update: <b>{age_s/60:.1f} minutes ago</b>\n"
                        f"Monitor may be stalled — check the bot."
                    )
            except Exception:
                continue

    # ── Enhancement 10: Graceful shutdown ────────────────────────────────────

    def shutdown_gracefully(self):
        """Stop all threads and save state cleanly on SIGTERM/SIGINT."""
        self._log("🔄 Graceful shutdown initiated…")
        self._running      = False
        self._scan_running = False

        for t in [self._monitor_thread, self._scanner_thread]:
            if t and t.is_alive():
                t.join(timeout=10)

        self._save_positions()
        self._log("✅ Shutdown complete — positions saved")
        logger.info("BotManager shutdown complete")

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

        # ── Enhancement 4: Position size scaling ──
        score = safety.get("score", 100)
        if effective_mode != Mode.DEMO:
            scaled = calculate_buy_size(score, buy_amount_sol)
            if scaled == 0.0:
                return {"success": False, "error": f"Safety score {score}/100 too low — buy blocked by risk filter"}
            if scaled != buy_amount_sol:
                self._log(
                    f"📐 Position scaled: {buy_amount_sol} SOL → {scaled} SOL (score {score}/100)",
                    event_type="SCALING"
                )
                buy_amount_sol = scaled

        # ── Enhancement 2: Dynamic slippage ──
        if slippage == 0.10 and effective_mode != Mode.DEMO:
            mc_usd, age_min_val = 0.0, 0.0
            try:
                r = requests.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
                    timeout=5
                )
                if r.status_code == 200:
                    pairs = r.json().get("pairs") or []
                    if pairs:
                        mc_usd      = float(pairs[0].get("marketCap") or 0)
                        created_ms  = pairs[0].get("pairCreatedAt") or 0
                        age_min_val = (time.time() * 1000 - created_ms) / 60000 if created_ms else 0
            except Exception:
                pass
            dyn_slip = calculate_dynamic_slippage(mc_usd, age_min_val)
            if dyn_slip != slippage:
                self._log(
                    f"⚡ Dynamic slippage: {slippage*100:.0f}% → {dyn_slip*100:.0f}%"
                    f" (MC ${mc_usd:,.0f}, age {age_min_val:.0f}min)",
                    event_type="SLIPPAGE"
                )
                slippage = dyn_slip

        self.pump_sdk.slippage          = slippage
        self.pump_sdk.buy_priority_fee  = buy_priority_fee
        self.pump_sdk.sell_priority_fee = sell_priority_fee

        # ── Demo mode — simulate ──
        if effective_mode == Mode.DEMO:
            sol_usd         = fetch_sol_price()
            price_sol       = 0.000001
            entry_price_usd = price_sol * sol_usd
            bonding_curve   = "DEMO-CURVE"
            curve_sol       = 0.0
            tokens_recv     = buy_amount_sol / price_sol
            tx_sig          = "DEMO-TX"
            actual_slippage = 0.0
            self._log(f"👻 DEMO: Simulated buy {buy_amount_sol} SOL → {token_address[:8]}…", event_type="BUY")
        else:
            bonding_curve = self.pump_sdk.derive_bonding_curve(token_address)
            if not bonding_curve:
                return {"success": False, "error": "Could not derive bonding curve"}

            price_sol, curve_sol = self.get_token_price(bonding_curve)
            if price_sol == 0:
                return {"success": False, "error": "Cannot read token price (not on bonding curve?)"}

            # ── Enhancement 1: Jupiter price cross-validation ──
            is_valid, price_sol_final = validate_price_with_jupiter(token_address, price_sol)
            if not is_valid:
                self._log(
                    f"⚡ Price corrected by Jupiter: ${price_sol:.10f} → ${price_sol_final:.10f}",
                    event_type="PRICE_VALIDATION"
                )
                price_sol = price_sol_final

            sol_usd         = fetch_sol_price()
            entry_price_usd = price_sol * sol_usd

            self._log(f"💸 Executing buy: {buy_amount_sol} SOL → {token_address[:8]}…", event_type="BUY")
            buy_result = self.pump_sdk.buy_token(
                token_address, bonding_curve, buy_amount_sol, buy_priority_fee
            )
            if not buy_result.get("success"):
                err = buy_result.get("error", "Buy failed")
                self._log(f"Buy failed: {err}", "error", "BUY")
                return {"success": False, "error": err}
            tx_sig = buy_result.get("signature", "")
            time.sleep(3)
            raw         = self.get_token_balance(token_address)
            tokens_recv = (raw / 1e6) if raw > 0 else (buy_amount_sol / price_sol)

            # ── Enhancement 3: Sandwich / MEV detection ──
            expected_tokens = buy_amount_sol / price_sol if price_sol > 0 else 0
            sandwich = detect_sandwich_attack(expected_tokens, tokens_recv, slippage)
            if sandwich["sandwiched"]:
                self._log(
                    f"⚠️ SANDWICH: expected {expected_tokens:,.0f} tokens, got {tokens_recv:,.0f} "
                    f"({sandwich['actual_slippage_pct']:.1f}% slippage)",
                    "warning", "MEV"
                )
                notifier.notify(
                    f"⚠️ <b>SANDWICH ATTACK DETECTED</b>\n"
                    f"🪙 <code>{token_address[:16]}…</code>\n"
                    f"📊 Expected: {expected_tokens:,.0f} tokens\n"
                    f"📉 Received: {tokens_recv:,.0f} tokens\n"
                    f"💸 Actual slippage: {sandwich['actual_slippage_pct']:.1f}%"
                )
            actual_slippage = sandwich.get("actual_slippage_pct", 0.0)

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
            "safety_score":        score,
            "safety_warnings":     safety.get("warnings", []),
            "buy_priority_fee":    buy_priority_fee,
            "sell_priority_fee":   sell_priority_fee,
            "slippage":            slippage,
            # Enhancement 9: slippage analytics
            "expected_tokens":     buy_amount_sol / price_sol if price_sol > 0 else 0,
            "actual_tokens":       tokens_recv,
            "actual_slippage_pct": actual_slippage,
        }

        with self._lock:
            self.positions[token_address] = pos
            self._save_positions()

        self._ensure_monitor()
        self._log(
            f"{'👻 DEMO' if effective_mode==Mode.DEMO else '🚀'} Position opened: "
            f"{tokens_recv:,.0f} tokens @ ${entry_price_usd:.10f}",
            event_type="BUY"
        )
        notifier.notify_buy(token_address, buy_amount_sol, entry_price_usd,
                            take_profit_percent, abs(stop_loss_percent), tx_sig)

        return {
            "success": True, "tx_signature": tx_sig,
            "tokens_received": tokens_recv, "entry_price_usd": entry_price_usd,
            "tp_target_usd": tp_price, "sl_target_usd": sl_price,
            "safety": safety, "mode": effective_mode.value,
        }

    # ── Sell (Enhancement 7: retry logic) ────────────────────────────────────

    def _execute_sell(self, token_address: str, reason: str, retry_count: int = 0):
        with self._lock:
            pos = self.positions.get(token_address)
        if not pos:
            return

        is_demo = pos.get("mode") == Mode.DEMO.value
        self._log(
            f"{'👻 DEMO ' if is_demo else ''}Selling {token_address[:8]}… [{reason}]",
            "warning", "SELL"
        )

        pnl_pct = pos.get("pnl_percent", 0)
        pnl_usd = pos.get("pnl_usd", 0)
        exit_px = pos.get("current_price_usd", 0)
        tx_sig  = "DEMO-SELL"

        if not is_demo:
            raw = self.get_token_balance(token_address)
            if raw == 0:
                self._log("Balance is 0 — skipping sell", "warning", "SELL")
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
                self._log(f"✅ Sell confirmed [{reason}]: {tx_sig[:16]}…", event_type="SELL")
            else:
                err = result.get("error", "unknown")
                if retry_count < 5:
                    self._log(
                        f"⚠️ Sell failed [{reason}] — retrying in 10s (attempt {retry_count+1}/5): {err}",
                        "warning", "SELL"
                    )
                    time.sleep(10)
                    return self._execute_sell(token_address, reason, retry_count + 1)
                else:
                    self._log(f"⛔ Sell FAILED after 5 retries [{reason}]: {err}", "error", "SELL")
                    notifier.notify(
                        f"🚨 <b>SELL FAILED — MANUAL ACTION REQUIRED</b>\n"
                        f"🪙 <code>{token_address[:16]}…</code>\n"
                        f"❌ {err}\n"
                        f"⚠️ Retried 5× — please sell manually."
                    )
        else:
            self._log(f"👻 DEMO sell executed @ ${exit_px:.10f}", event_type="SELL")

        notifier.notify_sell(token_address, reason, pnl_pct, pnl_usd, exit_px, tx_sig)

        with self._lock:
            self.positions.pop(token_address, None)
            self._demo_trends.pop(token_address, None)
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
            self._running        = True
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="monitor"
            )
            self._monitor_thread.start()

    def _monitor_loop(self):
        logger.info("Monitor started")
        sol_price_cache = {"price": 150.0, "ts": 0}

        while self._running:
            try:
                # Enhancement 12: Stuck position check every 300s
                if time.time() - self._last_stuck_check > 300:
                    self.check_stuck_positions()
                    self._last_stuck_check = time.time()

                # Enhancement 5: Clean expired arm confirmations
                with self._confirmation_lock:
                    expired = [
                        addr for addr, p in self.pending_confirmations.items()
                        if time.time() > p["expires_at"]
                    ]
                    for addr in expired:
                        self.pending_confirmations.pop(addr, None)
                        self._log(f"⏰ Arm expired: {addr[:8]}…", event_type="BUY")

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

                    is_demo = pos.get("mode") == Mode.DEMO.value

                    if is_demo:
                        # Enhancement 6: Realistic log-normal simulation
                        last_usd  = pos.get("current_price_usd", pos["entry_price_usd"])
                        trend     = self._demo_trends.get(token_address, "neutral")
                        current_usd, new_trend = simulate_realistic_price_move(
                            last_usd, trend, pos["entry_price_usd"]
                        )
                        self._demo_trends[token_address] = new_trend
                        curve_sol = 0.0
                    else:
                        price_sol, curve_sol = self.get_token_price(pos["bonding_curve"])
                        if price_sol == 0:
                            continue
                        current_usd = price_sol * sol_usd

                    entry_usd = pos["entry_price_usd"]
                    pnl_pct   = (current_usd - entry_usd) / entry_usd * 100
                    pnl_usd   = (current_usd - entry_usd) * pos["position_size"]

                    highest   = max(pos.get("highest_price_usd", entry_usd), current_usd)
                    sl_target = pos["sl_target_usd"]
                    tp_target = pos["tp_target_usd"]
                    trail_pct = pos.get("trailing_stop_pct", 0)

                    if trail_pct > 0 and current_usd > entry_usd:
                        new_sl = highest * (1 - trail_pct / 100)
                        if new_sl > sl_target:
                            sl_target = new_sl
                            self._log(f"Trailing SL → ${sl_target:.10f}", event_type="SELL")

                    pos_val = pos["position_size"] * current_usd
                    tp_prog = max(0, min(100, (pnl_pct / pos["take_profit_pct"]) * 100)) \
                              if pos["take_profit_pct"] > 0 else 0

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
                        self._log(f"🎯 TP hit @ ${current_usd:.10f} (+{pnl_pct:.2f}%)", event_type="TP_HIT")
                        to_sell.append((token_address, "Take Profit"))
                    elif current_usd <= sl_target:
                        self._log(f"🛑 SL hit @ ${current_usd:.10f} ({pnl_pct:.2f}%)", "warning", "SL_HIT")
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

    # ── Auto-scanner loop (Full-Auto) ─────────────────────────────────────────

    def _ensure_scanner(self):
        if not self._scanner_thread or not self._scanner_thread.is_alive():
            self._scan_running   = True
            self._scanner_thread = threading.Thread(
                target=self._scanner_loop, daemon=True, name="scanner"
            )
            self._scanner_thread.start()
            label = "Full-Auto" if self.mode == Mode.FULL_AUTO else "Scanner"
            self._log(f"🔍 {label} started — scanning real-time token launches")

    def _scanner_loop(self):
        logger.info("Scanner loop started")
        scanned_tokens: set = set()
        last_reset = time.time()

        while self._scan_running:
            try:
                if self.mode not in (Mode.FULL_AUTO, Mode.SEMI_AUTO):
                    time.sleep(10)
                    continue

                if time.time() - last_reset > 3600:
                    scanned_tokens.clear()
                    last_reset = time.time()
                    logger.info("Scanner: seen-set cleared (hourly reset)")

                pump_tokens = scan_pump_fun_new(limit=50)
                dex_tokens  = scan_dexscreener_new_pairs(limit=30)
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
                for token in fresh[:12]:
                    addr = token.get("token_address", "")
                    if not addr or len(addr) < 32:
                        continue
                    scanned_tokens.add(addr)

                    bc    = self.pump_sdk.derive_bonding_curve(addr)
                    chain = full_token_scan(self.client, addr, bc)

                    token.update({
                        "safety_score":    chain["score"],
                        "safety_warnings": chain["warnings"],
                        "mint_auth_ok":    chain.get("mint_auth_disabled", False),
                        "mint_authority":  chain.get("mint_authority"),
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
                        f"{'✅ PASS' if chain['safe'] else '⛔ FAIL'}",
                        event_type="SAFETY_CHECK"
                    )

                    if (self.mode == Mode.FULL_AUTO
                            and chain["safe"]
                            and addr not in self.positions
                            and self.wallet):

                        sol_bal     = self.get_sol_balance()
                        auto_amount = float(os.getenv("AUTO_BUY_SOL", "0.05"))

                        if sol_bal >= auto_amount + 0.01:
                            self._log(f"🤖 Full-Auto: Buying {addr[:8]}… (score {chain['score']}/100)", event_type="BUY")
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
                    combined = new_results + self.scan_results
                    seen_addr, deduped = set(), []
                    for r in combined:
                        a = r.get("token_address", "")
                        if a and a not in seen_addr:
                            seen_addr.add(a)
                            deduped.append(r)
                    self.scan_results = deduped[:200]

                time.sleep(20)

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
