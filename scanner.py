# scanner.py - Token scanner: Pump.fun new migrations + DexScreener trending
import logging
import requests
import base64
import struct
from solana.rpc.api import Client
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


# ── Pump.fun & DexScreener feeds ──────────────────────────────────────────────

def scan_pump_fun_new(limit: int = 30) -> list[dict]:
    """Fetch newest token launches from Pump.fun."""
    try:
        r = requests.get(
            "https://frontend-api.pump.fun/coins",
            params={
                "offset": 0, "limit": limit,
                "sort": "created_timestamp", "order": "DESC",
                "includeNsfw": "false",
            },
            timeout=8,
        )
        if r.status_code == 200:
            coins = r.json()
            return [
                {
                    "token_address": c.get("mint", ""),
                    "name":          c.get("name", "?"),
                    "symbol":        c.get("symbol", "?"),
                    "source":        "pump.fun",
                    "market_cap":    c.get("usd_market_cap", 0),
                    "created_ts":    c.get("created_timestamp", 0),
                    "twitter":       c.get("twitter", ""),
                    "telegram":      c.get("telegram", ""),
                    "website":       c.get("website", ""),
                    "description":   c.get("description", ""),
                }
                for c in coins
                if c.get("mint")
            ]
    except Exception as e:
        logger.warning(f"scan_pump_fun_new error: {e}")
    return []


def scan_dexscreener_trending(limit: int = 20) -> list[dict]:
    """Fetch trending Pump.fun tokens from DexScreener."""
    try:
        r = requests.get(
            "https://api.dexscreener.com/latest/dex/search",
            params={"q": "pump.fun"},
            timeout=8,
        )
        if r.status_code == 200:
            pairs = (r.json().get("pairs") or [])
            result = []
            seen = set()
            for p in pairs:
                if p.get("chainId") != "solana":
                    continue
                addr = (p.get("baseToken") or {}).get("address", "")
                if not addr or addr in seen:
                    continue
                seen.add(addr)
                result.append({
                    "token_address":    addr,
                    "name":             (p.get("baseToken") or {}).get("name", "?"),
                    "symbol":           (p.get("baseToken") or {}).get("symbol", "?"),
                    "source":           "dexscreener",
                    "price_usd":        float(p.get("priceUsd") or 0),
                    "volume_h1":        (p.get("volume") or {}).get("h1", 0),
                    "price_change_h1":  (p.get("priceChange") or {}).get("h1", 0),
                    "liquidity_usd":    (p.get("liquidity") or {}).get("usd", 0),
                    "market_cap":       p.get("marketCap", 0),
                })
                if len(result) >= limit:
                    break
            return result
    except Exception as e:
        logger.warning(f"scan_dexscreener_trending error: {e}")
    return []


# ── On-chain safety checks ─────────────────────────────────────────────────────

def check_mint_authority(rpc_client: Client, token_mint: str) -> dict:
    """
    Check if mint authority is disabled and if freeze authority is disabled.
    Returns {"mint_auth_disabled": bool, "freeze_auth_disabled": bool}
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

        # SPL Mint layout (82 bytes):
        # 0-3:    mint_authority option (4 bytes: 0=None, 1=Some)
        # 4-35:   mint_authority pubkey (32 bytes, only valid if option=1)
        # 36-43:  supply (u64)
        # 44:     decimals (u8)
        # 45:     is_initialized (bool)
        # 46-49:  freeze_authority option (4 bytes: 0=None, 1=Some)
        # 50-81:  freeze_authority pubkey

        if len(data) < 82:
            return {"mint_auth_disabled": False, "freeze_auth_disabled": False, "error": "Data too short"}

        mint_auth_opt   = struct.unpack("<I", data[0:4])[0]
        freeze_auth_opt = struct.unpack("<I", data[46:50])[0]

        return {
            "mint_auth_disabled":   mint_auth_opt == 0,
            "freeze_auth_disabled": freeze_auth_opt == 0,
        }
    except Exception as e:
        logger.warning(f"check_mint_authority error: {e}")
        return {"mint_auth_disabled": False, "freeze_auth_disabled": False, "error": str(e)}


def check_top_holders(rpc_client: Client, token_mint: str,
                      bonding_curve_addr: str | None = None,
                      threshold_pct: float = 30.0) -> dict:
    """
    Check top-10 holder concentration (excluding bonding curve).
    Returns {"concentrated": bool, "top10_pct": float, "holders": list}
    """
    try:
        resp = rpc_client.get_token_largest_accounts(Pubkey.from_string(token_mint))
        if not resp.value:
            return {"concentrated": False, "top10_pct": 0.0, "holders": []}

        accounts = resp.value[:10]

        # Get total supply
        supply_resp = rpc_client.get_token_supply(Pubkey.from_string(token_mint))
        total_supply = float((supply_resp.value.amount if supply_resp.value else None) or 0)

        if total_supply == 0:
            return {"concentrated": False, "top10_pct": 0.0, "holders": []}

        # Exclude bonding curve from concentration calc
        bc_lower = (bonding_curve_addr or "").lower()
        holders = []
        top10_amount = 0.0

        for acct in accounts:
            addr = str(acct.address)
            amt  = float(acct.amount.amount if hasattr(acct.amount, 'amount') else acct.amount)
            pct  = (amt / total_supply) * 100
            is_bc = addr.lower() == bc_lower
            holders.append({"address": addr, "amount": amt, "pct": pct, "is_bonding_curve": is_bc})
            if not is_bc:
                top10_amount += amt

        top10_pct   = (top10_amount / total_supply) * 100
        concentrated = top10_pct > threshold_pct

        return {
            "concentrated": concentrated,
            "top10_pct":    round(top10_pct, 2),
            "holders":      holders,
        }
    except Exception as e:
        logger.warning(f"check_top_holders error: {e}")
        return {"concentrated": False, "top10_pct": 0.0, "holders": [], "error": str(e)}


def full_token_scan(rpc_client: Client, token_mint: str,
                    bonding_curve_addr: str | None = None) -> dict:
    """
    Run all on-chain safety checks for a token.
    Returns a combined safety report.
    """
    mint_check   = check_mint_authority(rpc_client, token_mint)
    holder_check = check_top_holders(rpc_client, token_mint, bonding_curve_addr)

    warnings = []
    score = 100

    if not mint_check.get("mint_auth_disabled"):
        warnings.append("⚠ Mint authority is ENABLED — dev can print more tokens")
        score -= 30

    if not mint_check.get("freeze_auth_disabled"):
        warnings.append("⚠ Freeze authority is ENABLED — dev can freeze wallets")
        score -= 20

    if holder_check.get("concentrated"):
        pct = holder_check.get("top10_pct", 0)
        warnings.append(f"⚠ Top-10 holders own {pct:.1f}% of supply (bundle risk)")
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
        "holders":             holder_check.get("holders", []),
    }
