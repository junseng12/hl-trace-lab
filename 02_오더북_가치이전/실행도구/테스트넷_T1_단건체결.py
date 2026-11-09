"""Run one bounded Testnet counterparty-retention pilot.

The source .env file is never modified.  The signed-action switch is enabled
only in the in-memory configuration after the network, address, key and hard
cap validations from the existing runner are applied.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import secrets
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_ENV = REPO_ROOT / ".env.local"


# ---------------------------------------------------------------------------
# 1) Public evidence helpers
# ---------------------------------------------------------------------------
def post(requests_module, api_url: str, payload: dict) -> object:
    response = requests_module.post(api_url.rstrip("/") + "/info", json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def trade_key(item: dict) -> tuple[str, str, str] | None:
    if item.get("time") is None or item.get("coin") is None or item.get("tid") is None:
        return None
    return str(item["time"]), str(item["coin"]), str(item["tid"])


# ---------------------------------------------------------------------------
# 2) Safe Testnet execution and evidence collection
# ---------------------------------------------------------------------------
def main() -> None:
    """Create one bounded maker/taker fill and immediately reconstruct it.

    Safety order:
      validate network/config -> verify balances/caps -> start WebSocket ->
      place maker -> confirm best ask -> place IOC taker -> save public proof.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-root", type=Path, default=SCRIPT_DIR)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute-testnet-pilot", action="store_true")
    args = parser.parse_args()
    if not args.execute_testnet_pilot:
        raise SystemExit("REFUSED: explicit Testnet pilot flag is required.")

    sys.path.insert(0, str(args.lab_root))
    preflight = importlib.import_module("테스트넷_사전점검")
    a1 = importlib.import_module("체결도구")
    spot = importlib.import_module("현물시장도구")
    requests = importlib.import_module("requests")
    Account = importlib.import_module("eth_account").Account
    Exchange = importlib.import_module("hyperliquid.exchange").Exchange
    Info = importlib.import_module("hyperliquid.info").Info
    Cloid = importlib.import_module("hyperliquid.utils.types").Cloid

    logging.disable(logging.DEBUG)
    # The file must remain locked. Signed actions are enabled only in this
    # process after every Testnet, address, Agent-key and hard-cap check passes.
    raw = preflight.read_config(args.env)
    if raw.get("HL_ENABLE_SIGNED_ACTIONS", "").strip().lower() != "false":
        raise SystemExit("REFUSED: source config must remain locked with signed actions disabled.")
    memory_config = dict(raw)
    memory_config["HL_ENABLE_SIGNED_ACTIONS"] = "true"
    validated = preflight.validate_config(memory_config, signed_actions_required=True)
    if validated.api_url != preflight.EXPECTED_API_URL:
        raise SystemExit("REFUSED: only the official Testnet API is permitted.")

    target = Decimal("10.5")
    if target > Decimal(str(validated.safety_caps["HL_MAX_ORDER_NOTIONAL_USD"])):
        raise SystemExit("REFUSED: pilot target exceeds the configured order cap.")

    info = Info(validated.api_url, skip_ws=True)
    resolved = spot.resolve(info, "HYPE/USDC")
    asset_id = int(info.coin_to_asset[resolved["wire"]])
    market = a1.SpotMarket(
        display_name=resolved["display"],
        wire_coin=resolved["wire"],
        asset_id=asset_id,
        base_token=resolved["base"],
        quote_token="USDC",
        sz_decimals=resolved["szDecimals"],
    )

    started_at = datetime.now(timezone.utc)
    started_ms = int(started_at.timestamp() * 1000) - 2_000
    before = {
        wallet.slot: {
            "userFills": post(requests, validated.api_url, {"type": "userFills", "user": wallet.master, "aggregateByTime": False}),
            "historicalOrders": post(requests, validated.api_url, {"type": "historicalOrders", "user": wallet.master}),
        }
        for wallet in validated.wallets
    }
    recent_before = post(requests, validated.api_url, {"type": "recentTrades", "coin": market.wire_coin})

    # Start evidence capture before either controlled order is submitted.
    capture = a1.WsCapture(validated.api_url, market, validated.wallets)
    capture.start()
    pilot_root = args.output_dir / "runner"
    pilot_root.mkdir(parents=True, exist_ok=True)
    by_slot = {wallet.slot: wallet for wallet in validated.wallets}
    seller_cfg = by_slot["W2"]
    buyer_cfg = by_slot["W1"]
    maker_cloid = "0x" + secrets.token_hex(16)
    taker_cloid = "0x" + secrets.token_hex(16)
    maker_exchange = None
    maker_may_rest = False
    maker_terminal = False
    cleanup_response = None
    try:
        bid, ask, before_book = spot.top(validated.api_url, market.wire_coin)
        mid = (bid + ask) / Decimal("2")
        tick = Decimal(1).scaleb(-(8 - market.sz_decimals))
        slippage_cap = Decimal(str(validated.safety_caps["HL_MAX_SLIPPAGE_BPS"]))
        proposed = max(bid + tick, mid * (Decimal("1") - min(slippage_cap, Decimal("45")) / Decimal("10000")))
        price = spot.rounded_price(proposed, market.sz_decimals)
        if not bid < price < ask:
            raise RuntimeError("Computed HYPE maker price was not strictly inside the spread.")
        deviation = abs(price - mid) / mid * Decimal("10000")
        if deviation > slippage_cap:
            raise RuntimeError("Computed HYPE maker price exceeded the configured deviation cap.")
        quantum = Decimal(1).scaleb(-market.sz_decimals)
        size = (target / price / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum
        notional = size * price
        if notional > Decimal(str(validated.safety_caps["HL_MAX_ORDER_NOTIONAL_USD"])):
            raise RuntimeError("Rounded HYPE order exceeded the configured order cap.")
        seller_before = spot.available(info, seller_cfg.master, market.base_token)
        if seller_before < size:
            raise RuntimeError("W2 lacks prepared HYPE inventory for the bounded pilot.")

        seller_signer = Account.from_key(seller_cfg.private_key)
        buyer_signer = Account.from_key(buyer_cfg.private_key)
        if seller_signer.address.lower() != seller_cfg.agent or buyer_signer.address.lower() != buyer_cfg.agent:
            raise RuntimeError("Configured Agent key mismatch.")
        maker_exchange = Exchange(seller_signer, validated.api_url, account_address=seller_cfg.master)
        taker_exchange = Exchange(buyer_signer, validated.api_url, account_address=buyer_cfg.master)
        # Maker first: its order must rest inside the spread.  If not, the
        # script refuses to send the second order and attempts cleanup.
        maker_response = maker_exchange.order(
            market.display_name,
            False,
            float(size),
            float(price),
            {"limit": {"tif": "Alo"}},
            False,
            Cloid.from_str(maker_cloid),
        )
        maker_status = spot.status(maker_response)
        maker_may_rest = isinstance(maker_status, dict) and "resting" in maker_status
        if not maker_may_rest:
            raise RuntimeError("Maker order did not rest; no taker order was sent.")
        time.sleep(0.25)
        fresh_bid, fresh_ask, pre_ioc_book = spot.top(validated.api_url, market.wire_coin)
        if fresh_ask != price:
            raise RuntimeError("Maker was no longer the best ask; no taker order was sent.")
        # IOC taker is sent only while the controlled maker remains best ask.
        taker_response = taker_exchange.order(
            market.display_name,
            True,
            float(size),
            float(price),
            {"limit": {"tif": "Ioc"}},
            False,
            Cloid.from_str(taker_cloid),
        )
        taker_status = spot.status(taker_response)
        time.sleep(3)
        seller_after = spot.available(info, seller_cfg.master, market.base_token)
        recent = post(requests, validated.api_url, {"type": "recentTrades", "coin": market.wire_coin})
        expected_users = {seller_cfg.master.lower(), buyer_cfg.master.lower()}
        matches = [
            trade for trade in recent if isinstance(trade, dict)
            and int(trade.get("time", 0)) >= started_ms
            and set(str(user).lower() for user in trade.get("users", [])) == expected_users
            and Decimal(str(trade.get("px", "0"))) == price
            and Decimal(str(trade.get("sz", "0"))) == size
        ]
        valid = isinstance(taker_status, dict) and "filled" in taker_status and seller_after < seller_before and len(matches) >= 1
        maker_terminal = valid
        pilot = {
            "schemaVersion": 1,
            "study": "Experiment 1 controlled single fill",
            "network": "testnet",
            "market": resolved,
            "regime": "edge",
            "roles": {"seller": seller_cfg.master, "buyer": buyer_cfg.master},
            "bookBefore": before_book,
            "bookBeforeIoc": pre_ioc_book,
            "metrics": {
                "bid": str(bid), "ask": str(ask), "mid": str(mid), "price": str(price),
                "size": str(size), "notional": str(notional),
                "priceDeviationBps": str((price - mid) / mid * Decimal("10000")),
                "freshBestBidBeforeIoc": str(fresh_bid), "freshBestAskBeforeIoc": str(fresh_ask),
            },
            "orders": {
                "makerCloid": maker_cloid, "takerCloid": taker_cloid,
                "makerResponse": maker_response, "takerResponse": taker_response,
            },
            "publicPairTrades": matches,
            "sellerBaseBalance": {"before": str(seller_before), "after": str(seller_after)},
            "classification": "VALID_DIRECT_PAIR" if valid else "FAILED_OR_CONTAMINATED",
            "capturedAt": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        if maker_may_rest and not maker_terminal and maker_exchange is not None:
            try:
                cleanup_response = maker_exchange.cancel_by_cloid(market.display_name, Cloid.from_str(maker_cloid))
            except Exception:
                cleanup_response = {"status": "cleanup_failed_or_already_terminal"}
        websocket = capture.snapshot()
        capture.stop()
    pilot["makerCleanupIfNeeded"] = cleanup_response
    pilot_serialized = json.dumps(pilot, ensure_ascii=False, indent=2)
    preflight.ensure_no_secrets(pilot_serialized, validated.wallets)
    pilot_output_dir = pilot_root / "outputs" / "experiment1"
    pilot_output_dir.mkdir(parents=True, exist_ok=True)
    pilot_path = pilot_output_dir / f"single_fill_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    pilot_path.write_text(pilot_serialized + "\n", encoding="utf-8")
    controlled = [row for row in pilot.get("publicPairTrades", []) if isinstance(row, dict)]
    controlled_keys = {trade_key(row) for row in controlled} - {None}

    after = {
        wallet.slot: {
            "master": wallet.master,
            "userFills": post(requests, validated.api_url, {"type": "userFills", "user": wallet.master, "aggregateByTime": False}),
            "userFillsByTime": post(requests, validated.api_url, {"type": "userFillsByTime", "user": wallet.master, "startTime": started_ms}),
            "historicalOrders": post(requests, validated.api_url, {"type": "historicalOrders", "user": wallet.master}),
        }
        for wallet in validated.wallets
    }
    recent_after = post(requests, validated.api_url, {"type": "recentTrades", "coin": market.wire_coin})

    def matching_fills(slot: str, source: str) -> list[dict]:
        rows = after[slot][source]
        return [row for row in rows if isinstance(row, dict) and trade_key(row) in controlled_keys]

    ws_trades = a1.flatten_public_trades(websocket)
    ws_matches = [row for row in ws_trades if trade_key(row) in controlled_keys]
    recent_matches = [row for row in recent_after if isinstance(row, dict) and trade_key(row) in controlled_keys]
    w1_matches = matching_fills("W1", "userFills")
    w2_matches = matching_fills("W2", "userFills")

    # Final report compares four views of the same tid: public trades stream,
    # recentTrades, W1 userFills, and W2 userFills.
    report = {
        "schemaVersion": 1,
        "experiment": "Experiment 1 - per-fill counterparty storage and immediate reconstruction",
        "network": "Hyperliquid Testnet",
        "startedAt": started_at.isoformat(),
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "sourceConfigWasLocked": True,
        "signedEnablement": "in-memory only",
        "market": resolved,
        "roles": {"W2": "maker/seller", "W1": "taker/buyer", "W3": "observer", "W4": "observer"},
        "pilotExitCode": 0 if pilot.get("classification") == "VALID_DIRECT_PAIR" else 4,
        "pilotArtifact": str(pilot_path.resolve()),
        "pilot": pilot,
        "before": {"recentTrades": recent_before, "accounts": before},
        "websocket": websocket,
        "after": {"recentTrades": recent_after, "accounts": after},
        "controlledTradeKeys": [
            {"time": key[0], "coin": key[1], "tid": key[2]} for key in sorted(controlled_keys)
        ],
        "immediateReconstruction": {
            "controlledTradeCount": len(controlled_keys),
            "websocketTradeMatches": len(ws_matches),
            "recentTradesMatches": len(recent_matches),
            "W1UserFillsMatches": len(w1_matches),
            "W2UserFillsMatches": len(w2_matches),
            "websocketUsers": [row.get("users") for row in ws_matches],
            "recentTradesUsers": [row.get("users") for row in recent_matches],
            "W1UserFillsHasCounterpartyField": any("users" in row or "counterparty" in row for row in w1_matches),
            "W2UserFillsHasCounterpartyField": any("users" in row or "counterparty" in row for row in w2_matches),
            "sameTradeKeyPresentOnBothUserFills": bool(controlled_keys) and {trade_key(row) for row in w1_matches} == controlled_keys and {trade_key(row) for row in w2_matches} == controlled_keys,
        },
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    preflight.ensure_no_secrets(serialized, validated.wallets)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / f"experiment1_testnet_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    destination.write_text(serialized + "\n", encoding="utf-8")
    print(json.dumps({"status": pilot.get("classification"), "report": str(destination.resolve()), "summary": report["immediateReconstruction"]}, ensure_ascii=False))
    raise SystemExit(0 if pilot.get("classification") == "VALID_DIRECT_PAIR" else 4)


if __name__ == "__main__":
    main()
