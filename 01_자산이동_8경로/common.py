"""Shared read-only helpers for the eight Hyperliquid asset-path tracers.

The helpers in this module only call public Hyperliquid Info endpoints and
public JSON-RPC endpoints.  They never load a private key, sign an action, or
broadcast a transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests


TRANSFER_TOPIC0 = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
ARBITRUM_MAINNET_RPC = "https://arb1.arbitrum.io/rpc"
ARBITRUM_USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"


@dataclass(frozen=True)
class Network:
    name: str
    info_url: str
    hyperevm_rpc_url: str
    chain_id: int


NETWORKS = {
    "mainnet": Network(
        "mainnet", "https://api.hyperliquid.xyz/info",
        "https://rpc.hyperliquid.xyz/evm", 999,
    ),
    "testnet": Network(
        "testnet", "https://api.hyperliquid-testnet.xyz/info",
        "https://rpc.hyperliquid-testnet.xyz/evm", 998,
    ),
}


class TraceError(RuntimeError):
    """Expected validation or public-data error."""


def normalize_address(value: str) -> str:
    value = value.strip().lower()
    if not value.startswith("0x") or len(value) != 42:
        raise TraceError(f"Invalid EVM address: {value!r}")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise TraceError(f"Invalid EVM address: {value!r}") from exc
    return value


def normalize_hash(value: str, *, allow_without_prefix: bool = False) -> str:
    value = value.strip().lower()
    if allow_without_prefix and not value.startswith("0x"):
        value = "0x" + value
    if not value.startswith("0x") or len(value) != 66:
        raise TraceError(f"Invalid 32-byte hash: {value!r}")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise TraceError(f"Invalid 32-byte hash: {value!r}") from exc
    return value


def decimal_amount(value: Any, *, allow_zero: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TraceError(f"Invalid amount: {value!r}") from exc
    minimum_ok = result >= 0 if allow_zero else result > 0
    if not result.is_finite() or not minimum_ok:
        raise TraceError(f"Amount must be {'non-negative' if allow_zero else 'positive'}.")
    return result


def rpc_quantity(value: Any) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise TraceError(f"Unexpected JSON-RPC quantity: {value!r}")
    return int(value, 16)


def classify_candidate_count(count: int) -> str:
    if count == 1:
        return "UNIQUE_MATCH"
    if count == 0:
        return "NOT_FOUND"
    return "AMBIGUOUS"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def delta_of(update: dict[str, Any]) -> dict[str, Any]:
    delta = update.get("delta")
    return delta if isinstance(delta, dict) else {}


class PublicClient:
    """Small public HTTP client shared by every route tracer."""

    def __init__(self, network: Network, timeout_seconds: int = 30) -> None:
        self.network = network
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.request_id = 0

    def info(self, payload: dict[str, Any]) -> Any:
        response = self.session.post(
            self.network.info_url, json=payload, timeout=self.timeout_seconds
        )
        response.raise_for_status()
        return response.json()

    def rpc(self, rpc_url: str, method: str, params: list[Any]) -> Any:
        self.request_id += 1
        response = self.session.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": self.request_id,
                  "method": method, "params": params},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error") is not None:
            raise TraceError(f"RPC {method} failed: {payload['error']}")
        return payload.get("result")

    def ledger_updates(
        self, user: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        result = self.info({
            "type": "userNonFundingLedgerUpdates",
            "user": normalize_address(user),
            "startTime": int(start_ms),
            "endTime": int(end_ms),
        })
        if not isinstance(result, list):
            raise TraceError("Unexpected userNonFundingLedgerUpdates response.")
        return [item for item in result if isinstance(item, dict)]

    def evm_transaction_bundle(self, rpc_url: str, tx_hash: str) -> dict[str, Any]:
        tx_hash = normalize_hash(tx_hash)
        transaction = self.rpc(rpc_url, "eth_getTransactionByHash", [tx_hash])
        receipt = self.rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash])
        if not isinstance(transaction, dict) or not isinstance(receipt, dict):
            raise TraceError(f"Transaction or receipt not found: {tx_hash}")
        block_number = rpc_quantity(transaction.get("blockNumber"))
        block = self.rpc(rpc_url, "eth_getBlockByNumber", [hex(block_number), False])
        if not isinstance(block, dict):
            raise TraceError(f"Block not found for transaction: {tx_hash}")
        return {
            "transaction": transaction,
            "receipt": receipt,
            "block": block,
            "blockTimeMs": rpc_quantity(block.get("timestamp")) * 1000,
        }


def parse_erc20_transfers(
    receipt: dict[str, Any], token_address: str, decimals: int
) -> list[dict[str, Any]]:
    """Decode standard ERC-20 Transfer logs from one receipt."""
    token = normalize_address(token_address)
    if decimals < 0 or decimals > 255:
        raise TraceError("Token decimals must be between 0 and 255.")
    rows: list[dict[str, Any]] = []
    for log in receipt.get("logs", []):
        if not isinstance(log, dict):
            continue
        topics = log.get("topics")
        if (
            str(log.get("address", "")).lower() != token
            or not isinstance(topics, list)
            or len(topics) < 3
            or str(topics[0]).lower() != TRANSFER_TOPIC0
        ):
            continue
        try:
            from_address = normalize_address("0x" + str(topics[1])[-40:])
            to_address = normalize_address("0x" + str(topics[2])[-40:])
            amount_raw = int(str(log.get("data", "0x0")), 16)
        except (TraceError, ValueError):
            continue
        rows.append({
            "transactionHash": str(log.get("transactionHash", "")).lower(),
            "logIndex": log.get("logIndex"),
            "token": token,
            "from": from_address,
            "to": to_address,
            "amountRaw": str(amount_raw),
            "amount": str(Decimal(amount_raw) / (Decimal(10) ** decimals)),
        })
    return rows


def resolved_time_window(
    block_time_ms: int,
    start_time_ms: int | None,
    end_time_ms: int | None,
    window_seconds: int,
) -> tuple[int, int]:
    if window_seconds <= 0:
        raise TraceError("window-seconds must be positive.")
    start = start_time_ms if start_time_ms is not None else block_time_ms - window_seconds * 1000
    end = end_time_ms if end_time_ms is not None else block_time_ms + window_seconds * 1000
    if start > end:
        raise TraceError("start-time-ms must not be later than end-time-ms.")
    return int(start), int(end)
