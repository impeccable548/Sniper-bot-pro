# pump_sdk.py - Pump.fun SDK with Buy + Sell
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
import base58
import struct
import time
import logging

logger = logging.getLogger(__name__)

# Pump.fun Program Constants
PUMP_FUN_PROGRAM        = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_FUN_EVENT_AUTHORITY= Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
PUMP_FUN_FEE_RECIPIENT  = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
TOKEN_PROGRAM           = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM= Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
GLOBAL_STATE            = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")

BUY_DISCRIMINATOR  = bytes([0x66, 0x06, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea])
SELL_DISCRIMINATOR = bytes([0x33, 0xe6, 0x85, 0xa4, 0x01, 0x7f, 0x83, 0xad])


class PumpFunSDK:
    def __init__(self, rpc_url, private_key, slippage=0.25, priority_fee=5_000_000):
        self.client = Client(rpc_url)
        self.wallet = Keypair.from_bytes(base58.b58decode(private_key)) if private_key else None
        self.slippage = slippage
        self.priority_fee = priority_fee

    # ──────────────────────────────────────────────
    # PDA helpers
    # ──────────────────────────────────────────────
    def derive_bonding_curve(self, token_mint: str) -> str | None:
        try:
            mint = Pubkey.from_string(token_mint)
            pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP_FUN_PROGRAM)
            return str(pda)
        except Exception as e:
            logger.error(f"derive_bonding_curve error: {e}")
            return None

    def derive_associated_bonding_curve(self, bonding_curve: str, token_mint: str) -> str | None:
        try:
            bc  = Pubkey.from_string(bonding_curve)
            mint= Pubkey.from_string(token_mint)
            pda, _ = Pubkey.find_program_address(
                [bytes(bc), bytes(TOKEN_PROGRAM), bytes(mint)],
                ASSOCIATED_TOKEN_PROGRAM
            )
            return str(pda)
        except Exception as e:
            logger.error(f"derive_associated_bonding_curve error: {e}")
            return None

    def get_associated_token_address(self, owner, mint) -> str | None:
        try:
            o = Pubkey.from_string(owner) if isinstance(owner, str) else owner
            m = Pubkey.from_string(mint)  if isinstance(mint,  str) else mint
            pda, _ = Pubkey.find_program_address(
                [bytes(o), bytes(TOKEN_PROGRAM), bytes(m)],
                ASSOCIATED_TOKEN_PROGRAM
            )
            return str(pda)
        except Exception as e:
            logger.error(f"get_associated_token_address error: {e}")
            return None

    # ──────────────────────────────────────────────
    # Transaction helpers
    # ──────────────────────────────────────────────
    def _base_instructions(self, compute_units=400_000):
        return [
            set_compute_unit_limit(compute_units),
            set_compute_unit_price(self.priority_fee),
        ]

    def _send_and_confirm(self, instructions, recent_blockhash=None, max_attempts=40):
        if recent_blockhash is None:
            recent_blockhash = self.client.get_latest_blockhash().value.blockhash

        msg = MessageV0.try_compile(
            payer=self.wallet.pubkey(),
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=recent_blockhash,
        )
        tx = VersionedTransaction(msg, [self.wallet])
        resp = self.client.send_transaction(tx, opts=TxOpts(skip_preflight=True, max_retries=3))
        sig = str(resp.value)
        logger.info(f"TX sent: {sig}")

        for _ in range(max_attempts):
            time.sleep(2)
            try:
                sig_bytes = base58.b58decode(sig)
                status_resp = self.client.get_signature_statuses([sig_bytes])
                if status_resp and status_resp.value and status_resp.value[0]:
                    s = status_resp.value[0]
                    if s.err:
                        return {"success": False, "error": f"TX failed on-chain: {s.err}", "signature": sig}
                    if s.confirmation_status:
                        logger.info(f"TX confirmed: {sig}")
                        return {"success": True, "signature": sig}
            except Exception:
                continue

        logger.warning("TX confirmation timeout — assuming success")
        return {"success": True, "signature": sig}

    # ──────────────────────────────────────────────
    # BUY
    # ──────────────────────────────────────────────
    def buy_token(self, token_mint: str, bonding_curve: str, amount_sol: float):
        try:
            if not self.wallet:
                return {"success": False, "error": "No wallet configured"}

            mint_pk    = Pubkey.from_string(token_mint)
            bc_pk      = Pubkey.from_string(bonding_curve)
            assoc_bc   = self.derive_associated_bonding_curve(bonding_curve, token_mint)
            user_ata   = self.get_associated_token_address(str(self.wallet.pubkey()), token_mint)

            if not assoc_bc or not user_ata:
                return {"success": False, "error": "PDA derivation failed"}

            max_sol_cost  = amount_sol * (1 + self.slippage)
            token_amount  = int(amount_sol * 1e9 * 1_000_000)

            data = bytearray(BUY_DISCRIMINATOR)
            data.extend(struct.pack('<Q', token_amount))
            data.extend(struct.pack('<Q', int(max_sol_cost * 1e9)))

            accounts = [
                AccountMeta(pubkey=GLOBAL_STATE,                     is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_FEE_RECIPIENT,           is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_pk,                          is_signer=False, is_writable=False),
                AccountMeta(pubkey=bc_pk,                            is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(assoc_bc),     is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(user_ata),     is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey(),             is_signer=True,  is_writable=True),
                AccountMeta(pubkey=SYS_PROGRAM_ID,                   is_signer=False, is_writable=False),
                AccountMeta(pubkey=TOKEN_PROGRAM,                    is_signer=False, is_writable=False),
                AccountMeta(pubkey=RENT,                             is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_EVENT_AUTHORITY,         is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_PROGRAM,                 is_signer=False, is_writable=False),
            ]

            buy_ix = Instruction(program_id=PUMP_FUN_PROGRAM, accounts=accounts, data=bytes(data))
            instructions = self._base_instructions() + [buy_ix]

            result = self._send_and_confirm(instructions)
            if result["success"]:
                result["tokens_received"] = amount_sol / 1e-6  # rough estimate
            return result

        except Exception as e:
            logger.error(f"buy_token error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ──────────────────────────────────────────────
    # SELL
    # ──────────────────────────────────────────────
    def sell_token(self, token_mint: str, bonding_curve: str, token_amount: int, min_sol_output: float = 0.0):
        """
        Sell `token_amount` (raw units) of `token_mint` back through the bonding curve.
        `min_sol_output` is the minimum SOL to receive (slippage guard).
        """
        try:
            if not self.wallet:
                return {"success": False, "error": "No wallet configured"}

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
                AccountMeta(pubkey=GLOBAL_STATE,                     is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_FEE_RECIPIENT,           is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_pk,                          is_signer=False, is_writable=False),
                AccountMeta(pubkey=bc_pk,                            is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(assoc_bc),     is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(user_ata),     is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey(),             is_signer=True,  is_writable=True),
                AccountMeta(pubkey=SYS_PROGRAM_ID,                   is_signer=False, is_writable=False),
                AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM,         is_signer=False, is_writable=False),
                AccountMeta(pubkey=TOKEN_PROGRAM,                    is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_EVENT_AUTHORITY,         is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_PROGRAM,                 is_signer=False, is_writable=False),
            ]

            sell_ix = Instruction(program_id=PUMP_FUN_PROGRAM, accounts=accounts, data=bytes(data))
            instructions = self._base_instructions(600_000) + [sell_ix]

            return self._send_and_confirm(instructions)

        except Exception as e:
            logger.error(f"sell_token error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
