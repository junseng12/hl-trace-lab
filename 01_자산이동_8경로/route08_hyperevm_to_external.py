"""Route 08: trace HyperEVM -> external chain through LI.FI status data.

The official LI.FI status response binds the sending and receiving
transactions under one public ``transactionId``.  This read-only program
validates that binding, verifies the source HyperEVM receipt, and optionally
verifies the destination receipt through a caller-supplied public RPC URL.
No private key or API key is required.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import requests

from common import (
    NETWORKS,
    PublicClient,
    TraceError,
    normalize_hash,
    rpc_quantity,
    write_json,
)


DEFAULT_LIFI_STATUS_URL = "https://li.quest/v1/status"


def _chain_id(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_hash(value: Any) -> str | None:
    try:
        return normalize_hash(str(value))
    except TraceError:
        return None


def analyze_lifi_status(
    payload: dict[str, Any],
    *,
    source_hash: str,
    from_chain: int,
    to_chain: int,
    expected_destination_hash: str | None = None,
) -> dict[str, Any]:
    """Validate the bridge's exact public route identifier and both hashes."""
    source_hash = normalize_hash(source_hash)
    expected_destination = (
        normalize_hash(expected_destination_hash)
        if expected_destination_hash is not None
        else None
    )
    sending = payload.get("sending") if isinstance(payload.get("sending"), dict) else {}
    receiving = (
        payload.get("receiving")
        if isinstance(payload.get("receiving"), dict)
        else {}
    )
    returned_source = _safe_hash(sending.get("txHash"))
    returned_destination = _safe_hash(receiving.get("txHash"))
    transaction_id = _safe_hash(payload.get("transactionId"))
    status = str(payload.get("status", "")).upper()
    substatus = str(payload.get("substatus", "")).upper()

    checks = {
        "statusDone": status == "DONE",
        "substatusCompleted": substatus == "COMPLETED",
        "sourceHashMatches": returned_source == source_hash,
        "sourceChainMatches": _chain_id(sending.get("chainId")) == from_chain,
        "destinationHashPresent": returned_destination is not None,
        "destinationChainMatches": _chain_id(receiving.get("chainId")) == to_chain,
        "expectedDestinationMatches": (
            expected_destination is None
            or returned_destination == expected_destination
        ),
        "transactionIdPresent": transaction_id is not None,
    }
    if all(checks.values()):
        classification = "EXACT_PROTOCOL_ID_MATCH"
    elif status in {"PENDING", "NOT_FOUND"} or returned_destination is None:
        classification = "PENDING_OR_INCOMPLETE"
    else:
        classification = "MISMATCH"

    sending_time = sending.get("timestamp")
    receiving_time = receiving.get("timestamp")
    delay = None
    try:
        delay = int(receiving_time) - int(sending_time)
    except (TypeError, ValueError):
        pass

    return {
        "transactionId": transaction_id,
        "status": status,
        "substatus": substatus,
        "tool": payload.get("tool"),
        "sendingTransactionHash": returned_source,
        "receivingTransactionHash": returned_destination,
        "observedDelaySeconds": delay,
        "checks": checks,
        "classification": classification,
    }


def trace(args: argparse.Namespace) -> dict[str, Any]:
    network = NETWORKS[args.network]
    source_hash = normalize_hash(args.source_tx_hash)
    expected_destination = (
        normalize_hash(args.expected_destination_hash)
        if args.expected_destination_hash
        else None
    )
    params = {
        "txHash": source_hash,
        "fromChain": network.chain_id,
        "toChain": args.to_chain,
    }
    response = requests.get(args.lifi_status_url, params=params, timeout=30)
    response.raise_for_status()
    status_payload = response.json()
    if not isinstance(status_payload, dict):
        raise TraceError("Unexpected LI.FI status response.")

    analysis = analyze_lifi_status(
        status_payload,
        source_hash=source_hash,
        from_chain=network.chain_id,
        to_chain=args.to_chain,
        expected_destination_hash=expected_destination,
    )

    client = PublicClient(network)
    source_bundle = client.evm_transaction_bundle(
        network.hyperevm_rpc_url, source_hash
    )
    source_receipt_ok = rpc_quantity(
        source_bundle["receipt"].get("status", "0x0")
    ) == 1

    destination_bundle = None
    destination_chain_id = None
    destination_receipt_ok = None
    destination_hash = analysis["receivingTransactionHash"]
    if args.destination_rpc_url and destination_hash:
        destination_chain_id = rpc_quantity(
            client.rpc(args.destination_rpc_url, "eth_chainId", [])
        )
        destination_bundle = client.evm_transaction_bundle(
            args.destination_rpc_url, destination_hash
        )
        destination_receipt_ok = rpc_quantity(
            destination_bundle["receipt"].get("status", "0x0")
        ) == 1

    chain_checks = {
        "sourceReceiptSucceeded": source_receipt_ok,
        "destinationRpcQueried": bool(args.destination_rpc_url),
        "destinationRpcChainId": destination_chain_id,
        "destinationRpcChainMatches": (
            None if destination_chain_id is None
            else destination_chain_id == args.to_chain
        ),
        "destinationReceiptSucceeded": destination_receipt_ok,
    }
    classification = analysis["classification"]
    if not source_receipt_ok:
        classification = "MISMATCH"
    if destination_bundle is not None and not (
        destination_chain_id == args.to_chain and destination_receipt_ok
    ):
        classification = "MISMATCH"

    return {
        "schemaVersion": 1,
        "route": 8,
        "traceType": "HyperEVM -> External chain",
        "network": network.name,
        "query": {
            "sourceTransactionHash": source_hash,
            "fromChain": network.chain_id,
            "toChain": args.to_chain,
            "expectedDestinationHash": expected_destination,
        },
        "dataSources": {
            "lifiStatusApi": response.url,
            "sourceHyperEvmRpc": network.hyperevm_rpc_url,
            "destinationRpcProvided": bool(args.destination_rpc_url),
        },
        "analysis": analysis,
        "chainChecks": chain_checks,
        "raw": {
            "lifiStatus": status_payload,
            "sourceTransactionBundle": source_bundle,
            "destinationTransactionBundle": destination_bundle,
        },
        "classification": classification,
        "matchingRule": (
            "LI.FI's exact transactionId binds the exact sending and receiving "
            "transaction hashes; chain RPC receipts independently confirm execution."
        ),
        "limitations": [
            "If the bridge status service no longer retains a record, cross-chain linkage may require protocol-event decoding.",
            "A changed asset or recipient is valid bridge behavior; compare LI.FI sending/receiving fields rather than assuming equal amounts or addresses.",
            "A destination RPC URL is optional; without it the destination is provider-confirmed but not independently receipt-verified by this run.",
        ],
        "signedActions": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=sorted(NETWORKS), default="mainnet")
    parser.add_argument("--source-tx-hash", required=True)
    parser.add_argument("--to-chain", required=True, type=int)
    parser.add_argument("--expected-destination-hash")
    parser.add_argument(
        "--destination-rpc-url",
        help="Optional public destination JSON-RPC URL for independent receipt verification.",
    )
    parser.add_argument("--lifi-status-url", default=DEFAULT_LIFI_STATUS_URL)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.to_chain <= 0:
        raise SystemExit("--to-chain must be a positive EVM chain id.")
    try:
        report = trace(args)
    except (TraceError, requests.RequestException, KeyError, ValueError) as exc:
        raise SystemExit(f"TRACE FAILED: {exc}") from exc
    write_json(args.output, report)
    print(json.dumps({
        "route": 8,
        "classification": report["classification"],
        "transactionId": report["analysis"]["transactionId"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
