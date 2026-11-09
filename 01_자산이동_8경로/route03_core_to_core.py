"""Route 3: trace a direct HyperCore account-to-account transfer.

The route is entirely inside HyperCore, so no EVM transaction is expected.
The same Core ledger event may appear in one or both users' public histories;
duplicates are merged by hash and event time before classification.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
from typing import Any

from common import (
    NETWORKS,
    PublicClient,
    TraceError,
    classify_candidate_count,
    decimal_amount,
    delta_of,
    normalize_address,
    normalize_hash,
    write_json,
)


def _transfer_shape(delta: dict[str, Any]) -> tuple[str, Decimal] | None:
    kind = str(delta.get("type", "")).lower()
    try:
        if kind in {"send", "spottransfer"}:
            return str(delta.get("token", "")).upper(), abs(
                Decimal(str(delta.get("amount")))
            )
        if kind == "internaltransfer":
            return "USDC", abs(Decimal(str(delta.get("usdc"))))
    except Exception:
        return None
    return None


def find_core_transfer_candidates(
    sender_updates: list[dict[str, Any]],
    recipient_updates: list[dict[str, Any]],
    *,
    sender: str,
    recipient: str,
    token: str,
    amount: Decimal,
    core_hash: str | None = None,
) -> list[dict[str, Any]]:
    sender = normalize_address(sender)
    recipient = normalize_address(recipient)
    token = token.strip().upper()
    normalized_hash = normalize_hash(core_hash) if core_hash else None
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for source_name, updates in (
        ("senderLedger", sender_updates), ("recipientLedger", recipient_updates)
    ):
        for update in updates:
            delta = delta_of(update)
            shape = _transfer_shape(delta)
            if shape is None:
                continue
            row_token, row_amount = shape
            if row_token != token or row_amount != amount:
                continue
            if str(delta.get("user", "")).lower() != sender:
                continue
            if str(delta.get("destination", "")).lower() != recipient:
                continue
            try:
                row_hash = normalize_hash(str(update.get("hash", "")))
            except TraceError:
                continue
            if normalized_hash is not None and row_hash != normalized_hash:
                continue
            key = (row_hash, str(update.get("time", "")))
            entry = grouped.setdefault(key, {
                "hash": row_hash,
                "time": update.get("time"),
                "delta": delta,
                "seenIn": [],
            })
            if source_name not in entry["seenIn"]:
                entry["seenIn"].append(source_name)
    return list(grouped.values())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route 3: direct HyperCore sender -> recipient transfer"
    )
    parser.add_argument("--network", choices=sorted(NETWORKS), required=True)
    parser.add_argument("--sender", required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--token", default="HYPE")
    parser.add_argument("--amount", required=True)
    parser.add_argument("--core-hash")
    parser.add_argument("--start-time-ms", type=int, required=True)
    parser.add_argument("--end-time-ms", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def trace(args: argparse.Namespace) -> dict[str, Any]:
    if args.start_time_ms > args.end_time_ms:
        raise TraceError("start-time-ms must not be later than end-time-ms.")
    network = NETWORKS[args.network]
    client = PublicClient(network)
    sender = normalize_address(args.sender)
    recipient = normalize_address(args.recipient)
    amount = decimal_amount(args.amount)
    core_hash = normalize_hash(args.core_hash) if args.core_hash else None
    sender_rows = client.ledger_updates(
        sender, args.start_time_ms, args.end_time_ms
    )
    recipient_rows = client.ledger_updates(
        recipient, args.start_time_ms, args.end_time_ms
    )
    candidates = find_core_transfer_candidates(
        sender_rows, recipient_rows, sender=sender, recipient=recipient,
        token=args.token, amount=amount, core_hash=core_hash,
    )
    return {
        "schemaVersion": 1,
        "route": 3,
        "name": "HyperCore account to HyperCore account",
        "network": network.name,
        "signedActions": False,
        "query": {
            "sender": sender,
            "recipient": recipient,
            "token": args.token.upper(),
            "amount": str(amount),
            "coreHash": core_hash,
            "startTimeMs": args.start_time_ms,
            "endTimeMs": args.end_time_ms,
        },
        "senderLedgerUpdates": sender_rows,
        "recipientLedgerUpdates": recipient_rows,
        "candidateCount": len(candidates),
        "classification": classify_candidate_count(len(candidates)),
        "candidates": candidates,
        "expectedEvmTransaction": False,
        "rule": "Same Core hash/event + sender + recipient + token + exact amount; one distinct event only.",
    }


def main() -> None:
    args = build_parser().parse_args()
    result = trace(args)
    write_json(args.output, result)
    print(f"{result['classification']}: {result['candidateCount']} candidate(s)")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
