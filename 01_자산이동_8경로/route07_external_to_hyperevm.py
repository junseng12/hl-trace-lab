"""Route 07: link an external Bitcoin transfer to a HyperEVM receipt.

The verified project example used a bridge-specific 32-byte identifier that
appeared both in a Bitcoin OP_RETURN output and in the destination HyperEVM
transaction input.  This read-only program tests that exact public-data rule.
It is intentionally not a universal bridge decoder: other bridge protocols
may expose their correlation identifier through a status API or other events.
"""

from __future__ import annotations

import argparse
import json
import re
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


DEFAULT_BITCOIN_API = "https://mempool.space/api"
HEX_32_BYTES = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])")


def normalize_bitcoin_txid(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    if len(value) != 64:
        raise TraceError(f"Invalid Bitcoin transaction id: {value!r}")
    try:
        int(value, 16)
    except ValueError as exc:
        raise TraceError(f"Invalid Bitcoin transaction id: {value!r}") from exc
    return value


def normalize_protocol_id(value: str) -> str:
    return normalize_hash(value, allow_without_prefix=True)


def op_return_pushes(script_hex: str) -> list[bytes]:
    """Decode data pushes after OP_RETURN for standard script encodings."""
    try:
        raw = bytes.fromhex(script_hex)
    except ValueError:
        return []
    if not raw or raw[0] != 0x6A:
        return []
    offset = 1
    pushes: list[bytes] = []
    while offset < len(raw):
        opcode = raw[offset]
        offset += 1
        if 1 <= opcode <= 0x4B:
            size = opcode
        elif opcode == 0x4C and offset + 1 <= len(raw):
            size = raw[offset]
            offset += 1
        elif opcode == 0x4D and offset + 2 <= len(raw):
            size = int.from_bytes(raw[offset:offset + 2], "little")
            offset += 2
        elif opcode == 0x4E and offset + 4 <= len(raw):
            size = int.from_bytes(raw[offset:offset + 4], "little")
            offset += 4
        else:
            break
        if offset + size > len(raw):
            break
        pushes.append(raw[offset:offset + size])
        offset += size
    return pushes


def extract_op_return_identifiers(bitcoin_tx: dict[str, Any]) -> list[str]:
    """Return distinct 32-byte values publicly embedded in OP_RETURN outputs."""
    found: set[str] = set()
    for output in bitcoin_tx.get("vout", []):
        if not isinstance(output, dict):
            continue
        script_hex = str(output.get("scriptpubkey", ""))
        script_type = str(output.get("scriptpubkey_type", "")).lower()
        asm = str(output.get("scriptpubkey_asm", ""))
        is_op_return = script_type == "op_return" or script_hex.lower().startswith("6a")
        if not is_op_return:
            continue
        for pushed in op_return_pushes(script_hex):
            if len(pushed) == 32:
                found.add("0x" + pushed.hex())
            # The verified LI.FI Bitcoin example stores an ASCII payload of
            # the form ``0x<64 hex chars>=|lifi...`` in one larger push.
            # Extract the ASCII portion even when the envelope ends with a
            # binary checksum; do not assume every bridge uses this format.
            decoded = pushed.decode("ascii", errors="ignore")
            for match in HEX_32_BYTES.finditer(decoded):
                found.add("0x" + match.group(1).lower())
        for match in HEX_32_BYTES.finditer(asm):
            found.add("0x" + match.group(1).lower())
    return sorted(found)


def analyze_link(
    bitcoin_tx: dict[str, Any],
    evm_bundle: dict[str, Any],
    *,
    expected_protocol_id: str | None = None,
) -> dict[str, Any]:
    """Find the exact 32-byte identifier shared by both public transactions."""
    source_ids = extract_op_return_identifiers(bitcoin_tx)
    evm_input = str(evm_bundle["transaction"].get("input", "")).lower()
    input_hex = evm_input[2:] if evm_input.startswith("0x") else evm_input

    if expected_protocol_id is not None:
        expected = normalize_protocol_id(expected_protocol_id)
        shared = [
            expected
            for _ in [0]
            if expected in source_ids and expected[2:] in input_hex
        ]
    else:
        shared = [value for value in source_ids if value[2:] in input_hex]

    receipt_ok = rpc_quantity(evm_bundle["receipt"].get("status", "0x0")) == 1
    if receipt_ok and len(shared) == 1:
        classification = "EXACT_PROTOCOL_ID_MATCH"
    elif len(shared) > 1:
        classification = "AMBIGUOUS"
    else:
        classification = "NOT_FOUND"

    status = bitcoin_tx.get("status")
    bitcoin_time = status.get("block_time") if isinstance(status, dict) else None
    evm_time_ms = evm_bundle.get("blockTimeMs")
    delay = None
    if isinstance(bitcoin_time, int) and isinstance(evm_time_ms, int):
        delay = evm_time_ms // 1000 - bitcoin_time

    return {
        "bitcoinOpReturnIdentifiers": source_ids,
        "expectedProtocolIdentifier": (
            normalize_protocol_id(expected_protocol_id)
            if expected_protocol_id is not None
            else None
        ),
        "sharedProtocolIdentifiers": shared,
        "sharedIdentifierCount": len(shared),
        "destinationReceiptSucceeded": receipt_ok,
        "observedDelaySeconds": delay,
        "classification": classification,
    }


def trace(args: argparse.Namespace) -> dict[str, Any]:
    network = NETWORKS[args.network]
    bitcoin_txid = normalize_bitcoin_txid(args.bitcoin_txid)
    evm_hash = normalize_hash(args.hyperevm_tx_hash)
    bitcoin_url = args.bitcoin_api_base.rstrip("/") + "/tx/" + bitcoin_txid

    response = requests.get(bitcoin_url, timeout=30)
    response.raise_for_status()
    bitcoin_tx = response.json()
    if not isinstance(bitcoin_tx, dict):
        raise TraceError("Unexpected Bitcoin transaction response.")
    returned_txid = normalize_bitcoin_txid(str(bitcoin_tx.get("txid", bitcoin_txid)))
    if returned_txid != bitcoin_txid:
        raise TraceError("Bitcoin API returned a different transaction id.")

    client = PublicClient(network)
    evm_bundle = client.evm_transaction_bundle(network.hyperevm_rpc_url, evm_hash)
    analysis = analyze_link(
        bitcoin_tx,
        evm_bundle,
        expected_protocol_id=args.protocol_id,
    )
    return {
        "schemaVersion": 1,
        "route": 7,
        "traceType": "External Bitcoin -> HyperEVM",
        "network": network.name,
        "query": {
            "bitcoinTransactionId": bitcoin_txid,
            "hyperEvmTransactionHash": evm_hash,
            "protocolIdentifier": args.protocol_id,
        },
        "dataSources": {
            "bitcoinTransaction": bitcoin_url,
            "hyperEvmRpc": network.hyperevm_rpc_url,
        },
        "analysis": analysis,
        "raw": {
            "bitcoinTransaction": bitcoin_tx,
            "hyperEvmBundle": evm_bundle,
        },
        "classification": analysis["classification"],
        "matchingRule": (
            "The same exact 32-byte protocol identifier must occur in the "
            "Bitcoin OP_RETURN and destination HyperEVM transaction input."
        ),
        "limitations": [
            "The rule is bridge-protocol-specific and is not a universal Bitcoin-to-EVM decoder.",
            "If no common identifier is exposed, use the bridge's public status API and treat amount/time matching as weaker evidence.",
            "A matched route proves transaction linkage, not common control of source and destination addresses.",
        ],
        "signedActions": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=sorted(NETWORKS), default="mainnet")
    parser.add_argument("--bitcoin-txid", required=True)
    parser.add_argument("--hyperevm-tx-hash", required=True)
    parser.add_argument(
        "--protocol-id",
        help="Optional known 32-byte bridge identifier; auto-detected if omitted.",
    )
    parser.add_argument("--bitcoin-api-base", default=DEFAULT_BITCOIN_API)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        report = trace(args)
    except (TraceError, requests.RequestException, KeyError, ValueError) as exc:
        raise SystemExit(f"TRACE FAILED: {exc}") from exc
    write_json(args.output, report)
    print(json.dumps({
        "route": 7,
        "classification": report["classification"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
