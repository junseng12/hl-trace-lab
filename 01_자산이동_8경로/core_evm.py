"""Shared implementation for report routes 4 and 5.

Read-only HyperCore <-> HyperEVM HYPE transfer tracer.

This CLI never loads a private key and never sends a transaction.  It joins
public HyperCore ledger updates with HyperEVM standard RPC data and the custom
``eth_getSystemTxsByBlockNumber`` method.

Supported, deliberately narrow scope:

* HYPE HyperCore -> HyperEVM: Core ``send`` to 0x2222...2222 followed by a
  protocol-created System TX from that address to the same user.
* HYPE HyperEVM -> HyperCore: user-signed EVM value transfer to 0x2222...2222
  followed by a Core ``spotTransfer`` credit from the system address.

Linked ERC-20 spot assets use token-specific system addresses and decimal
rules.  They are intentionally excluded until a verified token registry is
added; never substitute the HYPE system address for another token.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import requests


HYPE_SYSTEM_ADDRESS = "0x2222222222222222222222222222222222222222"
WEI_PER_HYPE = Decimal(10**18)


@dataclass(frozen=True)
class Network:
    name: str
    api_url: str
    rpc_url: str
    chain_id: int


NETWORKS = {
    "mainnet": Network(
        "mainnet",
        "https://api.hyperliquid.xyz",
        "https://rpc.hyperliquid.xyz/evm",
        999,
    ),
    "testnet": Network(
        "testnet",
        "https://api.hyperliquid-testnet.xyz",
        "https://rpc.hyperliquid-testnet.xyz/evm",
        998,
    ),
}


class TraceError(RuntimeError):
    """Expected tracing or public-data validation error."""


def normalize_address(value: str) -> str:
    value = value.strip().lower()
    if not value.startswith("0x") or len(value) != 42:
        raise TraceError(f"Invalid address: {value!r}")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise TraceError(f"Invalid address: {value!r}") from exc
    return value


def normalize_hash(value: str) -> str:
    value = value.strip().lower()
    if not value.startswith("0x") or len(value) != 66:
        raise TraceError(f"Invalid 32-byte hash: {value!r}")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise TraceError(f"Invalid 32-byte hash: {value!r}") from exc
    return value


def decimal_amount(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise TraceError(f"Invalid amount: {value!r}") from exc
    if not result.is_finite() or result <= 0:
        raise TraceError("Amount must be a positive finite number.")
    return result


def quantity(value: Any) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise TraceError(f"Unexpected RPC quantity: {value!r}")
    return int(value, 16)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class PublicClient:
    def __init__(self, network: Network, rpc_delay_ms: int = 650) -> None:
        self.network = network
        self.rpc_delay = rpc_delay_ms / 1000
        self.request_id = 0
        self.session = requests.Session()

    def info(self, payload: dict[str, Any]) -> Any:
        response = self.session.post(
            self.network.api_url.rstrip("/") + "/info",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def rpc(self, method: str, params: list[Any]) -> Any:
        self.request_id += 1
        response = self.session.post(
            self.network.rpc_url,
            json={"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error") is not None:
            raise TraceError(f"RPC {method} failed: {payload['error']}")
        if self.rpc_delay:
            time.sleep(self.rpc_delay)
        return payload.get("result")

    def ledger_updates(self, user: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        result = self.info({
            "type": "userNonFundingLedgerUpdates",
            "user": user,
            "startTime": start_ms,
            "endTime": end_ms,
        })
        if not isinstance(result, list):
            raise TraceError("Unexpected userNonFundingLedgerUpdates response.")
        return [item for item in result if isinstance(item, dict)]

    def block(self, number: int, full_transactions: bool = False) -> dict[str, Any]:
        result = self.rpc("eth_getBlockByNumber", [hex(number), full_transactions])
        if not isinstance(result, dict):
            raise TraceError(f"EVM block {number} was not returned.")
        return result


def delta_of(update: dict[str, Any]) -> dict[str, Any]:
    delta = update.get("delta")
    return delta if isinstance(delta, dict) else {}


def core_to_evm_updates(
    updates: Iterable[dict[str, Any]],
    user: str,
    amount: Decimal | None,
    core_hash: str | None,
) -> list[dict[str, Any]]:
    matches = []
    for item in updates:
        delta = delta_of(item)
        if str(delta.get("type", "")).lower() != "send":
            continue
        if str(delta.get("user", "")).lower() != user:
            continue
        if str(delta.get("destination", "")).lower() != HYPE_SYSTEM_ADDRESS:
            continue
        if str(delta.get("token", "")).upper() != "HYPE":
            continue
        if amount is not None and Decimal(str(delta.get("amount", "0"))) != amount:
            continue
        if core_hash is not None and str(item.get("hash", "")).lower() != core_hash:
            continue
        matches.append(item)
    return matches


def evm_to_core_updates(
    updates: Iterable[dict[str, Any]], user: str, amount: Decimal
) -> list[dict[str, Any]]:
    matches = []
    for item in updates:
        delta = delta_of(item)
        if str(delta.get("type", "")).lower() not in {"spottransfer", "send"}:
            continue
        if str(delta.get("user", "")).lower() != HYPE_SYSTEM_ADDRESS:
            continue
        if str(delta.get("destination", "")).lower() != user:
            continue
        if str(delta.get("token", "")).upper() != "HYPE":
            continue
        if Decimal(str(delta.get("amount", "0"))) != amount:
            continue
        matches.append(item)
    return matches


def first_block_at_or_after(client: PublicClient, target_seconds: int) -> int:
    latest = quantity(client.rpc("eth_blockNumber", []))
    latest_block = client.block(latest)
    if quantity(latest_block["timestamp"]) < target_seconds:
        return latest

    low, high = 0, latest
    while low < high:
        mid = (low + high) // 2
        block = client.block(mid)
        if quantity(block["timestamp"]) < target_seconds:
            low = mid + 1
        else:
            high = mid
    return low


def block_range_for_time(
    client: PublicClient, event_ms: int, window_seconds: int
) -> tuple[int, int]:
    start = first_block_at_or_after(client, event_ms // 1000 - window_seconds)
    end = first_block_at_or_after(client, event_ms // 1000 + window_seconds)
    if end < start:
        start, end = end, start
    if end - start > 300:
        raise TraceError(
            f"Refusing to scan {end - start + 1} blocks. Reduce --window-seconds."
        )
    return max(0, start - 1), end + 1


def system_tx_candidates(
    client: PublicClient,
    start_block: int,
    end_block: int,
    recipient: str,
    amount: Decimal,
) -> list[dict[str, Any]]:
    expected_wei = int(amount * WEI_PER_HYPE)
    found = []
    for block_number in range(start_block, end_block + 1):
        rows = client.rpc("eth_getSystemTxsByBlockNumber", [hex(block_number)])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("from", "")).lower() != HYPE_SYSTEM_ADDRESS:
                continue
            if str(row.get("to", "")).lower() != recipient:
                continue
            try:
                value = quantity(row.get("value"))
            except TraceError:
                continue
            if value != expected_wei:
                continue
            block = client.block(block_number)
            found.append({
                "blockNumberDecimal": block_number,
                "blockTimestamp": quantity(block["timestamp"]),
                **row,
            })
    return found


def standard_visibility(client: PublicClient, system_tx: dict[str, Any]) -> dict[str, Any]:
    tx_hash = str(system_tx.get("hash", ""))
    block_number = int(system_tx["blockNumberDecimal"])
    standard_block = client.block(block_number, full_transactions=False)
    listed = [str(x).lower() for x in standard_block.get("transactions", [])]
    return {
        "inEthGetBlockByNumberTransactions": tx_hash.lower() in listed,
        "ethGetTransactionByHash": client.rpc("eth_getTransactionByHash", [tx_hash]),
        "ethGetTransactionReceipt": client.rpc("eth_getTransactionReceipt", [tx_hash]),
    }


def trace_core_to_evm(args: argparse.Namespace) -> dict[str, Any]:
    network = NETWORKS[args.network]
    client = PublicClient(network, args.rpc_delay_ms)
    user = normalize_address(args.user)
    amount = decimal_amount(args.amount) if args.amount is not None else None
    core_hash = normalize_hash(args.core_hash) if args.core_hash else None
    if amount is None and core_hash is None:
        raise TraceError("Provide at least one of --amount or --core-hash.")

    updates = client.ledger_updates(user, args.start_time_ms, args.end_time_ms)
    core_matches = core_to_evm_updates(updates, user, amount, core_hash)
    results = []
    for core in core_matches:
        core_amount = Decimal(str(delta_of(core)["amount"]))
        start_block, end_block = block_range_for_time(
            client, int(core["time"]), args.window_seconds
        )
        candidates = system_tx_candidates(
            client, start_block, end_block, user, core_amount
        )
        rendered = []
        for candidate in candidates:
            rendered.append({
                **candidate,
                "timeDeltaMs": candidate["blockTimestamp"] * 1000 - int(core["time"]),
                "standardVisibility": standard_visibility(client, candidate),
            })
        results.append({
            "coreLedgerUpdate": core,
            "scanBlocks": [start_block, end_block],
            "systemTxCandidates": rendered,
            "uniqueMatch": len(rendered) == 1,
        })

    return {
        "schemaVersion": 1,
        "traceType": "HYPE HyperCore -> HyperEVM",
        "network": network.name,
        "user": user,
        "query": {
            "startTimeMs": args.start_time_ms,
            "endTimeMs": args.end_time_ms,
            "amountHype": str(amount) if amount is not None else None,
            "coreHash": core_hash,
            "windowSeconds": args.window_seconds,
        },
        "coreCandidates": len(core_matches),
        "results": results,
        "classification": (
            "UNIQUE_MATCH" if len(results) == 1 and results[0]["uniqueMatch"]
            else "AMBIGUOUS_OR_NOT_FOUND"
        ),
        "signedActions": False,
    }


def trace_evm_to_core(args: argparse.Namespace) -> dict[str, Any]:
    network = NETWORKS[args.network]
    client = PublicClient(network, args.rpc_delay_ms)
    tx_hash = normalize_hash(args.evm_tx_hash)
    tx = client.rpc("eth_getTransactionByHash", [tx_hash])
    receipt = client.rpc("eth_getTransactionReceipt", [tx_hash])
    if not isinstance(tx, dict) or not isinstance(receipt, dict):
        raise TraceError("The standard EVM transaction or receipt was not found.")
    if quantity(receipt.get("status", "0x0")) != 1:
        raise TraceError("The EVM transaction did not succeed.")

    user = normalize_address(args.user or str(tx.get("from", "")))
    if str(tx.get("from", "")).lower() != user:
        raise TraceError("--user does not match transaction.from.")
    if str(tx.get("to", "")).lower() != HYPE_SYSTEM_ADDRESS:
        raise TraceError("Transaction destination is not the HYPE system address.")

    amount = Decimal(quantity(tx.get("value"))) / WEI_PER_HYPE
    block_number = quantity(tx["blockNumber"])
    block = client.block(block_number)
    evm_time_ms = quantity(block["timestamp"]) * 1000
    start_ms = evm_time_ms - args.window_seconds * 1000
    end_ms = evm_time_ms + args.window_seconds * 1000
    updates = client.ledger_updates(user, start_ms, end_ms)
    core_matches = evm_to_core_updates(updates, user, amount)
    rendered = [
        {**row, "timeDeltaMs": int(row["time"]) - evm_time_ms}
        for row in core_matches
    ]

    return {
        "schemaVersion": 1,
        "traceType": "HYPE HyperEVM -> HyperCore",
        "network": network.name,
        "user": user,
        "evmTransaction": tx,
        "evmReceipt": receipt,
        "evmBlockTimestampMs": evm_time_ms,
        "amountHype": str(amount),
        "coreCreditCandidates": rendered,
        "classification": "UNIQUE_MATCH" if len(rendered) == 1 else "AMBIGUOUS_OR_NOT_FOUND",
        "signedActions": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    core = sub.add_parser("core-to-evm", help="Join a Core send with a System TX")
    core.add_argument("--network", choices=sorted(NETWORKS), required=True)
    core.add_argument("--user", required=True)
    core.add_argument("--start-time-ms", type=int, required=True)
    core.add_argument("--end-time-ms", type=int, required=True)
    core.add_argument("--amount", help="Exact HYPE amount")
    core.add_argument("--core-hash")
    core.add_argument("--window-seconds", type=int, default=15)
    core.add_argument("--rpc-delay-ms", type=int, default=650)
    core.add_argument("--output", type=Path, required=True)

    evm = sub.add_parser("evm-to-core", help="Join a user EVM TX with a Core credit")
    evm.add_argument("--network", choices=sorted(NETWORKS), required=True)
    evm.add_argument("--evm-tx-hash", required=True)
    evm.add_argument("--user", help="Optional; defaults to transaction.from")
    evm.add_argument("--window-seconds", type=int, default=30)
    evm.add_argument("--rpc-delay-ms", type=int, default=650)
    evm.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not 1 <= args.window_seconds <= 120:
        raise SystemExit("--window-seconds must be between 1 and 120.")
    if not 0 <= args.rpc_delay_ms <= 5000:
        raise SystemExit("--rpc-delay-ms must be between 0 and 5000.")
    if args.command == "core-to-evm" and args.end_time_ms < args.start_time_ms:
        raise SystemExit("--end-time-ms must be >= --start-time-ms.")

    try:
        report = trace_core_to_evm(args) if args.command == "core-to-evm" else trace_evm_to_core(args)
    except (TraceError, requests.RequestException) as exc:
        raise SystemExit(f"TRACE FAILED: {exc}") from exc
    write_json(args.output, report)
    print(json.dumps({
        "classification": report["classification"],
        "traceType": report["traceType"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
