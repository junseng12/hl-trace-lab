"""Route 1: trace an external Arbitrum USDC deposit into HyperCore.

The verified path uses the same transaction hash in the Arbitrum transaction
and the HyperCore deposit ledger update.  This program only reads public data.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
from typing import Any

from common import (
    ARBITRUM_MAINNET_RPC,
    ARBITRUM_USDC,
    NETWORKS,
    PublicClient,
    TraceError,
    classify_candidate_count,
    decimal_amount,
    delta_of,
    normalize_address,
    normalize_hash,
    parse_erc20_transfers,
    resolved_time_window,
    rpc_quantity,
    write_json,
)


def find_deposit_candidates(
    transfers: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    *,
    user: str,
    tx_hash: str,
    amount: Decimal | None = None,
    bridge_address: str | None = None,
) -> list[dict[str, Any]]:
    """Return only pairs with the same hash, user and exact USDC amount."""
    user = normalize_address(user)
    tx_hash = normalize_hash(tx_hash)
    bridge = normalize_address(bridge_address) if bridge_address else None
    external_rows = []
    for row in transfers:
        try:
            row_amount = Decimal(str(row.get("amount")))
        except Exception:
            continue
        event_hash = str(row.get("transactionHash", "")).lower()
        if event_hash and event_hash != tx_hash:
            continue
        if row.get("from") != user:
            continue
        if bridge is not None and row.get("to") != bridge:
            continue
        if amount is not None and row_amount != amount:
            continue
        external_rows.append(row)
    candidates: list[dict[str, Any]] = []
    for update in updates:
        delta = delta_of(update)
        if str(delta.get("type", "")).lower() != "deposit":
            continue
        try:
            core_amount = abs(Decimal(str(delta.get("usdc"))))
        except Exception:
            continue
        if str(update.get("hash", "")).lower() != tx_hash:
            continue
        for external in external_rows:
            if Decimal(str(external["amount"])) != core_amount:
                continue
            candidates.append({
                "externalTransfer": external,
                "coreDeposit": update,
                "join": {
                    "sameHash": True,
                    "sameUser": True,
                    "sameAsset": "USDC",
                    "sameAmount": str(core_amount),
                },
            })
    return candidates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route 1: Arbitrum USDC transaction -> HyperCore deposit"
    )
    parser.add_argument("--network", choices=sorted(NETWORKS), default="mainnet")
    parser.add_argument("--user", required=True, help="Hyperliquid account address")
    parser.add_argument("--external-tx-hash", required=True)
    parser.add_argument("--amount", help="Optional exact USDC amount")
    parser.add_argument("--bridge-address", help="Optional deposit bridge address")
    parser.add_argument("--external-rpc-url", default=ARBITRUM_MAINNET_RPC)
    parser.add_argument("--token-address", default=ARBITRUM_USDC)
    parser.add_argument("--token-decimals", type=int, default=6)
    parser.add_argument("--start-time-ms", type=int)
    parser.add_argument("--end-time-ms", type=int)
    parser.add_argument("--window-seconds", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def trace(args: argparse.Namespace) -> dict[str, Any]:
    network = NETWORKS[args.network]
    client = PublicClient(network)
    user = normalize_address(args.user)
    tx_hash = normalize_hash(args.external_tx_hash)
    requested_amount = decimal_amount(args.amount) if args.amount else None
    bundle = client.evm_transaction_bundle(args.external_rpc_url, tx_hash)
    if rpc_quantity(bundle["receipt"].get("status", "0x0")) != 1:
        raise TraceError("External transaction receipt is not successful.")
    transfers = parse_erc20_transfers(
        bundle["receipt"], args.token_address, args.token_decimals
    )
    start_ms, end_ms = resolved_time_window(
        bundle["blockTimeMs"], args.start_time_ms, args.end_time_ms,
        args.window_seconds,
    )
    updates = client.ledger_updates(user, start_ms, end_ms)
    candidates = find_deposit_candidates(
        transfers, updates, user=user, tx_hash=tx_hash,
        amount=requested_amount, bridge_address=args.bridge_address,
    )
    return {
        "schemaVersion": 1,
        "route": 1,
        "name": "external network to HyperCore",
        "network": network.name,
        "signedActions": False,
        "query": {
            "user": user,
            "externalTxHash": tx_hash,
            "externalRpcUrl": args.external_rpc_url,
            "tokenAddress": normalize_address(args.token_address),
            "amount": str(requested_amount) if requested_amount else None,
            "startTimeMs": start_ms,
            "endTimeMs": end_ms,
        },
        "externalEvidence": {
            "transaction": bundle["transaction"],
            "receiptStatus": bundle["receipt"].get("status"),
            "blockTimeMs": bundle["blockTimeMs"],
            "transferEvents": transfers,
        },
        "coreLedgerUpdates": updates,
        "candidateCount": len(candidates),
        "classification": classify_candidate_count(len(candidates)),
        "candidates": candidates,
        "rule": "Same hash + user + USDC + exact amount; one candidate only.",
    }


def main() -> None:
    args = build_parser().parse_args()
    result = trace(args)
    write_json(args.output, result)
    print(f"{result['classification']}: {result['candidateCount']} candidate(s)")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
