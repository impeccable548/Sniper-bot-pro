# jito.py - Jito tip instruction + bundle submission
import random
import struct
import logging
import requests
import base58 as b58
import os

from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.system_program import ID as SYS_PROGRAM_ID

logger = logging.getLogger(__name__)

# Jito tip accounts (rotate randomly to spread tips)
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1uw1nbn2MFn",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

DEFAULT_TIP_LAMPORTS = 1_000_000  # 0.001 SOL


def build_jito_tip_instruction(payer: Pubkey, tip_lamports: int = DEFAULT_TIP_LAMPORTS) -> Instruction:
    """Build a SOL transfer to a random Jito tip account."""
    tip_account = Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS))
    # System program Transfer: discriminant=2 (u32 LE), amount (u64 LE)
    data = struct.pack("<IQ", 2, tip_lamports)
    return Instruction(
        program_id=SYS_PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=payer,       is_signer=True,  is_writable=True),
            AccountMeta(pubkey=tip_account, is_signer=False, is_writable=True),
        ],
        data=bytes(data),
    )


def submit_jito_bundle(signed_tx_bytes: bytes, block_engine_url: str | None = None) -> dict:
    """
    Submit a signed transaction as a Jito bundle.
    Falls back to logging only if no block engine URL is configured.
    """
    url = block_engine_url or os.getenv("JITO_BLOCK_ENGINE_URL", "")
    if not url:
        logger.info("No JITO_BLOCK_ENGINE_URL set — Jito tip included in TX but not bundle-submitted")
        return {"success": False, "error": "no_url"}

    try:
        encoded = b58.b58encode(signed_tx_bytes).decode()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [[encoded]],
        }
        r = requests.post(f"{url.rstrip('/')}/api/v1/bundles", json=payload, timeout=10)
        data = r.json()
        if "result" in data:
            bundle_id = data["result"]
            logger.info(f"Jito bundle submitted: {bundle_id}")
            return {"success": True, "bundle_id": bundle_id}
        else:
            err = data.get("error", {})
            logger.warning(f"Jito bundle error: {err}")
            return {"success": False, "error": str(err)}
    except Exception as e:
        logger.warning(f"Jito bundle submission failed: {e}")
        return {"success": False, "error": str(e)}
