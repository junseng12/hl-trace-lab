"""Run one bounded TEEYI/USDC split-fill phase on Hyperliquid Testnet."""

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
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_ENV = REPO_ROOT / ".env.local"


def post(requests_module, api_url: str, payload: dict) -> object:
    response = requests_module.post(api_url.rstrip("/") + "/info", json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def trade_key(row: dict) -> tuple[str, str, str] | None:
    if row.get("time") is None or row.get("coin") is None or row.get("tid") is None:
        return None
    return str(row["time"]), str(row["coin"]), str(row["tid"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-root", type=Path, default=SCRIPT_DIR)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--phase", choices=("t2", "t3"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute-testnet-split", action="store_true")
    args = parser.parse_args()
    if not args.execute_testnet_split:
        raise SystemExit("REFUSED: explicit Testnet split flag is required.")

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

    raw = preflight.read_config(args.env)
    if raw.get("HL_ENABLE_SIGNED_ACTIONS", "").strip().lower() != "false":
        raise SystemExit("REFUSED: source config must remain locked.")
    memory = dict(raw)
    memory["HL_ENABLE_SIGNED_ACTIONS"] = "true"
    cfg = preflight.validate_config(memory, signed_actions_required=True)
    order_cap = Decimal(str(cfg.safety_caps["HL_MAX_ORDER_NOTIONAL_USD"]))
    run_cap = Decimal(str(cfg.safety_caps["HL_MAX_RUN_NOTIONAL_USD"]))
    if order_cap < Decimal("11.0") or run_cap < Decimal("11.0"):
        raise SystemExit("REFUSED: configured caps are below the phase requirement.")

    by_slot = {wallet.slot: wallet for wallet in cfg.wallets}
    info = Info(cfg.api_url, skip_ws=True)
    resolved = spot.resolve(info, "TEEYI/USDC")
    market = a1.SpotMarket(
        resolved["display"], resolved["wire"], int(info.coin_to_asset[resolved["wire"]]),
        resolved["base"], "USDC", resolved["szDecimals"],
    )
    quantum = Decimal(1).scaleb(-market.sz_decimals)

    def size_for(notional: Decimal, price: Decimal) -> Decimal:
        return (notional / price / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum

    def make_exchange(slot: str):
        wallet = by_slot[slot]
        signer = Account.from_key(wallet.private_key)
        if signer.address.lower() != wallet.agent:
            raise RuntimeError(f"{slot} Agent key mismatch.")
        return Exchange(signer, cfg.api_url, account_address=wallet.master)

    participants = ("W1", "W2", "W3") if args.phase == "t2" else ("W1", "W2", "W4")
    exchanges = {slot: make_exchange(slot) for slot in participants}
    capture = a1.WsCapture(cfg.api_url, market, cfg.wallets)
    capture.start()
    cleanup: list[dict] = []
    resting: list[tuple[str, str]] = []

    def cloid() -> str:
        return "0x" + secrets.token_hex(16)

    def submit(slot: str, is_buy: bool, size: Decimal, price: Decimal, tif: str, cid: str) -> dict:
        if size * price > order_cap:
            raise RuntimeError("Order exceeds configured cap.")
        return exchanges[slot].order(
            market.display_name, is_buy, float(size), float(price),
            {"limit": {"tif": tif}}, False, Cloid.from_str(cid),
        )

    def cancel(slot: str, cid: str) -> None:
        try:
            result = exchanges[slot].cancel_by_cloid(market.display_name, Cloid.from_str(cid))
            cleanup.append({"slot": slot, "cloid": cid, "result": result})
        except Exception:
            cleanup.append({"slot": slot, "cloid": cid, "result": "failed_or_already_terminal"})

    started_ms = int(time.time() * 1000) - 1_000
    phase_data: dict = {}
    try:
        bid, ask, book = spot.top(cfg.api_url, market.wire_coin)
        mid = (bid + ask) / Decimal("2")
        p1 = spot.rounded_price(bid + (ask - bid) * Decimal("0.25"), market.sz_decimals)
        if not bid < p1 < ask:
            raise RuntimeError("Could not derive a safe first maker price.")

        if args.phase == "t2":
            p2 = spot.rounded_price(bid + (ask - bid) * Decimal("0.65"), market.sz_decimals)
            if not p1 < p2 < ask:
                raise RuntimeError("Could not derive a safe second maker price.")
            s1, s2 = size_for(Decimal("10.2"), p1), size_for(Decimal("10.2"), p2)
            st = size_for(Decimal("11.0"), p2)
            if not s1 < st < s1 + s2:
                raise RuntimeError("T2 sizes do not force a split across both makers.")
            if spot.available(info, by_slot["W1"].master, "TEEYI") < s1 or spot.available(info, by_slot["W3"].master, "TEEYI") < s2:
                raise RuntimeError("Controlled makers lack TEEYI inventory.")
            c1, c2, ct = cloid(), cloid(), cloid()
            r1 = submit("W1", False, s1, p1, "Alo", c1); resting.append(("W1", c1))
            r2 = submit("W3", False, s2, p2, "Alo", c2); resting.append(("W3", c2))
            if not all(isinstance(spot.status(r), dict) and "resting" in spot.status(r) for r in (r1, r2)):
                raise RuntimeError("One or more T2 maker orders did not rest.")
            time.sleep(0.4)
            rt = submit("W2", True, st, p2, "Ioc", ct)
            time.sleep(3)
            recent = post(requests, cfg.api_url, {"type": "recentTrades", "coin": market.wire_coin})
            buyer = by_slot["W2"].master.lower()
            sellers = {by_slot["W1"].master.lower(), by_slot["W3"].master.lower()}
            matches = [row for row in recent if isinstance(row, dict) and int(row.get("time", 0)) >= started_ms and len(row.get("users", [])) == 2 and str(row["users"][0]).lower() == buyer and str(row["users"][1]).lower() in sellers]
            valid = {str(row["users"][1]).lower() for row in matches} == sellers and len({trade_key(row) for row in matches}) >= 2
            phase_data = {"phase": "T2_taker_split", "valid": valid, "bookBefore": book, "prices": [str(p1), str(p2)], "sizes": [str(s1), str(s2), str(st)], "orders": [r1, r2, rt], "publicTrades": matches}
        else:
            sm = size_for(Decimal("11.0"), p1)
            st = size_for(Decimal("10.2"), p1)
            if not st < sm < st * 2:
                raise RuntimeError("T3 sizes do not force two takers to split one maker order.")
            if spot.available(info, by_slot["W1"].master, "TEEYI") < sm:
                raise RuntimeError("Controlled maker lacks TEEYI inventory.")
            cm, ca, cb = cloid(), cloid(), cloid()
            rm = submit("W1", False, sm, p1, "Alo", cm); resting.append(("W1", cm))
            if not isinstance(spot.status(rm), dict) or "resting" not in spot.status(rm):
                raise RuntimeError("T3 maker order did not rest.")
            time.sleep(0.35)
            ra = submit("W2", True, st, p1, "Ioc", ca)
            time.sleep(0.35)
            rb = submit("W4", True, st, p1, "Ioc", cb)
            time.sleep(3)
            recent = post(requests, cfg.api_url, {"type": "recentTrades", "coin": market.wire_coin})
            seller = by_slot["W1"].master.lower()
            buyers = {by_slot["W2"].master.lower(), by_slot["W4"].master.lower()}
            matches = [row for row in recent if isinstance(row, dict) and int(row.get("time", 0)) >= started_ms and len(row.get("users", [])) == 2 and str(row["users"][0]).lower() in buyers and str(row["users"][1]).lower() == seller]
            valid = {str(row["users"][0]).lower() for row in matches} == buyers and len({trade_key(row) for row in matches}) >= 2
            phase_data = {"phase": "T3_maker_split", "valid": valid, "bookBefore": book, "price": str(p1), "sizes": [str(sm), str(st), str(st)], "orders": [rm, ra, rb], "publicTrades": matches}
    finally:
        for slot, cid in resting:
            cancel(slot, cid)
        time.sleep(1)
        websocket = capture.snapshot()
        capture.stop()

    controlled_keys = {trade_key(row) for row in phase_data.get("publicTrades", [])} - {None}
    accounts = {}
    for slot, wallet in by_slot.items():
        fills = post(requests, cfg.api_url, {"type": "userFills", "user": wallet.master, "aggregateByTime": False})
        accounts[slot] = {"master": wallet.master, "matchingFills": [row for row in fills if isinstance(row, dict) and trade_key(row) in controlled_keys], "openOrders": info.frontend_open_orders(wallet.master)}
    ws_rows = a1.flatten_public_trades(websocket)
    report = {
        "schemaVersion": 1,
        "experiment": "Experiment 1 split-fill pilot",
        "network": "Hyperliquid Testnet",
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "sourceConfigWasLocked": True,
        "signedEnablement": "in-memory only",
        "market": resolved,
        "classification": "VALID" if phase_data.get("valid") else "INVALID",
        "phase": phase_data,
        "controlledTradeKeys": [{"time": item[0], "coin": item[1], "tid": item[2]} for item in sorted(controlled_keys)],
        "websocketMatches": [row for row in ws_rows if trade_key(row) in controlled_keys],
        "accounts": accounts,
        "cleanup": cleanup,
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    preflight.ensure_no_secrets(serialized, cfg.wallets)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized + "\n", encoding="utf-8")
    print(json.dumps({"classification": report["classification"], "phase": phase_data.get("phase"), "trades": len(phase_data.get("publicTrades", [])), "websocketMatches": len(report["websocketMatches"]), "output": str(args.output.resolve())}, ensure_ascii=False))
    raise SystemExit(0 if report["classification"] == "VALID" else 4)


if __name__ == "__main__":
    main()
