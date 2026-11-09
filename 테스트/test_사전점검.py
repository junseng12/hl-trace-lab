from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS = (
    Path(__file__).resolve().parents[1]
    / "02_오더북_가치이전"
    / "실행도구"
)
sys.path.insert(0, str(TOOLS))

import 테스트넷_사전점검 as preflight
from eth_account import Account


def valid_config() -> dict[str, str]:
    config = {
        "HL_NETWORK": "testnet",
        "HL_API_URL": preflight.EXPECTED_API_URL,
        "HL_ENABLE_SIGNED_ACTIONS": "false",
        "HL_MAX_ORDER_NOTIONAL_USD": "12",
        "HL_MAX_RUN_NOTIONAL_USD": "20",
        "HL_MAX_OPEN_POSITION_USD": "10",
        "HL_MAX_ORDERS_PER_RUN": "12",
        "HL_MAX_SLIPPAGE_BPS": "50",
    }
    for index, slot in enumerate(preflight.SLOT_NAMES, start=1):
        # Deterministic, test-only keys. They are never read from .env.local.
        key = index.to_bytes(32, "big").hex()
        agent = Account.from_key(bytes.fromhex(key)).address
        master_key = (index + 100).to_bytes(32, "big").hex()
        master = Account.from_key(bytes.fromhex(master_key)).address
        config[f"HL_{slot}_MASTER_ADDRESS"] = master
        config[f"HL_{slot}_AGENT_ADDRESS"] = agent
        config[f"HL_{slot}_AGENT_PRIVATE_KEY"] = key
    return config


class ValidateConfigTests(unittest.TestCase):
    def test_accepts_strict_testnet_four_wallet_config(self) -> None:
        result = preflight.validate_config(valid_config())
        self.assertEqual(result.api_url, preflight.EXPECTED_API_URL)
        self.assertEqual(len(result.wallets), 4)

    def test_rejects_mainnet(self) -> None:
        config = valid_config()
        config["HL_NETWORK"] = "mainnet"
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_config(config)

    def test_rejects_nonofficial_url(self) -> None:
        config = valid_config()
        config["HL_API_URL"] = "https://api.hyperliquid.xyz"
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_config(config)

    def test_rejects_duplicate_master(self) -> None:
        config = valid_config()
        config["HL_W2_MASTER_ADDRESS"] = config["HL_W1_MASTER_ADDRESS"]
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_config(config)

    def test_rejects_agent_key_mismatch(self) -> None:
        config = valid_config()
        config["HL_W4_AGENT_ADDRESS"] = config["HL_W3_AGENT_ADDRESS"]
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_config(config)

    def test_rejects_cap_above_hard_limit(self) -> None:
        config = valid_config()
        config["HL_MAX_ORDER_NOTIONAL_USD"] = "15.01"
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_config(config)

    def test_signed_mode_requires_explicit_true(self) -> None:
        config = valid_config()
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_config(config, signed_actions_required=True)
        config["HL_ENABLE_SIGNED_ACTIONS"] = "true"
        result = preflight.validate_config(config, signed_actions_required=True)
        self.assertEqual(len(result.wallets), 4)

    def test_redaction_guard_rejects_secret(self) -> None:
        validated = preflight.validate_config(valid_config())
        with self.assertRaises(preflight.PreflightError):
            preflight.ensure_no_secrets(validated.wallets[0].private_key, validated.wallets)


class FakeInfo:
    def __init__(self, master: str, agent: str, linked_master: str | None = None) -> None:
        self.master = master
        self.agent = agent
        self.linked_master = linked_master or master

    def user_role(self, address: str):
        if address == self.master:
            return {"role": "user"}
        if address == self.agent:
            return {"role": "agent", "data": {"user": self.linked_master}}
        return {"role": "missing"}

    def spot_user_state(self, address: str):
        return {"balances": [{"coin": "USDC", "total": "100"}]}

    def user_state(self, address: str):
        return {
            "marginSummary": {"accountValue": "100"},
            "crossMarginSummary": {"accountValue": "100"},
            "withdrawable": "100",
            "assetPositions": [],
        }

    def frontend_open_orders(self, address: str):
        return []


class PublicRelationshipTests(unittest.TestCase):
    def test_records_public_state_after_relationship_match(self) -> None:
        wallet = preflight.validate_config(valid_config()).wallets[0]
        result = preflight.public_wallet_state(FakeInfo(wallet.master, wallet.agent), wallet)
        self.assertTrue(result["relationship"]["matchesConfiguredMaster"])
        self.assertEqual(result["spotBalances"][0]["coin"], "USDC")

    def test_rejects_agent_linked_to_another_master(self) -> None:
        wallet = preflight.validate_config(valid_config()).wallets[0]
        wrong_master = "0x" + "12" * 20
        with self.assertRaises(preflight.PreflightError):
            preflight.public_wallet_state(
                FakeInfo(wallet.master, wallet.agent, linked_master=wrong_master), wallet
            )


if __name__ == "__main__":
    unittest.main()

