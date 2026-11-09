"""Route 06: trace a public HyperEVM -> HyperEVM transfer.

This program is read-only.  It accepts a known transaction hash, retrieves
the transaction/receipt/block from the public HyperEVM JSON-RPC endpoint, and
verifies either a native HYPE transfer or one ERC-20 Transfer log.  It never
loads a key, signs a transaction, or submits an action.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from common import (
    NETWORKS,
    PublicClient,
    TraceError,
    decimal_amount,
    normalize_address,
    normalize_hash,
    parse_erc20_transfers,
    rpc_quantity,
    write_json,
)


WEI_PER_HYPE = Decimal(10**18)


def analyze_native_transfer(
    bundle: dict[str, Any],
    *,
    expected_from: str | None = None,
    expected_to: str | None = None,
    expected_amount: Decimal | None = None,
) -> dict[str, Any]:
    """Validate an ordinary native-HYPE transfer from an RPC bundle."""
    tx = bundle["transaction"]
    receipt = bundle["receipt"]
    sender = normalize_address(str(tx.get("from", "")))
    recipient = normalize_address(str(tx.get("to", "")))
    amount = Decimal(rpc_quantity(tx.get("value"))) / WEI_PER_HYPE
    receipt_ok = rpc_quantity(receipt.get("status", "0x0")) == 1

    checks = {
        "receiptSucceeded": receipt_ok,
        "fromMatches": (
            expected_from is None
            or sender == normalize_address(expected_from)
        ),
        "toMatches": (
            expected_to is None
            or recipient == normalize_address(expected_to)
        ),
        "amountMatches": expected_amount is None or amount == expected_amount,
    }
    return {
        "asset": {"kind": "native", "symbol": "HYPE", "decimals": 18},
        "transfer": {
            "transactionHash": str(tx.get("hash", "")).lower(),
            "from": sender,
            "to": recipient,
            "amount": str(amount),
            "blockNumber": rpc_quantity(tx.get("blockNumber")),
            "blockTimeMs": bundle.get("blockTimeMs"),
        },
        "checks": checks,
        "classification": (
            "EXACT_HASH_MATCH" if all(checks.values()) else "MISMATCH"
        ),
    }


def analyze_erc20_transfer(
    bundle: dict[str, Any],
    *,
    token_address: str,
    token_decimals: int,
    expected_from: str | None = None,
    expected_to: str | None = None,
    expected_amount: Decimal | None = None,
) -> dict[str, Any]:
    """Select the ERC-20 transfer log matching the supplied expectations."""
    receipt = bundle["receipt"]
    rows = parse_erc20_transfers(receipt, token_address, token_decimals)
    candidates = []
    for row in rows:
        if expected_from is not None and row["from"] != normalize_address(expected_from):
            continue
        if expected_to is not None and row["to"] != normalize_address(expected_to):
            continue
        if expected_amount is not None and Decimal(row["amount"]) != expected_amount:
            continue
        candidates.append(row)

    receipt_ok = rpc_quantity(receipt.get("status", "0x0")) == 1
    classification = (
        "EXACT_HASH_MATCH"
        if receipt_ok and len(candidates) == 1
        else ("AMBIGUOUS" if len(candidates) > 1 else "MISMATCH")
    )
    return {
        "asset": {
            "kind": "erc20",
            "tokenAddress": normalize_address(token_address),
            "decimals": token_decimals,
        },
        "receiptSucceeded": receipt_ok,
        "allTransferLogs": rows,
        "matchingTransferLogs": candidates,
        "candidateCount": len(candidates),
        "classification": classification,
    }


def trace(args: argparse.Namespace) -> dict[str, Any]:
    network = NETWORKS[args.network]
    client = PublicClient(network)
    tx_hash = normalize_hash(args.tx_hash)
    bundle = client.evm_transaction_bundle(network.hyperevm_rpc_url, tx_hash)
    expected_amount = (
        decimal_amount(args.expected_amount)
        if args.expected_amount is not None
        else None
    )

    if args.token_address:
        analysis = analyze_erc20_transfer(
            bundle,
            token_address=args.token_address,
            token_decimals=args.token_decimals,
            expected_from=args.expected_from,
            expected_to=args.expected_to,
            expected_amount=expected_amount,
        )
    else:
        analysis = analyze_native_transfer(
            bundle,
            expected_from=args.expected_from,
            expected_to=args.expected_to,
            expected_amount=expected_amount,
        )

    return {
        "schemaVersion": 1,
        "route": 6,
        "traceType": "HyperEVM -> HyperEVM",
        "network": network.name,
        "rpcUrl": network.hyperevm_rpc_url,
        "query": {
            "transactionHash": tx_hash,
            "expectedFrom": args.expected_from,
            "expectedTo": args.expected_to,
            "expectedAmount": args.expected_amount,
            "tokenAddress": args.token_address,
            "tokenDecimals": args.token_decimals if args.token_address else None,
        },
        "analysis": analysis,
        "raw": bundle,
        "classification": analysis["classification"],
        "matchingRule": (
            "Exact transaction hash, successful receipt, and matching "
            "from/to/value (or ERC-20 Transfer log)."
        ),
        "limitations": [
            "A transaction proves the public on-chain transfer, not common control of the wallets.",
            "Historical before/after balances may require an archive RPC; this tracer uses transaction evidence.",
        ],
        "signedActions": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=sorted(NETWORKS), required=True)
    parser.add_argument("--tx-hash", required=True)
    parser.add_argument("--expected-from")
    parser.add_argument("--expected-to")
    parser.add_argument("--expected-amount")
    parser.add_argument(
        "--token-address",
        help="ERC-20 contract. Omit for a native HYPE transfer.",
    )
    parser.add_argument(
        "--token-decimals",
        type=int,
        default=18,
        help="Required token decimals when --token-address is used.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not 0 <= args.token_decimals <= 255:
        raise SystemExit("--token-decimals must be between 0 and 255.")
    try:
        report = trace(args)
    except (TraceError, requests.RequestException, KeyError) as exc:
        raise SystemExit(f"TRACE FAILED: {exc}") from exc
    write_json(args.output, report)
    print(json.dumps({
        "route": 6,
        "classification": report["classification"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
