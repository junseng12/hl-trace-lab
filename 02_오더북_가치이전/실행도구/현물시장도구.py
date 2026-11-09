"""Execute one bounded paired Testnet spot fill for the A/C repeatability pilots."""

from __future__ import annotations

import argparse
import json
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any

import 테스트넷_사전점검 as preflight
import requests
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.types import Cloid


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
ALLOWED_MARKETS = {"TEEYI/USDC", "BREAD/USDC"}


# 1) Market metadata, order-book and balance helpers.
def status(response: Any) -> Any:
    try:
        return response["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        return None


def resolve(info: Info, display: str) -> dict[str, Any]:
    meta = info.spot_meta()
    token_by_index = {t["index"]: t for t in meta["tokens"]}
    wire = info.name_to_coin.get(display)
    matches = [u for u in meta["universe"] if u.get("name") == wire]
    if len(matches) != 1:
        raise RuntimeError("Could not resolve exactly one requested spot market.")
    market = matches[0]
    base = token_by_index[market["tokens"][0]]
    return {"display": display, "wire": wire, "base": base["name"], "szDecimals": int(base["szDecimals"])}


def api_post(api_url: str, payload: dict[str, Any]) -> Any:
    response = requests.post(api_url + "/info", json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def top(api_url: str, wire: str) -> tuple[Decimal, Decimal, dict[str, Any]]:
    raw = api_post(api_url, {"type": "l2Book", "coin": wire})
    levels = raw.get("levels", [[], []])
    if not levels[0] or not levels[1]:
        raise RuntimeError("Two-sided market is required.")
    return Decimal(levels[0][0]["px"]), Decimal(levels[1][0]["px"]), raw


def rounded_price(value: Decimal, sz_decimals: int) -> Decimal:
    decimals = max(0, 8 - sz_decimals)
    return Decimal(str(round(float(f"{value:.5g}"), decimals)))


def available(info: Info, address: str, coin: str) -> Decimal:
    for item in info.spot_user_state(address).get("balances", []):
        if str(item.get("coin", "")).upper() == coin.upper():
            return Decimal(str(item.get("total", "0"))) - Decimal(str(item.get("hold", "0")))
    return Decimal("0")


# 2) One bounded Testnet maker/taker pair for repeatability pilots.
def main() -> None:
    """Validate caps, place a resting maker, then a matching IOC taker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=sorted(ALLOWED_MARKETS), required=True)
    parser.add_argument("--seller-slot", choices=("W2", "W4"), required=True)
    parser.add_argument("--buyer-slot", choices=("W1", "W3"), required=True)
    parser.add_argument("--regime", choices=("edge", "mid"), required=True)
    parser.add_argument("--target-notional", type=Decimal, default=Decimal("10.5"))
    parser.add_argument("--delay-ms", type=int, default=250)
    parser.add_argument("--allow-book-race", action="store_true")
    parser.add_argument("--execute-testnet-pair", action="store_true")
    args = parser.parse_args()
    if not args.execute_testnet_pair:
        raise SystemExit("REFUSED: explicit Testnet pair flag is required.")
    if not 100 <= args.delay_ms <= 1500:
        raise SystemExit("REFUSED: paired pilot delay must remain within 100-1500 ms.")

    # All signing material remains local and is checked against Agent addresses.
    raw = preflight.read_config(REPO_ROOT / ".env.local")
    cfg = preflight.validate_config(raw, signed_actions_required=True)
    order_cap = Decimal(preflight.require(raw, "HL_MAX_ORDER_NOTIONAL_USD"))
    run_cap = Decimal(preflight.require(raw, "HL_MAX_RUN_NOTIONAL_USD"))
    slippage_cap = Decimal(preflight.require(raw, "HL_MAX_SLIPPAGE_BPS"))
    if args.target_notional > order_cap or args.target_notional > run_cap or args.target_notional < Decimal("10"):
        raise SystemExit("REFUSED: target notional violates configured safety caps.")

    by_slot = {w.slot: w for w in cfg.wallets}
    seller_cfg, buyer_cfg = by_slot[args.seller_slot], by_slot[args.buyer_slot]
    seller_signer, buyer_signer = Account.from_key(seller_cfg.private_key), Account.from_key(buyer_cfg.private_key)
    if seller_signer.address.lower() != seller_cfg.agent or buyer_signer.address.lower() != buyer_cfg.agent:
        raise SystemExit("REFUSED: configured Agent key mismatch.")
    info = Info(cfg.api_url, skip_ws=True)
    market = resolve(info, args.market)
    bid, ask, before_book = top(cfg.api_url, market["wire"])
    mid = (bid + ask) / 2
    tick = Decimal(1).scaleb(-(8 - market["szDecimals"]))
    if args.regime == "edge":
        proposed = max(bid + tick, mid * (Decimal(1) - min(slippage_cap, Decimal("45")) / Decimal(10000)))
    else:
        proposed = mid
    price = rounded_price(proposed, market["szDecimals"])
    if not bid < price < ask:
        raise SystemExit("REFUSED: computed maker price is no longer strictly inside the spread.")
    deviation = abs(price - mid) / mid * Decimal(10000)
    if deviation > slippage_cap:
        raise SystemExit("REFUSED: computed price exceeds configured deviation cap.")
    quantum = Decimal(1).scaleb(-market["szDecimals"])
    size = (args.target_notional / price / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum
    notional = size * price
    if notional > order_cap:
        raise SystemExit("REFUSED: rounded order exceeds configured order cap.")
    seller_before = available(info, seller_cfg.master, market["base"])
    if seller_before < size:
        raise SystemExit("REFUSED: seller lacks prepared spot inventory.")

    started_ms = int(time.time() * 1000) - 1000
    maker_raw, taker_raw = "0x" + secrets.token_hex(16), "0x" + secrets.token_hex(16)
    seller_ex = Exchange(seller_signer, cfg.api_url, account_address=seller_cfg.master)
    buyer_ex = Exchange(buyer_signer, cfg.api_url, account_address=buyer_cfg.master)
    maker_response = seller_ex.order(
        market["display"], False, float(size), float(price), {"limit": {"tif": "Alo"}}, False, Cloid.from_str(maker_raw)
    )
    maker_status = status(maker_response)
    if not isinstance(maker_status, dict) or "resting" not in maker_status:
        raise SystemExit("Maker order did not rest; no taker order was sent.")
    time.sleep(args.delay_ms / 1000)
    fresh_bid, fresh_ask, pre_ioc_book = top(cfg.api_url, market["wire"])
    book_race_observed = fresh_ask != price
    if book_race_observed and not args.allow_book_race:
        seller_ex.cancel_by_cloid(market["display"], Cloid.from_str(maker_raw))
        raise SystemExit("Maker was no longer best ask; order canceled before taker submission.")
    taker_response = buyer_ex.order(
        market["display"], True, float(size), float(price), {"limit": {"tif": "Ioc"}}, False, Cloid.from_str(taker_raw)
    )
    taker_status = status(taker_response)
    time.sleep(1)
    seller_after = available(info, seller_cfg.master, market["base"])
    recent = api_post(cfg.api_url, {"type": "recentTrades", "coin": market["wire"]})
    expected_users = {seller_cfg.master.lower(), buyer_cfg.master.lower()}
    matches = [
        trade for trade in recent if isinstance(trade, dict)
        and int(trade.get("time", 0)) >= started_ms
        and set(str(x).lower() for x in trade.get("users", [])) == expected_users
        and Decimal(str(trade.get("px", "0"))) == price
        and Decimal(str(trade.get("sz", "0"))) == size
    ]
    valid = isinstance(taker_status, dict) and "filled" in taker_status and seller_after < seller_before and len(matches) >= 1
    cleanup_response = None
    if not valid:
        try:
            cleanup_response = seller_ex.cancel_by_cloid(market["display"], Cloid.from_str(maker_raw))
        except Exception:
            cleanup_response = {"status": "cleanup_failed_or_already_terminal"}
    report = {
        "schemaVersion": 1,
        "study": "Orderbook A/C paired fill",
        "network": "testnet",
        "market": market,
        "regime": args.regime,
        "roles": {"seller": seller_cfg.master, "buyer": buyer_cfg.master},
        "bookBefore": before_book,
        "bookBeforeIoc": pre_ioc_book,
        "metrics": {"bid": str(bid), "ask": str(ask), "mid": str(mid), "price": str(price), "size": str(size), "notional": str(notional), "priceDeviationBps": str((price-mid)/mid*Decimal(10000)), "configuredDelayMs": args.delay_ms, "freshBestAskBeforeIoc": str(fresh_ask), "bookRaceObserved": book_race_observed},
        "orders": {"makerCloid": maker_raw, "takerCloid": taker_raw, "makerResponse": maker_response, "takerResponse": taker_response},
        "makerCleanupIfInvalid": cleanup_response,
        "publicPairTrades": matches,
        "sellerBaseBalance": {"before": str(seller_before), "after": str(seller_after)},
        "classification": "VALID_DIRECT_PAIR" if valid else "FAILED_OR_CONTAMINATED",
        "capturedAt": datetime.now(timezone.utc).isoformat(),
    }
    serialized = json.dumps(report, indent=2, ensure_ascii=False)
    for value in [seller_cfg.private_key, buyer_cfg.private_key]:
        if value.lower().removeprefix("0x") in serialized.lower():
            raise SystemExit("REFUSED: secret redaction invariant failed.")
    out = REPO_ROOT / "결과" / "abc_study"
    out.mkdir(parents=True, exist_ok=True)
    destination = out / f"spot_{args.market.split('/')[0].lower()}_{args.regime}_{args.seller_slot.lower()}_{args.buyer_slot.lower()}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{secrets.token_hex(3)}.json"
    destination.write_text(serialized + "\n", encoding="utf-8")
    print(f"Spot pair trial: {report['classification']} {args.market} {args.regime} {price}x{size}")
    print(f"Artifact: {destination.resolve()}")
    raise SystemExit(0 if valid else 4)


if __name__ == "__main__":
    main()
