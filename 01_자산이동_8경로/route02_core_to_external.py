"""Route 2: trace a HyperCore USDC withdrawal to Arbitrum.

Core withdrawal and Arbitrum receipt hashes differ.  The tracer therefore
combines the destination, requested amount, recorded fee, net received amount
and adjacent time, and refuses to auto-select when multiple candidates remain.
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


def find_withdrawal_candidates(
    transfers: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    *,
    destination: str,
    requested_amount: Decimal | None = None,
    core_hash: str | None = None,
) -> list[dict[str, Any]]:
    """Match withdrawal amount minus recorded fee to the received transfer."""
    destination = normalize_address(destination)
    normalized_core_hash = normalize_hash(core_hash) if core_hash else None
    received_rows = [row for row in transfers if row.get("to") == destination]
    candidates: list[dict[str, Any]] = []
    for update in updates:
        delta = delta_of(update)
        if str(delta.get("type", "")).lower() != "withdraw":
            continue
        if (
            normalized_core_hash is not None
            and str(update.get("hash", "")).lower() != normalized_core_hash
        ):
            continue
        try:
            core_amount = abs(Decimal(str(delta.get("usdc"))))
            fee = abs(Decimal(str(delta.get("fee", "0"))))
        except Exception:
            continue
        if requested_amount is not None and core_amount != requested_amount:
            continue
        net = core_amount - fee
        if net < 0:
            continue
        for external in received_rows:
            try:
                received = Decimal(str(external.get("amount")))
            except Exception:
                continue
            if received != net:
                continue
            candidates.append({
                "coreWithdrawal": update,
                "externalTransfer": external,
                "join": {
                    "destination": destination,
                    "requestedUsdc": str(core_amount),
                    "feeUsdc": str(fee),
                    "netExpectedUsdc": str(net),
                    "receivedUsdc": str(received),
                    "exactNetAmount": True,
                },
            })
    return candidates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route 2: HyperCore withdrawal -> Arbitrum USDC receipt"
    )
    parser.add_argument("--network", choices=sorted(NETWORKS), default="mainnet")
    parser.add_argument("--user", required=True, help="Withdrawing HyperCore account")
    parser.add_argument("--destination", required=True, help="Arbitrum recipient")
    parser.add_argument("--external-tx-hash", required=True)
    parser.add_argument("--core-hash", help="Optional exact Core withdrawal hash")
    parser.add_argument("--requested-amount", help="Optional gross USDC amount")
    parser.add_argument("--external-rpc-url", default=ARBITRUM_MAINNET_RPC)
    parser.add_argument("--token-address", default=ARBITRUM_USDC)
    parser.add_argument("--token-decimals", type=int, default=6)
    parser.add_argument("--start-time-ms", type=int)
    parser.add_argument("--end-time-ms", type=int)
    parser.add_argument("--window-seconds", type=int, default=600)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def trace(args: argparse.Namespace) -> dict[str, Any]:
    network = NETWORKS[args.network]
    client = PublicClient(network)
    user = normalize_address(args.user)
    destination = normalize_address(args.destination)
    external_hash = normalize_hash(args.external_tx_hash)
    core_hash = normalize_hash(args.core_hash) if args.core_hash else None
    requested = (
        decimal_amount(args.requested_amount) if args.requested_amount else None
    )
    bundle = client.evm_transaction_bundle(args.external_rpc_url, external_hash)
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
    candidates = find_withdrawal_candidates(
        transfers, updates, destination=destination,
        requested_amount=requested, core_hash=core_hash,
    )
    return {
        "schemaVersion": 1,
        "route": 2,
        "name": "HyperCore to external network",
        "network": network.name,
        "signedActions": False,
        "query": {
            "user": user,
            "destination": destination,
            "coreHash": core_hash,
            "externalTxHash": external_hash,
            "requestedAmount": str(requested) if requested else None,
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
        "rule": "Destination + gross amount - recorded fee = exact received amount; one candidate only.",
    }


def main() -> None:
    args = build_parser().parse_args()
    result = trace(args)
    write_json(args.output, result)
    print(f"{result['classification']}: {result['candidateCount']} candidate(s)")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
