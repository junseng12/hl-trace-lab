"""Recover orderbook counterparties from downloaded node fill archives.

Input is one or more *decompressed* JSONL files from the official
``node_fills_by_block`` archive.  Each current-format line is a block envelope
whose ``events`` are ``[wallet_address, fill]`` pairs.  Both sides share
``(coin, time, tid)``, so grouping that key reconstructs the matched pair.

This tool is local/read-only. It does not download requester-pays S3 objects.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# 1) Normalize archive records into the same (coin, time, tid) model
# ---------------------------------------------------------------------------


def normalize_address(value: str) -> str:
    value = value.strip().lower()
    if not value.startswith("0x") or len(value) != 42:
        raise SystemExit(f"Invalid address: {value!r}")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise SystemExit(f"Invalid address: {value!r}") from exc
    return value


def fill_key(fill: dict[str, Any]) -> tuple[str, str, str] | None:
    """Build the same join key used by the live WebSocket tracer."""
    if fill.get("coin") is None or fill.get("time") is None or fill.get("tid") is None:
        return None
    return str(fill["coin"]), str(fill["time"]), str(fill["tid"])


def current_events(envelope: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield fills from current node_fills_by_block block envelopes.

    Current files store each side separately as ``[wallet, fill]``.  Two rows
    with the same key therefore reveal the matched wallet pair.
    """
    events = envelope.get("events")
    if not isinstance(events, list):
        return
    for event in events:
        if not isinstance(event, list) or len(event) != 2:
            continue
        user, fill = event
        if not isinstance(user, str) or not isinstance(fill, dict):
            continue
        yield {
            "user": user.lower(),
            "blockNumber": envelope.get("block_number"),
            "blockTime": envelope.get("block_time"),
            "fill": fill,
        }


def legacy_trade(record: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize the documented legacy node_trades side_info shape."""
    side_info = record.get("side_info")
    if not isinstance(side_info, list) or len(side_info) != 2:
        return None
    users = [side.get("user") for side in side_info if isinstance(side, dict)]
    if len(users) != 2 or not all(isinstance(user, str) for user in users):
        return None
    return {
        "legacy": True,
        "coin": record.get("coin"),
        "time": record.get("time"),
        "tid": record.get("tid"),
        "px": record.get("px"),
        "sz": record.get("sz"),
        "users": [user.lower() for user in users],
        "raw": record,
    }


def read_jsonl(paths: list[Path]) -> Iterable[tuple[Path, int, dict[str, Any]]]:
    """Stream large decompressed files without loading an hour into RAM."""
    for path in paths:
        if path.suffix.lower() == ".lz4":
            raise SystemExit(
                f"{path}: decompress first with 'unlz4 FILE.lz4 FILE.jsonl'."
            )
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"{path}:{line_number}: invalid JSON: {exc}") from exc
                if isinstance(value, dict):
                    yield path, line_number, value


# ---------------------------------------------------------------------------
# 2) Filter the requested time/market and rebuild counterparty pairs
# ---------------------------------------------------------------------------
def main() -> None:
    """Scan local archives and group both fill sides by the stable trade key."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True,
                        help="Decompressed hourly JSONL; repeat for multiple files")
    parser.add_argument("--user", action="append",
                        help="Target wallet; repeat for multiple wallets")
    parser.add_argument("--coin")
    parser.add_argument("--tid", type=int)
    parser.add_argument("--start-time-ms", type=int)
    parser.add_argument("--end-time-ms", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.user and args.tid is None:
        raise SystemExit("Provide at least one --user or --tid filter.")
    if args.end_time_ms is not None and args.start_time_ms is not None:
        if args.end_time_ms < args.start_time_ms:
            raise SystemExit("--end-time-ms must be >= --start-time-ms")
    for path in args.input:
        if not path.is_file():
            raise SystemExit(f"Input file not found: {path}")

    targets = {normalize_address(user) for user in (args.user or [])}
    coin_filter = args.coin.strip() if args.coin else None
    # Only candidate rows are retained; unrelated archive events are streamed
    # past and discarded to keep memory use bounded.
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    legacy_matches = []
    lines = 0
    events = 0
    for path, line_number, envelope in read_jsonl(args.input):
        lines += 1
        legacy = legacy_trade(envelope)
        if legacy is not None:
            if targets and not targets.intersection(legacy["users"]):
                continue
            if coin_filter and str(legacy.get("coin")) != coin_filter:
                continue
            if args.tid is not None and str(legacy.get("tid")) != str(args.tid):
                continue
            legacy_matches.append({"source": f"{path}:{line_number}", **legacy})
            continue
        for row in current_events(envelope):
            events += 1
            fill = row["fill"]
            key = fill_key(fill)
            if key is None:
                continue
            if coin_filter and key[0] != coin_filter:
                continue
            if args.tid is not None and key[2] != str(args.tid):
                continue
            event_time = int(key[1])
            if args.start_time_ms is not None and event_time < args.start_time_ms:
                continue
            if args.end_time_ms is not None and event_time > args.end_time_ms:
                continue
            grouped[key].append({"source": f"{path}:{line_number}", **row})

    matches = []
    for key, rows in sorted(grouped.items()):
        # A complete ordinary match has exactly two unique wallet addresses.
        users = list(dict.fromkeys(row["user"] for row in rows))
        if targets and not targets.intersection(users):
            continue
        counterparties = {
            target: [user for user in users if user != target]
            for target in sorted(targets.intersection(users))
        }
        matches.append({
            "key": {"coin": key[0], "time": key[1], "tid": key[2]},
            "users": users,
            "isCompletePair": len(users) == 2,
            "counterpartiesForTargets": counterparties,
            "fills": rows,
        })

    report = {
        "schemaVersion": 1,
        "sourceType": "local Hyperliquid node fill archive",
        "inputs": [str(path.resolve()) for path in args.input],
        "filters": {
            "users": sorted(targets), "coin": coin_filter, "tid": args.tid,
            "startTimeMs": args.start_time_ms, "endTimeMs": args.end_time_ms,
        },
        "linesScanned": lines,
        "fillEventsScanned": events,
        "matchedTrades": len(matches),
        "matches": matches,
        "legacyMatches": legacy_matches,
        "signedActions": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "status": "complete", "matchedTrades": len(matches),
        "legacyMatches": len(legacy_matches), "output": str(args.output.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
