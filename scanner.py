# scanner.py - Real-time token scanner: Pump.fun new launches + DexScreener trending
import logging
import time
import base64
import struct
import requests
from solana.rpc.api import Client
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

# Only show tokens younger than this (seconds)
MAX_TOKEN_AGE_SECS = 7200   # 2 hours

# ── Pump.fun real-time new launches ─────────────────────────────────────────

def scan_pump_fun_new(limit: int = 50) -> list[dict]:
    """
    Fetch the most recently created tokens on Pump.fun.
    Filters to tokens launched within the last MAX_TOKEN_AGE_SECS seconds.
    """
    try:
        r = requests.get(
            "https://frontend-api.pump.fun/coins",
            params={
                "offset": 0,
                "limit":  limit,
                "sort":   "created_timestamp",
                "order":  "DESC",
                "includeNsfw": "false",
            },
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            logger.warning(f"Pump.fun API status {r.status_code}")
            return []

        now_ms  = time.time() * 1000
        cutoff  = (time.time() - MAX_TOKEN_AGE_SECS) * 1000
        coins   = r.json() if isinstance(r.json(), list) else []

        result = []
        for c in coins:
            created = c.get("created_timestamp", 0) or 0
            if created < cutoff:
                continue  # skip old tokens
            age_min = round((now_ms - created) / 60000, 1)
            result.append({
                "token_address": c.get("mint", ""),
                "name":          c.get("name", "?"),
                "symbol":        c.get("symbol", "?"),
                "source":        "pump.fun",
                "market_cap":    c.get("usd_market_cap", 0),
                "created_ts":    created,
                "age_min":       age_min,
                "twitter":       c.get("twitter", ""),
                "telegram":      c.get("telegram", ""),
                "website":       c.get("website", ""),
                "description":   (c.get("description") or "")[:80],
                "has_socials":   any([c.get("twitter"), c.get("telegram"), c.get("website")]),
                "reply_count":   c.get("reply_count", 0),
            })
        logger.info(f"Pump.fun: {len(result)} new tokens (last {MAX_TOKEN_AGE_SECS//60}min)")
        return result

    except Exception as e:
        logger.warning(f"scan_pump_fun_new error: {e}")
        return []


# ── DexScreener real-time new Solana pairs ───────────────────────────────────

def scan_dexscreener_new_pairs(limit: int = 30) -> list[dict]:
    """
    Fetch the newest Solana Pump.fun pairs from DexScreener.
    Uses the /latest/dex/pairs/solana endpoint sorted by pairCreatedAt.
    """
    try:
        # Token profiles endpoint — truly newest listed tokens
        r = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        results = []
        seen    = set()

        if r.status_code == 200:
            profiles = r.json() if isinstance(r.json(), list) else []
            cutoff   = time.time() - MAX_TOKEN_AGE_SECS

            for p in profiles:
                if p.get("chainId") != "solana":
                    continue
                addr = p.get("tokenAddress", "")
                if not addr or addr in seen:
                    continue
                seen.add(addr)
                results.append({
                    "token_address": addr,
                    "name":          p.get("description", addr[:8]) or addr[:8],
                    "symbol":        "",
                    "source":        "dexscreener-new",
                    "icon":          p.get("icon", ""),
                    "has_socials":   bool(p.get("links")),
                    "age_min":       0,
                    "market_cap":    0,
                })
                if len(results) >= limit:
                    break

        # Also pull trending Solana pairs on pump.fun
        r2 = requests.get(
            "https://api.dexscreener.com/latest/dex/search",
            params={"q": "pump.fun"},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r2.status_code == 200:
            pairs = (r2.json().get("pairs") or [])
            cutoff_ms = (time.time() - MAX_TOKEN_AGE_SECS) * 1000
            for p in pairs:
                if p.get("chainId") != "solana":
                    continue
                addr = (p.get("baseToken") or {}).get("address", "")
                if not addr or addr in seen:
                    continue
                created_ms = p.get("pairCreatedAt", 0) or 0
                if created_ms and created_ms < cutoff_ms:
                    continue
                seen.add(addr)
                age_min = round((time.time() * 1000 - created_ms) / 60000, 1) if created_ms else 0
                results.append({
                    "token_address":   addr,
                    "name":            (p.get("baseToken") or {}).get("name", "?"),
                    "symbol":          (p.get("baseToken") or {}).get("symbol", "?"),
                    "source":          "dexscreener",
                    "price_usd":       float(p.get("priceUsd") or 0),
                    "volume_h1":       (p.get("volume") or {}).get("h1", 0),
                    "price_change_h1": (p.get("priceChange") or {}).get("h1", 0),
                    "liquidity_usd":   (p.get("liquidity") or {}).get("usd", 0),
                    "market_cap":      p.get("marketCap", 0),
                    "age_min":         age_min,
                    "has_socials":     False,
                })
                if len(results) >= limit * 2:
                    break

        logger.info(f"DexScreener: {len(results)} recent tokens")
        return results

    except Exception as e:
        logger.warning(f"scan_dexscreener error: {e}")
        return []


# ── On-chain mint authority check ────────────────────────────────────────────

def check_mint_authority(rpc_client: Client, token_mint: str) -> dict:
    """
    Check if mint authority and freeze authority are disabled.
    Returns quickly — if RPC times out / rate-limits, returns a soft failure.
    """
    try:
        resp = rpc_client.get_account_info(Pubkey.from_string(token_mint))
        if not resp.value or not resp.value.data:
            return {"mint_auth_disabled": False, "freeze_auth_disabled": False, "error": "No data"}

        raw = resp.value.data
        if isinstance(raw, (list, tuple)):
            data = base64.b64decode(raw[0]) if isinstance(raw[0], str) else bytes(raw[0])
        elif isinstance(raw, bytes):
            data = raw
        else:
            data = base64.b64decode(str(raw))

        if len(data) < 82:
            return {"mint_auth_disabled": False, "freeze_auth_disabled": False, "error": "Data too short"}

        # SPL Mint layout: bytes 0-3 = mintAuthority option (0=None), bytes 46-49 = freezeAuthority option
        mint_auth_opt   = struct.unpack("<I", data[0:4])[0]
        freeze_auth_opt = struct.unpack("<I", data[46:50])[0]

        return {
            "mint_auth_disabled":   mint_auth_opt   == 0,
            "freeze_auth_disabled": freeze_auth_opt == 0,
        }
    except Exception as e:
        logger.debug(f"check_mint_authority: {e}")
        return {"mint_auth_disabled": False, "freeze_auth_disabled": False, "error": str(e)}


def check_top_holders(rpc_client: Client, token_mint: str,
                      bonding_curve_addr: str | None = None,
                      threshold_pct: float = 30.0) -> dict:
    """
    Check top-10 holder concentration.
    Handles 429 / rate-limit gracefully — returns unchecked result instead of crashing.
    """
    try:
        resp = rpc_client.get_token_largest_accounts(Pubkey.from_string(token_mint))
        if not resp.value:
            return {"concentrated": False, "top10_pct": 0.0, "holders": [], "skipped": True}

        accounts = resp.value[:10]
        supply_resp = rpc_client.get_token_supply(Pubkey.from_string(token_mint))
        total_supply = float((supply_resp.value.amount if supply_resp.value else None) or 0)

        if total_supply == 0:
            return {"concentrated": False, "top10_pct": 0.0, "holders": [], "skipped": True}

        bc_lower = (bonding_curve_addr or "").lower()
        holders, top10_amount = [], 0.0

        for acct in accounts:
            addr = str(acct.address)
            amt  = float(acct.amount.amount if hasattr(acct.amount, "amount") else acct.amount)
            pct  = (amt / total_supply) * 100
            is_bc = addr.lower() == bc_lower
            holders.append({"address": addr, "amount": amt, "pct": pct, "is_bonding_curve": is_bc})
            if not is_bc:
                top10_amount += amt

        top10_pct    = (top10_amount / total_supply) * 100
        concentrated = top10_pct > threshold_pct

        return {
            "concentrated": concentrated,
            "top10_pct":    round(top10_pct, 2),
            "holders":      holders,
            "skipped":      False,
        }
    except Exception as e:
        # Silently skip — common on public RPC due to rate limits
        logger.debug(f"check_top_holders skipped ({type(e).__name__}): {e}")
        return {"concentrated": False, "top10_pct": 0.0, "holders": [], "skipped": True, "error": str(e)}


def full_token_scan(rpc_client: Client, token_mint: str,
                    bonding_curve_addr: str | None = None) -> dict:
    """
    Combined on-chain safety scan.
    Mint authority check is always attempted.
    Holder check gracefully skipped on rate-limit.
    """
    mint_check   = check_mint_authority(rpc_client, token_mint)
    time.sleep(0.15)   # small pause to avoid 429 cascade
    holder_check = check_top_holders(rpc_client, token_mint, bonding_curve_addr)

    warnings = []
    score    = 100

    if not mint_check.get("mint_auth_disabled"):
        warnings.append("⚠ Mint authority ENABLED — dev can print tokens")
        score -= 35

    if not mint_check.get("freeze_auth_disabled"):
        warnings.append("⚠ Freeze authority ENABLED — dev can freeze wallets")
        score -= 25

    if not holder_check.get("skipped") and holder_check.get("concentrated"):
        pct = holder_check.get("top10_pct", 0)
        warnings.append(f"⚠ Top-10 wallets hold {pct:.1f}% (bundle risk)")
        score -= 25

    score = max(0, score)
    return {
        "score":               score,
        "safe":                score >= 50 and not any("ENABLED" in w for w in warnings),
        "warnings":            warnings,
        "mint_auth_disabled":  mint_check.get("mint_auth_disabled", False),
        "freeze_auth_disabled":mint_check.get("freeze_auth_disabled", False),
        "top10_pct":           holder_check.get("top10_pct", 0.0),
        "concentrated":        holder_check.get("concentrated", False),
        "holders_skipped":     holder_check.get("skipped", False),
        "holders":             holder_check.get("holders", []),
    }
