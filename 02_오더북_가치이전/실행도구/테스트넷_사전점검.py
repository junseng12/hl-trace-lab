"""Read-only Hyperliquid Testnet preflight for four Master/Agent pairs.

This module deliberately imports only the read-only Info client. It never
constructs Exchange, signs an action, or sends a private key over the network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
VENDOR = REPO_ROOT / "hyperliquid_mainnet_agent" / "vendor"
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))

from eth_account import Account  # noqa: E402
from hyperliquid.info import Info  # noqa: E402
from hyperliquid.utils import constants  # noqa: E402


EXPECTED_NETWORK = "testnet"
EXPECTED_API_URL = constants.TESTNET_API_URL.rstrip("/")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SLOT_NAMES = ("W1", "W2", "W3", "W4")

# Future signed runners must remain at or below these values. This preflight
# validates and records them but cannot execute any signed action.
HARD_CAPS: dict[str, Decimal | int] = {
    "HL_MAX_ORDER_NOTIONAL_USD": Decimal("15"),
    "HL_MAX_RUN_NOTIONAL_USD": Decimal("40"),
    "HL_MAX_OPEN_POSITION_USD": Decimal("20"),
    "HL_MAX_ORDERS_PER_RUN": 20,
    "HL_MAX_SLIPPAGE_BPS": 100,
}


# 1) Parse and validate local configuration without printing any secret.
class PreflightError(Exception):
    """Safe, non-secret-bearing validation error."""


@dataclass(frozen=True)
class WalletConfig:
    slot: str
    master: str
    agent: str
    private_key: str


@dataclass(frozen=True)
class ValidatedConfig:
    api_url: str
    wallets: tuple[WalletConfig, ...]
    safety_caps: dict[str, str | int]


def read_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise PreflightError(".env.local is missing. Copy .env.local.example first.")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PreflightError(f"Invalid config syntax at line {line_number}.")
        name, value = line.split("=", 1)
        name = name.strip()
        if not name:
            raise PreflightError(f"Empty config name at line {line_number}.")
        if name in values:
            raise PreflightError(f"Duplicate config field: {name}.")
        values[name] = value.strip()
    return values


def require(config: dict[str, str], name: str) -> str:
    value = config.get(name, "").strip()
    if not value:
        raise PreflightError(f"Required config field is missing: {name}.")
    return value


def normalize_address(value: str, field: str) -> str:
    if not ADDRESS_RE.fullmatch(value):
        raise PreflightError(f"{field} is not a valid 20-byte 0x address.")
    return value.lower()


def derive_agent_address(private_key: str, slot: str) -> str:
    key = private_key[2:] if private_key.startswith("0x") else private_key
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        raise PreflightError(f"{slot} Agent private key is not a 32-byte hexadecimal value.")
    try:
        return Account.from_key(bytes.fromhex(key)).address.lower()
    except (ValueError, TypeError) as exc:
        raise PreflightError(f"{slot} Agent private key is invalid.") from exc


def parse_cap(config: dict[str, str], name: str) -> str | int:
    raw = require(config, name)
    hard = HARD_CAPS[name]
    if isinstance(hard, int):
        try:
            value = int(raw)
        except ValueError as exc:
            raise PreflightError(f"{name} must be an integer.") from exc
        if value <= 0 or value > hard:
            raise PreflightError(f"{name} must be between 1 and hard cap {hard}.")
        return value

    try:
        value_decimal = Decimal(raw)
    except InvalidOperation as exc:
        raise PreflightError(f"{name} must be numeric.") from exc
    if not value_decimal.is_finite() or value_decimal <= 0 or value_decimal > hard:
        raise PreflightError(f"{name} must be positive and at most hard cap {hard}.")
    return format(value_decimal, "f")


def validate_config(
    config: dict[str, str], *, signed_actions_required: bool = False
) -> ValidatedConfig:
    """Enforce Testnet-only mode, unique wallets, valid Agent keys and caps."""
    network = require(config, "HL_NETWORK").lower()
    api_url = require(config, "HL_API_URL").rstrip("/")
    signing_enabled = require(config, "HL_ENABLE_SIGNED_ACTIONS").lower()

    if network != EXPECTED_NETWORK:
        raise PreflightError("Only HL_NETWORK=testnet is permitted.")
    if api_url != EXPECTED_API_URL:
        raise PreflightError("Only the official Hyperliquid Testnet API URL is permitted.")
    expected_signing = "true" if signed_actions_required else "false"
    if signing_enabled != expected_signing:
        if signed_actions_required:
            raise PreflightError("Signed runner requires HL_ENABLE_SIGNED_ACTIONS=true.")
        raise PreflightError("This read-only preflight requires HL_ENABLE_SIGNED_ACTIONS=false.")

    wallets: list[WalletConfig] = []
    masters: set[str] = set()
    agents: set[str] = set()
    all_addresses: set[str] = set()

    for slot in SLOT_NAMES:
        master = normalize_address(require(config, f"HL_{slot}_MASTER_ADDRESS"), f"HL_{slot}_MASTER_ADDRESS")
        agent = normalize_address(require(config, f"HL_{slot}_AGENT_ADDRESS"), f"HL_{slot}_AGENT_ADDRESS")
        private_key = require(config, f"HL_{slot}_AGENT_PRIVATE_KEY")
        derived = derive_agent_address(private_key, slot)
        if derived != agent:
            raise PreflightError(f"{slot} Agent key does not derive to the configured Agent address.")
        if master == agent:
            raise PreflightError(f"{slot} Master and Agent addresses must differ.")
        if master in masters:
            raise PreflightError("Master addresses must be unique across W1-W4.")
        if agent in agents:
            raise PreflightError("Agent addresses must be unique across W1-W4.")
        if master in all_addresses or agent in all_addresses:
            raise PreflightError("No Master or Agent address may be reused in another slot.")
        masters.add(master)
        agents.add(agent)
        all_addresses.update((master, agent))
        wallets.append(WalletConfig(slot, master, agent, private_key))

    caps = {name: parse_cap(config, name) for name in HARD_CAPS}
    return ValidatedConfig(api_url, tuple(wallets), caps)


# 2) Verify the public Master-Agent relationship and current account state.
def public_wallet_state(info: Info, wallet: WalletConfig) -> dict[str, Any]:
    master_role = info.user_role(wallet.master)
    agent_role = info.user_role(wallet.agent)

    if not isinstance(master_role, dict) or master_role.get("role") != "user":
        raise PreflightError(f"{wallet.slot} configured Master is not a Testnet user account.")
    linked_master = ""
    if isinstance(agent_role, dict) and agent_role.get("role") == "agent":
        data = agent_role.get("data")
        if isinstance(data, dict):
            linked_master = str(data.get("user", "")).lower()
    if linked_master != wallet.master:
        raise PreflightError(f"{wallet.slot} Agent userRole does not link to its configured Master.")

    spot_state = info.spot_user_state(wallet.master)
    perp_state = info.user_state(wallet.master)
    open_orders = info.frontend_open_orders(wallet.master)

    balances = spot_state.get("balances", []) if isinstance(spot_state, dict) else []
    positions = perp_state.get("assetPositions", []) if isinstance(perp_state, dict) else []
    margin_summary = perp_state.get("marginSummary", {}) if isinstance(perp_state, dict) else {}
    cross_margin_summary = perp_state.get("crossMarginSummary", {}) if isinstance(perp_state, dict) else {}
    withdrawable = perp_state.get("withdrawable") if isinstance(perp_state, dict) else None

    return {
        "slot": wallet.slot,
        "master": wallet.master,
        "agent": wallet.agent,
        "derivedAgent": wallet.agent,
        "relationship": {
            "masterRole": master_role.get("role"),
            "agentRole": agent_role.get("role"),
            "linkedMaster": linked_master,
            "matchesConfiguredMaster": True,
        },
        "spotBalances": balances,
        "openOrders": open_orders,
        "perp": {
            "marginSummary": margin_summary,
            "crossMarginSummary": cross_margin_summary,
            "withdrawable": withdrawable,
            "openPositions": positions,
        },
    }


# 3) Redact-by-construction: refuse to write any artifact containing a key.
def ensure_no_secrets(serialized: str, wallets: tuple[WalletConfig, ...]) -> None:
    lower_output = serialized.lower()
    for wallet in wallets:
        raw = wallet.private_key.lower()
        bare = raw[2:] if raw.startswith("0x") else raw
        if raw in lower_output or bare in lower_output:
            raise PreflightError("Secret-redaction invariant failed; no report was written.")


def write_report(report: dict[str, Any], output_dir: Path, wallets: tuple[WalletConfig, ...]) -> Path:
    serialized = json.dumps(report, indent=2, ensure_ascii=False)
    ensure_no_secrets(serialized, wallets)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = output_dir / f"사전점검_{stamp}.json"
    destination.write_text(serialized + "\n", encoding="utf-8")
    return destination


# 4) Assemble the read-only preflight report.
def run(env_path: Path, output_dir: Path) -> Path:
    config = validate_config(read_config(env_path))
    info = Info(config.api_url, skip_ws=True)
    wallet_states = [public_wallet_state(info, wallet) for wallet in config.wallets]
    report = {
        "schemaVersion": 1,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "network": EXPECTED_NETWORK,
        "apiUrl": config.api_url,
        "readOnly": True,
        "signedActionsEnabled": False,
        "status": "PASS",
        "safetyCaps": config.safety_caps,
        "wallets": wallet_states,
    }
    return write_report(report, output_dir, config.wallets)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=REPO_ROOT / ".env.local")
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "결과" / "사전점검"
    )
    args = parser.parse_args()

    try:
        destination = run(args.env, args.output_dir)
    except PreflightError as exc:
        print(f"Preflight FAIL: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        print("Preflight FAIL: a read-only Testnet API request failed; no secret was printed.", file=sys.stderr)
        raise SystemExit(3) from None

    print("Preflight PASS: all four Agent relationships and public account states were verified.")
    print(f"Redacted report: {destination.resolve()}")


if __name__ == "__main__":
    main()

