# pump_sdk.py - Pump.fun SDK: Buy + Sell + Jito Tip + CloseAccount + MEV detection
import base58
import struct
import time
import logging
import os

from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.instruction import Instruction, AccountMeta
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.system_program import ID as SYS_PROGRAM_ID
from solders.sysvar import RENT

from jito import build_jito_tip_instruction, submit_jito_bundle, DEFAULT_TIP_LAMPORTS

logger = logging.getLogger(__name__)

# ── Program constants ──────────────────────────────────────────────────────────
PUMP_FUN_PROGRAM         = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_FUN_EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
PUMP_FUN_FEE_RECIPIENT   = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
TOKEN_PROGRAM            = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
GLOBAL_STATE             = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")

BUY_DISCRIMINATOR  = bytes([0x66, 0x06, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea])
SELL_DISCRIMINATOR = bytes([0x33, 0xe6, 0x85, 0xa4, 0x01, 0x7f, 0x83, 0xad])

COMPUTE_UNIT_LIMIT        = 200_000
DEFAULT_BUY_PRIORITY_FEE  = 100_000
DEFAULT_SELL_PRIORITY_FEE = 800_000


# ── Sandwich / MEV detection (module-level, no wallet needed) ─────────────────

def detect_sandwich_attack(expected_tokens: float, actual_tokens: float,
                           expected_slippage: float) -> dict:
    """
    Compare expected vs actual tokens received after a buy.
    If actual slippage > expected slippage + 10% threshold → likely sandwiched.
    """
    if expected_tokens <= 0:
        return {"sandwiched": False, "actual_slippage_pct": 0.0,
                "expected_tokens": expected_tokens, "actual_tokens": actual_tokens}

    actual_slippage = (expected_tokens - actual_tokens) / expected_tokens
    sandwiched      = actual_slippage > (expected_slippage + 0.10)

    return {
        "sandwiched":           sandwiched,
        "expected_tokens":      round(expected_tokens, 2),
        "actual_tokens":        round(actual_tokens, 2),
        "expected_slippage_pct":round(expected_slippage * 100, 2),
        "actual_slippage_pct":  round(actual_slippage * 100, 2),
    }


# ── PumpFunSDK ────────────────────────────────────────────────────────────────

class PumpFunSDK:
    def __init__(self, rpc_url: str, private_key: str,
                 slippage: float = 0.10,
                 buy_priority_fee: int  = DEFAULT_BUY_PRIORITY_FEE,
                 sell_priority_fee: int = DEFAULT_SELL_PRIORITY_FEE,
                 jito_tip_lamports: int = DEFAULT_TIP_LAMPORTS,
                 use_jito: bool = True):

        self.client            = Client(rpc_url)
        self.rpc_url           = rpc_url
        self.wallet            = Keypair.from_bytes(base58.b58decode(private_key)) if private_key else None
        self.slippage          = slippage
        self.buy_priority_fee  = buy_priority_fee
        self.sell_priority_fee = sell_priority_fee
        self.jito_tip_lamports = jito_tip_lamports
        self.use_jito          = use_jito

    # ── PDA helpers ───────────────────────────────────────────────────────────

    def derive_bonding_curve(self, token_mint: str) -> str | None:
        try:
            mint = Pubkey.from_string(token_mint)
            pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP_FUN_PROGRAM)
            return str(pda)
        except Exception as e:
            logger.error(f"derive_bonding_curve: {e}")
            return None

    def derive_associated_bonding_curve(self, bonding_curve: str, token_mint: str) -> str | None:
        try:
            bc   = Pubkey.from_string(bonding_curve)
            mint = Pubkey.from_string(token_mint)
            pda, _ = Pubkey.find_program_address(
                [bytes(bc), bytes(TOKEN_PROGRAM), bytes(mint)],
                ASSOCIATED_TOKEN_PROGRAM,
            )
            return str(pda)
        except Exception as e:
            logger.error(f"derive_associated_bonding_curve: {e}")
            return None

    def get_associated_token_address(self, owner, mint) -> str | None:
        try:
            o = Pubkey.from_string(owner) if isinstance(owner, str) else owner
            m = Pubkey.from_string(mint)  if isinstance(mint,  str) else mint
            pda, _ = Pubkey.find_program_address(
                [bytes(o), bytes(TOKEN_PROGRAM), bytes(m)],
                ASSOCIATED_TOKEN_PROGRAM,
            )
            return str(pda)
        except Exception as e:
            logger.error(f"get_associated_token_address: {e}")
            return None

    # ── Transaction helpers ───────────────────────────────────────────────────

    def _base_ixs(self, priority_fee: int) -> list:
        ixs = [
            set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
            set_compute_unit_price(priority_fee),
        ]
        if self.use_jito and self.wallet:
            ixs.append(build_jito_tip_instruction(self.wallet.pubkey(), self.jito_tip_lamports))
        return ixs

    def _send_and_confirm(self, instructions: list, max_attempts: int = 40,
                          max_retries: int = 3) -> dict:
        """
        Send transaction with exponential-backoff retry on network/timeout failures.
        Retries up to max_retries times: backoff = 2^retry seconds (2, 4, 8).
        """
        if not self.wallet:
            return {"success": False, "error": "No wallet"}

        last_error = "Unknown error"

        for retry in range(max_retries + 1):
            if retry > 0:
                wait = 2 ** retry
                logger.info(f"TX retry {retry}/{max_retries} — waiting {wait}s (backoff)…")
                time.sleep(wait)

            try:
                recent_blockhash = self.client.get_latest_blockhash().value.blockhash
                msg = MessageV0.try_compile(
                    payer=self.wallet.pubkey(),
                    instructions=instructions,
                    address_lookup_table_accounts=[],
                    recent_blockhash=recent_blockhash,
                )
                tx  = VersionedTransaction(msg, [self.wallet])
                raw = bytes(tx)

                jito_url = os.getenv("JITO_BLOCK_ENGINE_URL", "")
                if self.use_jito and jito_url:
                    bundle_result = submit_jito_bundle(raw, jito_url)
                    if bundle_result.get("success"):
                        logger.info(f"TX via Jito bundle: {bundle_result.get('bundle_id')}")

                resp = self.client.send_transaction(
                    tx, opts=TxOpts(skip_preflight=True, max_retries=3)
                )
                sig = str(resp.value)
                logger.info(f"TX sent: {sig}")

                confirmed = False
                tx_error  = None
                for _ in range(max_attempts):
                    time.sleep(2)
                    try:
                        sig_bytes   = base58.b58decode(sig)
                        status_resp = self.client.get_signature_statuses([sig_bytes])
                        if status_resp and status_resp.value and status_resp.value[0]:
                            s = status_resp.value[0]
                            if s.err:
                                tx_error  = f"TX failed on-chain: {s.err}"
                                break
                            if s.confirmation_status:
                                logger.info(f"TX confirmed: {sig}")
                                confirmed = True
                                break
                    except Exception:
                        continue

                if confirmed:
                    return {"success": True, "signature": sig}

                if tx_error:
                    last_error = tx_error
                    logger.warning(f"TX on-chain error (attempt {retry+1}): {tx_error}")
                    # Don't retry on-chain failures — the TX was processed, just failed
                    return {"success": False, "error": tx_error, "signature": sig}
                else:
                    # Timeout — assume success on last retry, retry otherwise
                    if retry == max_retries:
                        logger.warning(f"TX confirmation timeout after {max_retries+1} attempts — assuming success")
                        return {"success": True, "signature": sig}
                    last_error = "Confirmation timeout"
                    logger.warning(f"TX timeout on attempt {retry+1} — will retry")

            except Exception as e:
                last_error = str(e)
                logger.warning(f"TX attempt {retry+1} exception: {e}")

        return {"success": False, "error": last_error}

    # ── CloseAccount (rent recovery) ─────────────────────────────────────────

    def _build_close_account_ix(self, token_account: Pubkey) -> Instruction:
        return Instruction(
            program_id=TOKEN_PROGRAM,
            accounts=[
                AccountMeta(pubkey=token_account,       is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey(), is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey(), is_signer=True,  is_writable=False),
            ],
            data=bytes([9]),
        )

    # ── BUY ───────────────────────────────────────────────────────────────────

    def buy_token(self, token_mint: str, bonding_curve: str,
                  amount_sol: float, priority_fee: int | None = None) -> dict:
        try:
            if not self.wallet:
                return {"success": False, "error": "No wallet configured"}

            pf       = priority_fee if priority_fee is not None else self.buy_priority_fee
            mint_pk  = Pubkey.from_string(token_mint)
            bc_pk    = Pubkey.from_string(bonding_curve)
            assoc_bc = self.derive_associated_bonding_curve(bonding_curve, token_mint)
            user_ata = self.get_associated_token_address(str(self.wallet.pubkey()), token_mint)

            if not assoc_bc or not user_ata:
                return {"success": False, "error": "PDA derivation failed"}

            max_sol_cost = amount_sol * (1 + self.slippage)
            token_amount = int(amount_sol * 1e9 * 1_000_000)

            data = bytearray(BUY_DISCRIMINATOR)
            data.extend(struct.pack('<Q', token_amount))
            data.extend(struct.pack('<Q', int(max_sol_cost * 1e9)))

            accounts = [
                AccountMeta(pubkey=GLOBAL_STATE,                    is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_FEE_RECIPIENT,          is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_pk,                         is_signer=False, is_writable=False),
                AccountMeta(pubkey=bc_pk,                           is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(assoc_bc),    is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(user_ata),    is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey(),            is_signer=True,  is_writable=True),
                AccountMeta(pubkey=SYS_PROGRAM_ID,                  is_signer=False, is_writable=False),
                AccountMeta(pubkey=TOKEN_PROGRAM,                   is_signer=False, is_writable=False),
                AccountMeta(pubkey=RENT,                            is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_EVENT_AUTHORITY,        is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_PROGRAM,                is_signer=False, is_writable=False),
            ]

            buy_ix       = Instruction(program_id=PUMP_FUN_PROGRAM, accounts=accounts, data=bytes(data))
            instructions = self._base_ixs(pf) + [buy_ix]

            result = self._send_and_confirm(instructions)
            if result["success"]:
                result["tokens_received"] = amount_sol / 1e-6
            return result

        except Exception as e:
            logger.error(f"buy_token error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ── SELL + CloseAccount ───────────────────────────────────────────────────

    def sell_token(self, token_mint: str, bonding_curve: str,
                   token_amount: int, min_sol_output: float = 0.0,
                   priority_fee: int | None = None,
                   close_account: bool = True) -> dict:
        try:
            if not self.wallet:
                return {"success": False, "error": "No wallet configured"}

            pf       = priority_fee if priority_fee is not None else self.sell_priority_fee
            mint_pk  = Pubkey.from_string(token_mint)
            bc_pk    = Pubkey.from_string(bonding_curve)
            assoc_bc = self.derive_associated_bonding_curve(bonding_curve, token_mint)
            user_ata = self.get_associated_token_address(str(self.wallet.pubkey()), token_mint)

            if not assoc_bc or not user_ata:
                return {"success": False, "error": "PDA derivation failed"}

            min_sol_lamports = int(min_sol_output * 1e9 * (1 - self.slippage))

            data = bytearray(SELL_DISCRIMINATOR)
            data.extend(struct.pack('<Q', token_amount))
            data.extend(struct.pack('<Q', min_sol_lamports))

            accounts = [
                AccountMeta(pubkey=GLOBAL_STATE,                    is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_FEE_RECIPIENT,          is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_pk,                         is_signer=False, is_writable=False),
                AccountMeta(pubkey=bc_pk,                           is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(assoc_bc),    is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(user_ata),    is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey(),            is_signer=True,  is_writable=True),
                AccountMeta(pubkey=SYS_PROGRAM_ID,                  is_signer=False, is_writable=False),
                AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM,        is_signer=False, is_writable=False),
                AccountMeta(pubkey=TOKEN_PROGRAM,                   is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_EVENT_AUTHORITY,        is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_PROGRAM,                is_signer=False, is_writable=False),
            ]

            sell_ix      = Instruction(program_id=PUMP_FUN_PROGRAM, accounts=accounts, data=bytes(data))
            instructions = self._base_ixs(pf) + [sell_ix]

            if close_account:
                close_ix = self._build_close_account_ix(Pubkey.from_string(user_ata))
                instructions.append(close_ix)
                logger.info("CloseAccount bundled — rent will be refunded")

            return self._send_and_confirm(instructions)

        except Exception as e:
            logger.error(f"sell_token error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
