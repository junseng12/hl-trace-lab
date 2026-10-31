"""Collect public Hyperliquid evidence around one manually executed trade.

This program is read-only. It never loads a private key and never sends an order.
Start it first, wait for READY, make a normal trade in the Hyperliquid UI, then
return to the terminal and press Enter. The program saves before/after REST
snapshots plus the live public trades WebSocket stream.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import websocket
except ModuleNotFoundError:
    websocket = None


NETWORKS = {
    "testnet": {
        "api": "https://api.hyperliquid-testnet.xyz",
        "ws": "wss://api.hyperliquid-testnet.xyz/ws",
    },
    "mainnet": {
        "api": "https://api.hyperliquid.xyz",
        "ws": "wss://api.hyperliquid.xyz/ws",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def post_info(api_url: str, payload: dict[str, Any]) -> Any:
    response = requests.post(
        api_url.rstrip("/") + "/info",
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def resolve_market(api_url: str, market: str) -> dict[str, str]:
    """Resolve PERP symbols directly and BASE/QUOTE spot pairs via spotMeta."""
    market = market.strip()
    if "/" not in market:
        return {"display": market, "wire": market, "kind": "perp"}

    base, quote = [part.strip().upper() for part in market.split("/", 1)]
    meta = post_info(api_url, {"type": "spotMeta"})
    tokens = {int(item["index"]): str(item["name"]).upper() for item in meta["tokens"]}
    matches = []
    for item in meta["universe"]:
        token_ids = item.get("tokens", [])
        if len(token_ids) != 2:
            continue
        candidate = (tokens.get(int(token_ids[0])), tokens.get(int(token_ids[1])))
        if candidate == (base, quote):
            matches.append(item)
    if len(matches) != 1:
        raise SystemExit(
            f"Could not resolve exactly one spot market for {market!r}; matches={len(matches)}. "
            "Use the API wire coin directly (for example @107) if necessary."
        )
    return {"display": f"{base}/{quote}", "wire": str(matches[0]["name"]), "kind": "spot"}


def fill_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    if row.get("time") is None or row.get("coin") is None or row.get("tid") is None:
        return None
    return str(row["time"]), str(row["coin"]), str(row["tid"])


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class TradeStream:
    def __init__(self, ws_url: str, coin: str, output_path: Path) -> None:
        self.ws_url = ws_url
        self.coin = coin
        self.output_path = output_path
        self.ready = threading.Event()
        self.rows: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._file = None
        self._app: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._file = self.output_path.open("a", encoding="utf-8")

        def on_open(app: websocket.WebSocketApp) -> None:
            app.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": self.coin},
            }))

        def on_message(_app: websocket.WebSocketApp, message: str) -> None:
            try:
                payload = json.loads(message)
                channel = payload.get("channel")
                if channel == "subscriptionResponse":
                    self.ready.set()
                    return
                if channel != "trades":
                    return
                data = payload.get("data", [])
                rows = data if isinstance(data, list) else [data]
                captured_at = utc_now()
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    item = {"capturedAt": captured_at, **row}
                    with self._lock:
                        self.rows.append(item)
                        assert self._file is not None
                        self._file.write(json.dumps(item, ensure_ascii=False) + "\n")
                        self._file.flush()
            except Exception as exc:  # Preserve collection errors in the report.
                self.errors.append(repr(exc))

        def on_error(_app: websocket.WebSocketApp, error: object) -> None:
            self.errors.append(str(error))

        self._app = websocket.WebSocketApp(
            self.ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
        )
        self._thread = threading.Thread(
            target=lambda: self._app.run_forever(ping_interval=25, ping_timeout=10),
            daemon=True,
        )
        self._thread.start()
        if not self.ready.wait(timeout=15):
            self.stop()
            raise SystemExit(f"WebSocket subscription did not become ready: {self.errors}")

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.rows)

    def stop(self) -> None:
        if self._app is not None:
            self._app.close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._file is not None:
            self._file.close()
            self._file = None


def account_snapshot(api_url: str, user: str) -> dict[str, Any]:
    return {
        "capturedAt": utc_now(),
        "user": user.lower(),
        "userFills": post_info(api_url, {
            "type": "userFills",
            "user": user,
            "aggregateByTime": False,
        }),
        "openOrders": post_info(api_url, {"type": "frontendOpenOrders", "user": user}),
    }


def market_snapshot(api_url: str, wire_coin: str) -> dict[str, Any]:
    return {
        "capturedAt": utc_now(),
        "coin": wire_coin,
        "recentTrades": post_info(api_url, {"type": "recentTrades", "coin": wire_coin}),
        "l2Book": post_info(api_url, {"type": "l2Book", "coin": wire_coin}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=sorted(NETWORKS), required=True)
    parser.add_argument("--user", required=True, help="Master/trading account address, not Agent address")
    parser.add_argument("--market", required=True, help="PERP symbol (HYPE) or spot pair (HYPE/USDC)")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--settle-seconds", type=int, default=8)
    args = parser.parse_args()

    if websocket is None:
        raise SystemExit(
            "Missing dependency 'websocket-client'. Run: "
            "python -m pip install -r experiments/hl_experiment1_repro/requirements.txt"
        )

    if not args.user.startswith("0x") or len(args.user) != 42:
        raise SystemExit("--user must be a 42-character 0x address.")
    if args.settle_seconds < 3 or args.settle_seconds > 120:
        raise SystemExit("--settle-seconds must be between 3 and 120.")

    network = NETWORKS[args.network]
    market = resolve_market(network["api"], args.market)
    args.output_dir.mkdir(parents=True, exist_ok=False)

    manifest = {
        "schemaVersion": 1,
        "experiment": "Hyperliquid Experiment 1 public counterparty capture",
        "network": args.network,
        "apiUrl": network["api"],
        "wsUrl": network["ws"],
        "user": args.user.lower(),
        "market": market,
        "startedAt": utc_now(),
        "signedActions": False,
    }
    write_json(args.output_dir / "manifest.json", manifest)

    pre_account = account_snapshot(network["api"], args.user)
    pre_market = market_snapshot(network["api"], market["wire"])
    write_json(args.output_dir / "01_pre_account.json", pre_account)
    write_json(args.output_dir / "02_pre_market.json", pre_market)

    stream = TradeStream(network["ws"], market["wire"], args.output_dir / "03_ws_trades.ndjson")
    stream.start()
    print()
    print("READY")
    print(f"Network : {args.network}")
    print(f"Account : {args.user.lower()}")
    print(f"Market  : {market['display']} (API coin: {market['wire']})")
    print("브라우저에서 이 계정으로 정상적인 소액 거래 1건을 체결하세요.")
    print("체결이 완료되면 이 터미널로 돌아와 Enter를 누르세요.")
    input()
    print(f"마지막 이벤트 반영을 위해 {args.settle_seconds}초 기다립니다...")
    time.sleep(args.settle_seconds)

    try:
        post_account = account_snapshot(network["api"], args.user)
        post_market = market_snapshot(network["api"], market["wire"])
    finally:
        ws_rows = stream.snapshot()
        stream.stop()

    write_json(args.output_dir / "04_post_account.json", post_account)
    write_json(args.output_dir / "05_post_market.json", post_market)

    pre_keys = {
        fill_key(row) for row in pre_account["userFills"]
        if isinstance(row, dict) and fill_key(row) is not None
    }
    new_fills = [
        row for row in post_account["userFills"]
        if isinstance(row, dict)
        and row.get("coin") == market["wire"]
        and fill_key(row) is not None
        and fill_key(row) not in pre_keys
    ]
    new_keys = {fill_key(row) for row in new_fills}
    ws_matches = [row for row in ws_rows if fill_key(row) in new_keys]
    recent_matches = [
        row for row in post_market["recentTrades"]
        if isinstance(row, dict) and fill_key(row) in new_keys
    ]

    evidence = []
    by_key_ws = {fill_key(row): row for row in ws_matches}
    by_key_recent = {fill_key(row): row for row in recent_matches}
    for fill in new_fills:
        key = fill_key(fill)
        market_row = by_key_ws.get(key) or by_key_recent.get(key)
        users = market_row.get("users", []) if isinstance(market_row, dict) else []
        counterparty = None
        if isinstance(users, list) and len(users) == 2:
            own = args.user.lower()
            lowered = [str(item).lower() for item in users]
            if own in lowered:
                counterparty = lowered[1] if lowered[0] == own else lowered[0]
        evidence.append({
            "key": {"time": key[0], "coin": key[1], "tid": key[2]} if key else None,
            "userFill": fill,
            "marketTrade": market_row,
            "counterparty": counterparty,
            "counterpartyRecovered": counterparty is not None,
            "source": "websocket" if key in by_key_ws else ("recentTrades" if key in by_key_recent else None),
        })

    summary = {
        **manifest,
        "finishedAt": utc_now(),
        "newUserFills": len(new_fills),
        "websocketRowsCaptured": len(ws_rows),
        "websocketErrors": stream.errors,
        "fillsMatchedToWebsocket": len(ws_matches),
        "fillsMatchedToRecentTrades": len(recent_matches),
        "counterpartiesRecovered": sum(item["counterpartyRecovered"] for item in evidence),
        "evidence": evidence,
    }
    write_json(args.output_dir / "06_summary.json", summary)
    print(json.dumps({
        "status": "complete",
        "output": str(args.output_dir.resolve()),
        "newUserFills": summary["newUserFills"],
        "counterpartiesRecovered": summary["counterpartiesRecovered"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
