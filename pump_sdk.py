# pump_sdk.py - Real Pump.fun SDK Integration
from solana.rpc.api import Client
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

# Pump.fun Program Constants
PUMP_FUN_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_FUN_EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
PUMP_FUN_FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# Buy instruction discriminator (first 8 bytes of instruction data)
BUY_INSTRUCTION_DISCRIMINATOR = bytes([0x66, 0x06, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea])

class PumpFunSDK:
    def __init__(self, rpc_url, private_key):
        self.client = Client(rpc_url)
        self.wallet = Keypair.from_bytes(base58.b58decode(private_key)) if private_key else None
        self.slippage = 0.50  # 50% slippage
        self.priority_fee = 5_000_000  # 0.005 SOL in lamports

    def derive_bonding_curve(self, token_mint):
        """Derive bonding curve PDA from token mint"""
        try:
            mint_pubkey = Pubkey.from_string(token_mint)
            seeds = [b"bonding-curve", bytes(mint_pubkey)]
            pda, bump = Pubkey.find_program_address(seeds, PUMP_FUN_PROGRAM)
            return str(pda)
        except Exception as e:
            print(f"Error deriving bonding curve: {e}")
            return None

    def derive_associated_bonding_curve(self, bonding_curve, token_mint):
        """Derive associated bonding curve token account"""
        try:
            bonding_curve_pubkey = Pubkey.from_string(bonding_curve)
            mint_pubkey = Pubkey.from_string(token_mint)

            # Find associated token account
            seeds = [
                bytes(bonding_curve_pubkey),
                bytes(TOKEN_PROGRAM),
                bytes(mint_pubkey)
            ]
            pda, bump = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM)
            return str(pda)
        except Exception as e:
            print(f"Error deriving associated bonding curve: {e}")
            return None

    def get_associated_token_address(self, owner, mint):
        """Get associated token address for owner and mint"""
        try:
            owner_pubkey = Pubkey.from_string(owner) if isinstance(owner, str) else owner
            mint_pubkey = Pubkey.from_string(mint) if isinstance(mint, str) else mint

            seeds = [
                bytes(owner_pubkey),
                bytes(TOKEN_PROGRAM),
                bytes(mint_pubkey)
            ]
            pda, bump = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM)
            return str(pda)
        except Exception as e:
            print(f"Error getting ATA: {e}")
            return None

    def build_buy_instruction(self, token_mint, bonding_curve, amount_sol, max_sol_cost):
        """Build Pump.fun buy instruction"""
        try:
            mint_pubkey = Pubkey.from_string(token_mint)
            bonding_curve_pubkey = Pubkey.from_string(bonding_curve)

            # Derive associated bonding curve
            associated_bonding_curve = self.derive_associated_bonding_curve(bonding_curve, token_mint)
            if not associated_bonding_curve:
                raise Exception("Failed to derive associated bonding curve")

            # Get user's associated token account
            user_token_account = self.get_associated_token_address(self.wallet.pubkey(), token_mint)
            if not user_token_account:
                raise Exception("Failed to get user token account")

            # Calculate token amount (with slippage)
            # This is simplified - real calculation needs curve state
            token_amount = int(amount_sol * 1e9 * 1000000)  # Rough estimate

            # Build instruction data
            instruction_data = bytearray(BUY_INSTRUCTION_DISCRIMINATOR)
            instruction_data.extend(struct.pack('<Q', token_amount))  # Token amount
            instruction_data.extend(struct.pack('<Q', int(max_sol_cost * 1e9)))  # Max SOL cost

            # Build accounts list
            accounts = [
                AccountMeta(pubkey=Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"), is_signer=False, is_writable=False),  # Global state
                AccountMeta(pubkey=PUMP_FUN_FEE_RECIPIENT, is_signer=False, is_writable=True),  # Fee recipient
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),  # Token mint
                AccountMeta(pubkey=bonding_curve_pubkey, is_signer=False, is_writable=True),  # Bonding curve
                AccountMeta(pubkey=Pubkey.from_string(associated_bonding_curve), is_signer=False, is_writable=True),  # Associated bonding curve
                AccountMeta(pubkey=Pubkey.from_string(user_token_account), is_signer=False, is_writable=True),  # User token account
                AccountMeta(pubkey=self.wallet.pubkey(), is_signer=True, is_writable=True),  # User (payer)
                AccountMeta(pubkey=SYS_PROGRAM_ID, is_signer=False, is_writable=False),  # System program
                AccountMeta(pubkey=TOKEN_PROGRAM, is_signer=False, is_writable=False),  # Token program
                AccountMeta(pubkey=RENT, is_signer=False, is_writable=False),  # Rent
                AccountMeta(pubkey=PUMP_FUN_EVENT_AUTHORITY, is_signer=False, is_writable=False),  # Event authority
                AccountMeta(pubkey=PUMP_FUN_PROGRAM, is_signer=False, is_writable=False),  # Program
            ]

            # Create instruction
            instruction = Instruction(
                program_id=PUMP_FUN_PROGRAM,
                accounts=accounts,
                data=bytes(instruction_data)
            )

            return instruction

        except Exception as e:
            print(f"Error building buy instruction: {e}")
            return None

    def buy_token(self, token_mint, bonding_curve, amount_sol):
        """Execute buy transaction on Pump.fun"""
        try:
            print(f"🔨 Building buy transaction...")

            # Calculate max SOL cost with slippage
            max_sol_cost = amount_sol * (1 + self.slippage)

            # Get recent blockhash
            recent_blockhash = self.client.get_latest_blockhash().value.blockhash

            # Build instructions
            instructions = []

            # Add compute budget instructions
            instructions.append(set_compute_unit_limit(400_000))
            instructions.append(set_compute_unit_price(self.priority_fee))

            # Add buy instruction
            buy_ix = self.build_buy_instruction(token_mint, bonding_curve, amount_sol, max_sol_cost)
            if not buy_ix:
                return {"success": False, "error": "Failed to build buy instruction"}

            instructions.append(buy_ix)

            # Create message
            message = MessageV0.try_compile(
                payer=self.wallet.pubkey(),
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=recent_blockhash
            )

            # Create and sign transaction
            transaction = VersionedTransaction(message, [self.wallet])

            print(f"📤 Sending transaction...")

            # Send transaction
            response = self.client.send_transaction(
                transaction,
                opts={"skip_preflight": True, "max_retries": 3}
            )

            signature = str(response.value)
            print(f"✅ Transaction sent: {signature}")

            # Wait for confirmation
            print(f"⏳ Waiting for confirmation...")
            max_attempts = 30
            for i in range(max_attempts):
                time.sleep(2)
                status = self.client.get_signature_statuses([signature]).value[0]
                if status and status.confirmation_status:
                    if status.err:
                        return {"success": False, "error": f"Transaction failed: {status.err}"}
                    print(f"✅ Transaction confirmed!")

                    # Estimate tokens received (simplified)
                    tokens_received = amount_sol * 1e6  # Rough estimate

                    return {
                        "success": True,
                        "signature": signature,
                        "tokens_received": tokens_received
                    }

            return {"success": False, "error": "Transaction timeout"}

        except Exception as e:
            print(f"❌ Buy error: {e}")
            return {"success": False, "error": str(e)}

    def sell_token(self, token_mint, bonding_curve, token_amount):
        """Execute sell transaction on Pump.fun (for Stage 2)"""
        # TODO: Implement sell logic for Stage 2
        print("⚠️ Sell function not implemented yet (Stage 2)")
        return {"success": False, "error": "Sell not implemented yet"}