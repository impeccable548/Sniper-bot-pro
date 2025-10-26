# pump_sdk.py - Real Pump.fun SDK Integration - SIMPLE VERSION
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

# Pump.fun Program Constants
PUMP_FUN_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_FUN_EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
PUMP_FUN_FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# Buy instruction discriminator
BUY_INSTRUCTION_DISCRIMINATOR = bytes([0x66, 0x06, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea])

class PumpFunSDK:
    def __init__(self, rpc_url, private_key):
        self.client = Client(rpc_url)
        self.wallet = Keypair.from_bytes(base58.b58decode(private_key)) if private_key else None
        self.slippage = 0.50
        self.priority_fee = 5_000_000
    
    def derive_bonding_curve(self, token_mint):
        """Derive bonding curve PDA"""
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
        """Get ATA for owner and mint"""
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
        """Build buy instruction"""
        try:
            mint_pubkey = Pubkey.from_string(token_mint)
            bonding_curve_pubkey = Pubkey.from_string(bonding_curve)
            
            associated_bonding_curve = self.derive_associated_bonding_curve(bonding_curve, token_mint)
            if not associated_bonding_curve:
                raise Exception("Failed to derive associated bonding curve")
            
            user_token_account = self.get_associated_token_address(self.wallet.pubkey(), token_mint)
            if not user_token_account:
                raise Exception("Failed to get user token account")
            
            token_amount = int(amount_sol * 1e9 * 1000000)
            
            instruction_data = bytearray(BUY_INSTRUCTION_DISCRIMINATOR)
            instruction_data.extend(struct.pack('<Q', token_amount))
            instruction_data.extend(struct.pack('<Q', int(max_sol_cost * 1e9)))
            
            accounts = [
                AccountMeta(pubkey=Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"), is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_FEE_RECIPIENT, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                AccountMeta(pubkey=bonding_curve_pubkey, is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(associated_bonding_curve), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(user_token_account), is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey(), is_signer=True, is_writable=True),
                AccountMeta(pubkey=SYS_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=TOKEN_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=RENT, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_EVENT_AUTHORITY, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FUN_PROGRAM, is_signer=False, is_writable=False),
            ]
            
            return Instruction(
                program_id=PUMP_FUN_PROGRAM,
                accounts=accounts,
                data=bytes(instruction_data)
            )
            
        except Exception as e:
            print(f"Error building buy instruction: {e}")
            return None
    
    def buy_token(self, token_mint, bonding_curve, amount_sol):
        """Execute buy transaction"""
        try:
            print(f"🔨 Building buy transaction...")
            
            max_sol_cost = amount_sol * (1 + self.slippage)
            
            print(f"📡 Getting blockhash...")
            recent_blockhash = self.client.get_latest_blockhash().value.blockhash
            print(f"✅ Got blockhash")
            
            instructions = []
            instructions.append(set_compute_unit_limit(400_000))
            instructions.append(set_compute_unit_price(self.priority_fee))
            
            print(f"🔧 Building buy instruction...")
            buy_ix = self.build_buy_instruction(token_mint, bonding_curve, amount_sol, max_sol_cost)
            if not buy_ix:
                return {"success": False, "error": "Failed to build buy instruction"}
            
            instructions.append(buy_ix)
            print(f"✅ Buy instruction built")
            
            print(f"📝 Compiling message...")
            message = MessageV0.try_compile(
                payer=self.wallet.pubkey(),
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=recent_blockhash
            )
            print(f"✅ Message compiled")
            
            print(f"✍️ Signing transaction...")
            transaction = VersionedTransaction(message, [self.wallet])
            print(f"✅ Transaction signed")
            
            print(f"📤 Sending transaction...")
            response = self.client.send_transaction(
                transaction,
                opts=TxOpts(skip_preflight=True, max_retries=3)
            )
            
            signature = str(response.value)
            print(f"✅ Transaction sent: {signature}")
            print(f"🔗 View: https://solscan.io/tx/{signature}")
            
            # Return immediately - don't wait for confirmation
            # The monitoring loop will track it
            tokens_received = amount_sol * 1e6
            
            print(f"✅ Returning success")
            return {
                "success": True,
                "signature": signature,
                "tokens_received": tokens_received
            }
            
        except Exception as e:
            error_msg = str(e)
            print(f"❌ Buy error: {error_msg}")
            import traceback
            print(f"📋 Traceback:")
            traceback.print_exc()
            return {"success": False, "error": error_msg}
    
    def sell_token(self, token_mint, bonding_curve, token_amount):
        """Sell (Stage 2)"""
        print("⚠️ Sell not implemented yet (Stage 2)")
        return {"success": False, "error": "Sell not implemented yet"}