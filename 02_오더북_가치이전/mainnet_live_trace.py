"""Read-only mainnet orderbook counterparty tracer.

Subscribe to public ``trades`` streams before a normal UI trade, snapshot the
target accounts' ``userFills`` before/after, and join records with the stable
key ``(coin, time, tid)``.  No private key is read and no order is sent.
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


API_URL = "https://api.hyperliquid.xyz"
WS_URL = "wss://api.hyperliquid.xyz/ws"


# ---------------------------------------------------------------------------
# 1) Common helpers and public Info API calls
# ---------------------------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_address(value: str) -> str:
    value = value.strip().lower()
    if not value.startswith("0x") or len(value) != 42:
        raise SystemExit(f"Invalid 0x address: {value!r}")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise SystemExit(f"Invalid 0x address: {value!r}") from exc
    return value


def post_info(payload: dict[str, Any]) -> Any:
    """Call the public HyperCore Info API; no authentication is required."""
    response = requests.post(API_URL + "/info", json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def resolve_markets(markets: list[str]) -> list[dict[str, str]]:
    """Resolve PERP symbols and BASE/QUOTE spot pairs to API coin identifiers."""
    spot_meta = None
    resolved: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in markets:
        market = raw.strip()
        if "/" not in market:
            item = {"display": market.upper(), "wire": market, "kind": "perp"}
        else:
            if spot_meta is None:
                spot_meta = post_info({"type": "spotMeta"})
            base, quote = [part.strip().upper() for part in market.split("/", 1)]
            tokens = {
                int(token["index"]): str(token["name"]).upper()
                for token in spot_meta["tokens"]
            }
            matches = []
            for universe in spot_meta["universe"]:
                ids = universe.get("tokens", [])
                if len(ids) == 2 and (
                    tokens.get(int(ids[0])), tokens.get(int(ids[1]))
                ) == (base, quote):
                    matches.append(universe)
            if len(matches) != 1:
                raise SystemExit(
                    f"Could not resolve exactly one spot market for {market!r}. "
                    "Pass its API coin such as @107 if necessary."
                )
            item = {
                "display": f"{base}/{quote}",
                "wire": str(matches[0]["name"]),
                "kind": "spot",
            }
        if item["wire"] not in seen:
            resolved.append(item)
            seen.add(item["wire"])
    return resolved


def fill_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return Hyperliquid's stable per-fill join key.

    ``tid`` alone is not globally unique, so coin and block time are retained.
    The same key appears in the target's fill and the public market trade.
    """
    if row.get("coin") is None or row.get("time") is None or row.get("tid") is None:
        return None
    return str(row["coin"]), str(row["time"]), str(row["tid"])


def counterparty_from_trade(trade: dict[str, Any] | None, user: str) -> str | None:
    """Remove the target address from ``users=[buyer, seller]``."""
    if not isinstance(trade, dict):
        return None
    users = trade.get("users")
    if not isinstance(users, list) or len(users) != 2:
        return None
    normalized = [str(value).lower() for value in users]
    if user not in normalized:
        return None
    return normalized[1] if normalized[0] == user else normalized[0]


def user_fills(user: str) -> list[dict[str, Any]]:
    """Read the target wallet's recent fills (price, size, side, PnL, tid)."""
    result = post_info({
        "type": "userFills",
        "user": user,
        "aggregateByTime": False,
    })
    if not isinstance(result, list):
        raise RuntimeError("Unexpected userFills response")
    return [row for row in result if isinstance(row, dict)]


def market_snapshot(coin: str) -> dict[str, Any]:
    """Save short-lived recent trades and the current order book as context."""
    return {
        "capturedAt": utc_now(),
        "coin": coin,
        "recentTrades": post_info({"type": "recentTrades", "coin": coin}),
        "l2Book": post_info({"type": "l2Book", "coin": coin}),
    }


# ---------------------------------------------------------------------------
# 2) Real-time public market collector
# ---------------------------------------------------------------------------
class MultiMarketTradeStream:
    """Subscribe to ``trades`` for several markets on one WebSocket.

    Each received trade is appended immediately to NDJSON so an interruption
    does not erase evidence already collected in memory.
    """
    def __init__(self, coins: list[str], output: Path) -> None:
        self.coins = coins
        self.output = output
        self.ready: set[str] = set()
        self.ready_event = threading.Event()
        self.rows: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._file = None
        self._app: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Open the socket, subscribe every requested coin, and wait for ACKs."""
        self._file = self.output.open("a", encoding="utf-8")

        def on_open(app: Any) -> None:
            for coin in self.coins:
                app.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": coin},
                }))

        def on_message(_app: Any, message: str) -> None:
            try:
                payload = json.loads(message)
                if payload.get("channel") == "subscriptionResponse":
                    # GitHub docs define data.subscription as the original
                    # subscription; collect all ACKs before printing READY.
                    data = payload.get("data", {})
                    subscription = data.get("subscription", {}) if isinstance(data, dict) else {}
                    coin = subscription.get("coin")
                    if coin in self.coins:
                        self.ready.add(coin)
                    if len(self.ready) == len(self.coins):
                        self.ready_event.set()
                    return
                if payload.get("channel") != "trades":
                    return
                data = payload.get("data", [])
                for row in data if isinstance(data, list) else [data]:
                    if not isinstance(row, dict) or str(row.get("coin")) not in self.coins:
                        continue
                    item = {"capturedAt": utc_now(), **row}
                    with self._lock:
                        self.rows.append(item)
                        assert self._file is not None
                        self._file.write(json.dumps(item, ensure_ascii=False) + "\n")
                        self._file.flush()
            except Exception as exc:
                self.errors.append(repr(exc))

        def on_error(_app: Any, error: object) -> None:
            self.errors.append(str(error))

        self._app = websocket.WebSocketApp(
            WS_URL, on_open=on_open, on_message=on_message, on_error=on_error
        )
        self._thread = threading.Thread(
            target=lambda: self._app.run_forever(ping_interval=25, ping_timeout=10),
            daemon=True,
        )
        self._thread.start()
        if not self.ready_event.wait(timeout=20):
            self.stop()
            raise SystemExit(
                f"WebSocket subscriptions were not all confirmed. "
                f"ready={sorted(self.ready)}, errors={self.errors}"
            )

    def snapshot(self) -> list[dict[str, Any]]:
        """Return a thread-safe copy for final matching."""
        with self._lock:
            return list(self.rows)

    def stop(self) -> None:
        """Close the socket and evidence file cleanly."""
        if self._app is not None:
            self._app.close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._file is not None:
            self._file.close()
            self._file = None


# ---------------------------------------------------------------------------
# 3) Experiment workflow: snapshot -> collect -> snapshot -> join
# ---------------------------------------------------------------------------
def main() -> None:
    """Run one read-only Mainnet observation session.

    The user manually performs a normal UI trade only after READY.  This code
    compares before/after userFills and joins each new fill to the market-wide
    stream by (coin, time, tid).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", action="append", required=True,
                        help="Target Master/trading address; repeat for multiple users")
    parser.add_argument("--market", action="append", required=True,
                        help="HYPE, HYPE/USDC or API coin; repeat for multiple markets")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=int,
                        help="Automatic capture duration; omit to stop with Enter")
    parser.add_argument("--settle-seconds", type=int, default=8)
    parser.add_argument("--crosscheck-counterparties", action="store_true",
                        help="Query each recovered counterparty's public userFills")
    args = parser.parse_args()

    if websocket is None:
        raise SystemExit("Install dependencies: python -m pip install -r requirements.txt")
    if args.duration_seconds is not None and not 5 <= args.duration_seconds <= 86400:
        raise SystemExit("--duration-seconds must be between 5 and 86400")
    if not 3 <= args.settle_seconds <= 120:
        raise SystemExit("--settle-seconds must be between 3 and 120")

    users = list(dict.fromkeys(normalize_address(user) for user in args.user))
    markets = resolve_markets(args.market)
    coins = [market["wire"] for market in markets]
    args.output_dir.mkdir(parents=True, exist_ok=False)
    started_monotonic = time.monotonic()
    manifest = {
        "schemaVersion": 1,
        "experiment": "Hyperliquid mainnet public orderbook tracing",
        "network": "mainnet",
        "apiUrl": API_URL,
        "wsUrl": WS_URL,
        "users": users,
        "markets": markets,
        "startedAt": utc_now(),
        "signedActions": False,
    }
    write_json(args.output_dir / "00_manifest.json", manifest)

    # Before-state is required to distinguish new fills from older history.
    pre_accounts = {user: user_fills(user) for user in users}
    pre_markets = {coin: market_snapshot(coin) for coin in coins}
    write_json(args.output_dir / "01_pre_accounts.json", pre_accounts)
    write_json(args.output_dir / "02_pre_markets.json", pre_markets)

    stream = MultiMarketTradeStream(coins, args.output_dir / "03_ws_trades.ndjson")
    stream.start()
    print("READY - WebSocket subscriptions confirmed")
    print("Users   : " + ", ".join(users))
    print("Markets : " + ", ".join(f"{m['display']} ({m['wire']})" for m in markets))
    print("브라우저에서 정상적인 소액 거래를 수행하세요. 이 스크립트는 주문하지 않습니다.")
    if args.duration_seconds is None:
        input("완료 후 Enter: ")
    else:
        time.sleep(args.duration_seconds)
    time.sleep(args.settle_seconds)

    try:
        post_accounts = {user: user_fills(user) for user in users}
        post_markets = {coin: market_snapshot(coin) for coin in coins}
    finally:
        ws_rows = stream.snapshot()
        stream.stop()
    write_json(args.output_dir / "04_post_accounts.json", post_accounts)
    write_json(args.output_dir / "05_post_markets.json", post_markets)

    # Build lookup tables once; matching then remains deterministic and fast.
    ws_index = {fill_key(row): row for row in ws_rows if fill_key(row) is not None}
    recent_index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for snapshot in post_markets.values():
        rows = snapshot.get("recentTrades", [])
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and fill_key(row) is not None:
                recent_index[fill_key(row)] = row

    evidence = []
    counterparty_fills_cache: dict[str, list[dict[str, Any]]] = {}
    for user in users:
        pre_keys = {fill_key(row) for row in pre_accounts[user] if fill_key(row) is not None}
        for fill in post_accounts[user]:
            key = fill_key(fill)
            if key is None or key in pre_keys or key[0] not in coins:
                continue
            trade = ws_index.get(key) or recent_index.get(key)
            # WebSocket is primary evidence. recentTrades is only a short-lived
            # fallback if the fill happened at a reconnect boundary.
            source = "websocket" if key in ws_index else ("recentTrades" if key in recent_index else None)
            counterparty = counterparty_from_trade(trade, user)
            crosscheck = None
            if args.crosscheck_counterparties and counterparty:
                if counterparty not in counterparty_fills_cache:
                    counterparty_fills_cache[counterparty] = user_fills(counterparty)
                    time.sleep(0.15)
                opposite = [
                    row for row in counterparty_fills_cache[counterparty]
                    if fill_key(row) == key
                ]
                crosscheck = {
                    "sameKeyOnCounterpartyUserFills": len(opposite) == 1,
                    "matches": opposite,
                }
            evidence.append({
                "key": {"coin": key[0], "time": key[1], "tid": key[2]},
                "targetUser": user,
                "userFill": fill,
                "publicMarketTrade": trade,
                "counterparty": counterparty,
                "source": source,
                "counterpartyUserFillsCrosscheck": crosscheck,
            })

    elapsed = time.monotonic() - started_monotonic
    ws_path = args.output_dir / "03_ws_trades.ndjson"
    size_bytes = ws_path.stat().st_size if ws_path.exists() else 0
    summary = {
        **manifest,
        "finishedAt": utc_now(),
        "captureElapsedSeconds": round(elapsed, 3),
        "websocketRowsCaptured": len(ws_rows),
        "websocketBytes": size_bytes,
        "websocketErrors": stream.errors,
        "newTargetFills": len(evidence),
        "counterpartiesRecovered": sum(bool(row["counterparty"]) for row in evidence),
        "evidence": evidence,
    }
    write_json(args.output_dir / "06_summary.json", summary)
    print(json.dumps({
        "status": "complete",
        "output": str(args.output_dir.resolve()),
        "newTargetFills": len(evidence),
        "counterpartiesRecovered": summary["counterpartiesRecovered"],
        "websocketRowsCaptured": len(ws_rows),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
