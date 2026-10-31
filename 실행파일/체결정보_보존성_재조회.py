"""Recheck older controlled Testnet fills using public endpoints only."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import requests


API_URL = "https://api.hyperliquid-testnet.xyz"


def post(payload: dict) -> object:
    response = requests.post(API_URL + "/info", json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def normalize_users(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).lower() for item in value]
    if isinstance(value, str):
        return [item.lower() for item in value.split()]
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source = json.loads(args.reference.read_text(encoding="utf-8"))
    examples: list[dict] = []
    for group_name, group in source.get("spot", {}).items():
        for item in group.get("examples", []):
            examples.append({"group": group_name, **item, "users": normalize_users(item.get("users"))})
    for group_name in ("directExamples", "controlExamples"):
        for item in source.get("perpB", {}).get(group_name, []):
            examples.append({"group": f"perpB.{group_name}", **item, "users": normalize_users(item.get("users"))})

    addresses = sorted({address for item in examples for address in item["users"]})
    fills_by_address = {
        address: post({"type": "userFills", "user": address, "aggregateByTime": False})
        for address in addresses
    }
    markets = source.get("thirdPartyAccessibilityAudit", {}).get("recentTrades", {}).get("marketsChecked", [])
    recent_by_market = {market: post({"type": "recentTrades", "coin": market}) for market in markets}

    checks = []
    for item in examples:
        tid = str(item.get("tid"))
        trade_hash = str(item.get("hash", "")).lower()
        sides = {}
        for address in item["users"]:
            rows = fills_by_address.get(address, [])
            matches = [
                row for row in rows if isinstance(row, dict)
                and str(row.get("tid")) == tid
                and str(row.get("hash", "")).lower() == trade_hash
            ]
            sides[address] = {
                "matchCount": len(matches),
                "matches": matches,
                "hasCounterpartyField": any("users" in row or "counterparty" in row for row in matches),
            }
        recent_matches = []
        for market, rows in recent_by_market.items():
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and str(row.get("tid")) == tid and str(row.get("hash", "")).lower() == trade_hash:
                    recent_matches.append({"market": market, "row": row})
        checks.append({
            "group": item["group"],
            "tid": item.get("tid"),
            "hash": item.get("hash"),
            "expectedUsers": item["users"],
            "userFillsBySide": sides,
            "bothSidesRecoverableByKnownCandidateJoin": len(sides) == 2 and all(side["matchCount"] > 0 for side in sides.values()),
            "recentTradesMatches": recent_matches,
        })

    report = {
        "schemaVersion": 1,
        "experiment": "Experiment 1 historical counterparty retention recheck",
        "network": "Hyperliquid Testnet",
        "authentication": "none; public /info requests only",
        "referenceCapturedAt": source.get("capturedAt"),
        "recheckedAt": datetime.now(timezone.utc).isoformat(),
        "examplesChecked": len(checks),
        "recentMarketsChecked": markets,
        "summary": {
            "examplesStillInRecentTrades": sum(bool(item["recentTradesMatches"]) for item in checks),
            "examplesRecoverableByKnownCandidateUserFillsJoin": sum(item["bothSidesRecoverableByKnownCandidateJoin"] for item in checks),
            "examplesWithCounterpartyFieldInUserFills": sum(
                any(side["hasCounterpartyField"] for side in item["userFillsBySide"].values()) for item in checks
            ),
        },
        "checks": checks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "summary": report["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
