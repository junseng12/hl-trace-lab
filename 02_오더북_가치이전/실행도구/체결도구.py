"""Execute one controlled Experiment 2 A1 pair on Hyperliquid Testnet only.

W1 posts a HYPE/USDC ALO sell strictly inside the spread. After the order is
confirmed resting, W2 sends an IOC buy at exactly the same price. A run is only
classified VALID when public trade data identifies W2 as buyer and W1 as
seller for every fill belonging to the two experiment cloids.
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Callable

import 테스트넷_사전점검 as preflight
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.types import Cloid


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
MIN_ORDER_USD = Decimal("10")
BUYER_BALANCE_BUFFER = Decimal("1.01")
WS_READY_TIMEOUT_SECONDS = 12.0
POST_TRADE_CAPTURE_SECONDS = 3.0
TERMINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "triggered",
    "rejected",
    "expired",
    "margincanceled",
    "vaultwithdrawalcanceled",
    "openinterestcapcanceled",
    "selftradecanceled",
    "reduceonlycanceled",
    "siblingfilledcanceled",
    "delistedcanceled",
    "liquidatedcanceled",
    "scheduledcancel",
    "tickrejected",
    "mintradentlrejected",
    "perpmarginrejected",
    "reduceonlyrejected",
    "badalopxrejected",
    "ioccancelrejected",
    "badtriggerpxrejected",
    "marketordernoliquidityrejected",
    "positionincreaseatopeninterestcaprejected",
    "positionflipatopeninterestcaprejected",
    "tooaggressiveatopeninterestcaprejected",
    "openinterestincreaserejected",
    "insufficientspotbalancerejected",
    "oraclerejected",
    "perpmaxpositionrejected",
}


# ---------------------------------------------------------------------------
# 1) Safety types, configuration loading and immutable experiment plan
# ---------------------------------------------------------------------------
class RunnerError(Exception):
    """Expected safe refusal or invalid experiment condition."""


@dataclass(frozen=True)
class SpotMarket:
    display_name: str
    wire_coin: str
    asset_id: int
    base_token: str
    quote_token: str
    sz_decimals: int


@dataclass(frozen=True)
class A1Plan:
    price: Decimal
    size: Decimal
    notional: Decimal
    best_bid: Decimal
    best_ask: Decimal
    price_tick: Decimal
    target_notional: Decimal


@dataclass
class SubmissionTracker:
    signed_order_attempts: int = 0
    maker_submission_attempted: bool = False


def attempt_signed_order(tracker: SubmissionTracker, role: str, submit: Callable[[], Any]) -> Any:
    if role not in {"maker", "taker"}:
        raise RunnerError("Unknown signed order role.")
    tracker.signed_order_attempts += 1
    if role == "maker":
        tracker.maker_submission_attempted = True
    # Attempt state is updated before the network call. If the response is lost
    # after server acceptance, finally cleanup still queries/cancels the cloid.
    return submit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def decimal_string(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def parse_positive_decimal(raw: str, field: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise RunnerError(f"{field} must be numeric.") from exc
    if not value.is_finite() or value <= 0:
        raise RunnerError(f"{field} must be positive.")
    return value


def load_signed_config(path: Path) -> tuple[dict[str, str], preflight.ValidatedConfig, Decimal]:
    raw = preflight.read_config(path)
    validated = preflight.validate_config(raw, signed_actions_required=True)
    if validated.api_url != preflight.EXPECTED_API_URL:
        raise RunnerError("Runner hard refusal: API URL is not official Testnet.")
    target = parse_positive_decimal(
        preflight.require(raw, "HL_A1_TARGET_NOTIONAL_USD"),
        "HL_A1_TARGET_NOTIONAL_USD",
    )
    order_cap = Decimal(str(validated.safety_caps["HL_MAX_ORDER_NOTIONAL_USD"]))
    run_cap = Decimal(str(validated.safety_caps["HL_MAX_RUN_NOTIONAL_USD"]))
    order_count_cap = int(validated.safety_caps["HL_MAX_ORDERS_PER_RUN"])
    if target < MIN_ORDER_USD:
        raise RunnerError("A1 target notional must be at least $10.")
    if target > order_cap or target > run_cap:
        raise RunnerError("A1 target notional exceeds a configured order or run cap.")
    if order_count_cap < 2:
        raise RunnerError("A1 requires an order-count cap of at least two.")
    return raw, validated, target


# ---------------------------------------------------------------------------
# 2) Resolve market metadata and build a legal, capped maker/taker plan
# ---------------------------------------------------------------------------
def resolve_hype_usdc(info: Any) -> SpotMarket:
    meta = info.spot_meta()
    tokens = meta.get("tokens", []) if isinstance(meta, dict) else []
    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    token_by_index = {token.get("index"): token for token in tokens if isinstance(token, dict)}
    matches: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for market in universe:
        if not isinstance(market, dict) or len(market.get("tokens", [])) != 2:
            continue
        base = token_by_index.get(market["tokens"][0])
        quote = token_by_index.get(market["tokens"][1])
        if not isinstance(base, dict) or not isinstance(quote, dict):
            continue
        if str(base.get("name", "")).upper() == "HYPE" and str(quote.get("name", "")).upper() == "USDC":
            matches.append((market, base, quote))
    if len(matches) != 1:
        raise RunnerError(f"Could not uniquely resolve HYPE/USDC from Testnet metadata; matches={len(matches)}.")

    market, base, quote = matches[0]
    wire_coin = str(market.get("name", ""))
    display_name = f'{base["name"]}/{quote["name"]}'
    if not wire_coin or info.name_to_coin.get(display_name) != wire_coin:
        raise RunnerError("SDK metadata mapping for HYPE/USDC is inconsistent.")
    if wire_coin not in info.coin_to_asset:
        raise RunnerError("SDK did not derive an asset id for HYPE/USDC.")
    asset_id = int(info.coin_to_asset[wire_coin])
    sz_decimals = int(info.asset_to_sz_decimals[asset_id])
    return SpotMarket(
        display_name=display_name,
        wire_coin=wire_coin,
        asset_id=asset_id,
        base_token=str(base["name"]),
        quote_token=str(quote["name"]),
        sz_decimals=sz_decimals,
    )


def best_bid_ask(book: Any) -> tuple[Decimal, Decimal]:
    if not isinstance(book, dict) or not isinstance(book.get("levels"), list) or len(book["levels"]) != 2:
        raise RunnerError("Malformed HYPE/USDC l2Book response.")
    bids = [Decimal(str(level["px"])) for level in book["levels"][0] if isinstance(level, dict) and "px" in level]
    asks = [Decimal(str(level["px"])) for level in book["levels"][1] if isinstance(level, dict) and "px" in level]
    if not bids or not asks:
        raise RunnerError("HYPE/USDC book must contain both bids and asks.")
    best_bid = max(bids)
    best_ask = min(asks)
    if best_bid <= 0 or best_ask <= best_bid:
        raise RunnerError("HYPE/USDC book has an invalid or crossed spread.")
    return best_bid, best_ask


def legal_price_inside_spread(best_bid: Decimal, best_ask: Decimal, sz_decimals: int) -> tuple[Decimal, Decimal]:
    max_decimals = 8 - sz_decimals
    if max_decimals < 0:
        raise RunnerError("Unexpected spot size precision in metadata.")
    midpoint = (best_bid + best_ask) / 2
    decimal_tick = Decimal(1).scaleb(-max_decimals)
    significant_tick = Decimal(1).scaleb(midpoint.adjusted() - 4)
    tick = max(decimal_tick, significant_tick)
    first_index = (best_bid / tick).to_integral_value(rounding=ROUND_FLOOR) + 1
    last_index = (best_ask / tick).to_integral_value(rounding=ROUND_CEILING) - 1
    if first_index > last_index:
        raise RunnerError("Spread contains no legal price strictly between bid and ask.")
    index = (first_index + last_index) // 2
    price = index * tick
    if not best_bid < price < best_ask:
        raise RunnerError("Failed to construct a price strictly inside the spread.")
    return price, tick


def build_plan(
    book: Any,
    market: SpotMarket,
    target: Decimal,
    order_cap: Decimal,
    run_cap: Decimal,
    slippage_cap_bps: Decimal,
) -> A1Plan:
    if target < MIN_ORDER_USD:
        raise RunnerError("Planned order is below the $10 minimum.")
    if target > order_cap or target > run_cap:
        raise RunnerError("Planned target exceeds a configured cap.")
    bid, ask = best_bid_ask(book)
    price, tick = legal_price_inside_spread(bid, ask, market.sz_decimals)
    if price_deviation_bps(price, bid, ask) > slippage_cap_bps:
        raise RunnerError("Inside-spread maker price exceeds the configured slippage cap.")
    size_quantum = Decimal(1).scaleb(-market.sz_decimals)
    size = (target / price).quantize(size_quantum, rounding=ROUND_CEILING)
    notional = size * price
    if notional < MIN_ORDER_USD:
        raise RunnerError("Rounded order notional is below the $10 minimum.")
    if notional > order_cap or notional > run_cap:
        raise RunnerError("Rounded order notional exceeds a configured cap.")
    return A1Plan(price, size, notional, bid, ask, tick, target)


def price_deviation_bps(price: Decimal, best_bid: Decimal, best_ask: Decimal) -> Decimal:
    midpoint = (best_bid + best_ask) / 2
    if midpoint <= 0:
        raise RunnerError("Cannot calculate slippage from a non-positive midpoint.")
    return abs(price - midpoint) * Decimal("10000") / midpoint


def enforce_pre_ioc_book(book_snapshot: dict[str, Any], plan: A1Plan, slippage_cap_bps: Decimal) -> Decimal:
    bid, ask = best_bid_ask(book_snapshot["l2Book"])
    if bid >= plan.price:
        raise RunnerError("Fresh bid reached the maker price; IOC was not sent.")
    if ask != plan.price:
        raise RunnerError("W1 is no longer the fresh best ask at the planned price; IOC was not sent.")
    deviation = price_deviation_bps(plan.price, bid, ask)
    if deviation > slippage_cap_bps:
        raise RunnerError("Fresh pre-IOC price exceeds the configured slippage cap.")
    return deviation


def available_spot_balance(spot_state: Any, coin: str) -> Decimal:
    balances = spot_state.get("balances", []) if isinstance(spot_state, dict) else []
    for balance in balances:
        if isinstance(balance, dict) and str(balance.get("coin", "")).upper() == coin.upper():
            total = Decimal(str(balance.get("total", "0")))
            hold = Decimal(str(balance.get("hold", "0")))
            return max(Decimal("0"), total - hold)
    return Decimal("0")


def verify_balances(before_by_slot: dict[str, Any], market: SpotMarket, plan: A1Plan) -> None:
    w1_spot = before_by_slot["W1"]["spotState"]
    w2_spot = before_by_slot["W2"]["spotState"]
    w1_available = available_spot_balance(w1_spot, market.base_token)
    w2_available = available_spot_balance(w2_spot, market.quote_token)
    if w1_available < plan.size:
        raise RunnerError("W1 lacks available Testnet HYPE for the planned sell.")
    if w2_available < plan.notional * BUYER_BALANCE_BUFFER:
        raise RunnerError("W2 lacks available Testnet USDC including the safety buffer.")


def capture_account_snapshot(info: Any, wallet: preflight.WalletConfig) -> dict[str, Any]:
    public = preflight.public_wallet_state(info, wallet)
    return {
        **public,
        "spotState": info.spot_user_state(wallet.master),
        "fills": info.user_fills(wallet.master),
    }


def capture_all_accounts(info: Any, wallets: tuple[preflight.WalletConfig, ...]) -> dict[str, Any]:
    return {wallet.slot: capture_account_snapshot(info, wallet) for wallet in wallets}


def public_book_snapshot(info: Any, market: SpotMarket) -> dict[str, Any]:
    book = info.l2_snapshot(market.display_name)
    bid, ask = best_bid_ask(book)
    return {
        "capturedAt": now_iso(),
        "l2Book": book,
        "derivedBbo": {"bid": decimal_string(bid), "ask": decimal_string(ask)},
    }


# ---------------------------------------------------------------------------
# 3) Start public evidence capture before any signed order
# ---------------------------------------------------------------------------
class WsCapture:
    def __init__(self, api_url: str, market: SpotMarket, wallets: tuple[preflight.WalletConfig, ...]) -> None:
        self.api_url = api_url
        self.market = market
        self.wallets = wallets
        self.clients: list[Info] = []
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.lock = threading.Lock()

    def callback(self, bucket: str) -> Callable[[Any], None]:
        def receive(message: Any) -> None:
            event = {"receivedAt": now_iso(), "message": message}
            with self.lock:
                self.events.setdefault(bucket, []).append(event)

        return receive

    def start(self) -> None:
        market_info = Info(self.api_url, skip_ws=False)
        self.clients.append(market_info)
        market_info.subscribe({"type": "trades", "coin": self.market.display_name}, self.callback("market.trades"))
        market_info.subscribe({"type": "bbo", "coin": self.market.display_name}, self.callback("market.bbo"))
        market_info.subscribe({"type": "l2Book", "coin": self.market.display_name}, self.callback("market.l2Book"))

        # One Info/WebSocket instance per Master is intentional. The SDK cannot
        # multiplex multiple orderUpdates subscriptions on one instance.
        for wallet in self.wallets:
            user_info = Info(self.api_url, skip_ws=False)
            self.clients.append(user_info)
            user_info.subscribe(
                {"type": "userFills", "user": wallet.master},
                self.callback(f"{wallet.slot}.userFills"),
            )
            user_info.subscribe(
                {"type": "orderUpdates", "user": wallet.master},
                self.callback(f"{wallet.slot}.orderUpdates"),
            )

        deadline = time.monotonic() + WS_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if all(client.ws_manager is not None and client.ws_manager.ws_ready for client in self.clients):
                time.sleep(0.75)
                return
            time.sleep(0.05)
        self.stop()
        raise RunnerError("WebSocket capture was not ready before the safety timeout.")

    def stop(self) -> None:
        for client in reversed(self.clients):
            try:
                client.disconnect_websocket()
            except Exception:
                pass
        self.clients.clear()

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with self.lock:
            return json.loads(json.dumps(self.events))


# ---------------------------------------------------------------------------
# 4) Interpret order responses and guarantee bounded cleanup
# ---------------------------------------------------------------------------
def new_cloid() -> tuple[str, Cloid]:
    raw = "0x" + secrets.token_hex(16)
    return raw, Cloid.from_str(raw)


def response_first_status(response: Any) -> Any:
    try:
        return response["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        return None


def response_is_resting(response: Any, expected_cloid: str) -> bool:
    status = response_first_status(response)
    if not isinstance(status, dict) or not isinstance(status.get("resting"), dict):
        return False
    returned = str(status["resting"].get("cloid", expected_cloid)).lower()
    return returned == expected_cloid.lower()


def response_is_filled(response: Any, expected_cloid: str) -> bool:
    status = response_first_status(response)
    if not isinstance(status, dict) or not isinstance(status.get("filled"), dict):
        return False
    returned = str(status["filled"].get("cloid", expected_cloid)).lower()
    return returned == expected_cloid.lower()


def response_oid(response: Any, expected_cloid: str) -> str:
    status = response_first_status(response)
    if not isinstance(status, dict):
        return ""
    detail = status.get("resting") if isinstance(status.get("resting"), dict) else status.get("filled")
    if not isinstance(detail, dict) or detail.get("oid") is None:
        return ""
    returned_cloid = detail.get("cloid")
    if returned_cloid is not None and str(returned_cloid).lower() != expected_cloid.lower():
        return ""
    return str(detail["oid"])


def order_status_value(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    order = response.get("order")
    if isinstance(order, dict):
        return str(order.get("status", "")).lower()
    return str(response.get("status", "")).lower()


def normalized_order_status(response: Any) -> str:
    return "".join(character for character in order_status_value(response) if character.isalpha())


def is_terminal_order_status(response: Any) -> bool:
    """Return whether Hyperliquid reports a final state for this order.

    The explicit set mirrors the documented orderStatus values.  The suffix
    fallback keeps cleanup fail-safe if Hyperliquid adds a more specific
    rejected/canceled reason without first updating this runner.
    """

    status = normalized_order_status(response)
    return status in TERMINAL_ORDER_STATUSES or status.endswith(
        ("rejected", "canceled", "cancelled")
    )


def query_status_remaining_size(response: Any) -> Decimal | None:
    if not isinstance(response, dict):
        return None
    wrapper = response.get("order")
    order = wrapper.get("order") if isinstance(wrapper, dict) else None
    if not isinstance(order, dict) or order.get("sz") is None:
        return None
    try:
        return Decimal(str(order["sz"]))
    except InvalidOperation:
        return None


def query_status_oid(response: Any, expected_cloid: str) -> str:
    if not isinstance(response, dict):
        return ""
    wrapper = response.get("order")
    order = wrapper.get("order") if isinstance(wrapper, dict) else None
    if not isinstance(order, dict) or order.get("oid") is None:
        return ""
    returned_cloid = order.get("cloid")
    if returned_cloid is not None and str(returned_cloid).lower() != expected_cloid.lower():
        return ""
    return str(order["oid"])


def websocket_oids_for_cloid(ws_events: dict[str, Any], bucket: str, cloid: str) -> set[str]:
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if str(value.get("cloid", "")).lower() == cloid.lower() and value.get("oid") is not None:
                found.add(str(value["oid"]))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for event in ws_events.get(bucket, []):
        visit(event)
    return found


def resolve_order_oid(
    cloid: str,
    exchange_response: Any,
    query_responses: list[Any],
    ws_events: dict[str, Any],
    ws_bucket: str,
) -> dict[str, Any]:
    sources: dict[str, list[str]] = {"exchangeResponse": [], "orderStatus": [], "orderUpdates": []}
    direct = response_oid(exchange_response, cloid)
    if direct:
        sources["exchangeResponse"].append(direct)
    for response in query_responses:
        candidate = query_status_oid(response, cloid)
        if candidate:
            sources["orderStatus"].append(candidate)
    sources["orderUpdates"] = sorted(websocket_oids_for_cloid(ws_events, ws_bucket, cloid))
    unique = {oid for values in sources.values() for oid in values}
    return {
        "cloid": cloid,
        "oid": next(iter(unique)) if len(unique) == 1 else None,
        "sources": sources,
        "consistent": len(unique) == 1,
    }


def wait_for_order_status(
    info: Any,
    master: str,
    cloid: Cloid,
    accepted: set[str],
    timeout_seconds: float = 5.0,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last: Any = None
    while time.monotonic() < deadline:
        last = info.query_order_by_cloid(master, cloid)
        if order_status_value(last) in accepted:
            return last
        time.sleep(0.25)
    return last


def cleanup_maker_order(
    info: Any,
    exchange: Any,
    master: str,
    market_name: str,
    cloid: Cloid,
    *,
    attempts: int = 3,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    report: dict[str, Any] = {"attempts": [], "terminalConfirmed": False, "finalStatus": None}
    for attempt_number in range(1, attempts + 1):
        status_response: Any = None
        try:
            status_response = info.query_order_by_cloid(master, cloid)
            status = normalized_order_status(status_response)
            report["attempts"].append(
                {"attempt": attempt_number, "statusBeforeCancel": status_response, "normalizedStatus": status}
            )
            if is_terminal_order_status(status_response):
                report["terminalConfirmed"] = True
                report["finalStatus"] = status_response
                return report
        except Exception as exc:
            report["attempts"].append(
                {"attempt": attempt_number, "statusError": f"{type(exc).__name__}; details suppressed."}
            )

        try:
            cancel_response = exchange.cancel_by_cloid(market_name, cloid)
            report["attempts"][-1]["cancelResponse"] = cancel_response
        except Exception as exc:
            report["attempts"][-1]["cancelError"] = f"{type(exc).__name__}; details suppressed."
        sleep_fn(min(0.5 * (2 ** (attempt_number - 1)), 2.0))

    try:
        final_status = info.query_order_by_cloid(master, cloid)
        report["finalStatus"] = final_status
        report["terminalConfirmed"] = is_terminal_order_status(final_status)
    except Exception as exc:
        report["finalStatusError"] = f"{type(exc).__name__}; details suppressed."
    return report


# ---------------------------------------------------------------------------
# 5) Validate the public buyer/seller pair and write redacted evidence
# ---------------------------------------------------------------------------
def flatten_public_trades(ws_events: dict[str, Any]) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for event in ws_events.get("market.trades", []):
        message = event.get("message", {}) if isinstance(event, dict) else {}
        data = message.get("data") if isinstance(message, dict) else None
        if isinstance(data, list):
            trades.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            trades.append(data)
    return trades


def fills_for_oid(snapshot: dict[str, Any], slot: str, oid: str) -> list[dict[str, Any]]:
    fills = snapshot.get(slot, {}).get("fills", [])
    return [
        fill
        for fill in fills
        if isinstance(fill, dict) and str(fill.get("oid", "")) == str(oid)
    ]


def validate_actual_pair(
    after: dict[str, Any],
    ws_events: dict[str, Any],
    w1: preflight.WalletConfig,
    w2: preflight.WalletConfig,
    maker_oid: str,
    taker_oid: str,
    plan: A1Plan,
    wire_coin: str,
) -> dict[str, Any]:
    reasons: list[str] = []
    maker_fills = fills_for_oid(after, "W1", maker_oid)
    taker_fills = fills_for_oid(after, "W2", taker_oid)
    if not maker_fills:
        reasons.append("No W1 fill was found for the maker oid resolved from its cloid.")
    if not taker_fills:
        reasons.append("No W2 fill was found for the taker oid resolved from its cloid.")

    def trade_key(item: dict[str, Any]) -> tuple[str, str, str] | None:
        if item.get("time") is None or item.get("coin") is None or item.get("tid") is None:
            return None
        return (str(item["time"]), str(item["coin"]), str(item["tid"]))

    maker_keys = {key for fill in maker_fills if (key := trade_key(fill)) is not None}
    taker_keys = {key for fill in taker_fills if (key := trade_key(fill)) is not None}
    if not maker_keys or maker_keys != taker_keys:
        reasons.append("W1 and W2 oid fills do not share exactly the same (time, coin, tid) keys.")

    target_price = plan.price
    for label, fills, expected_side in (("W1", maker_fills, "A"), ("W2", taker_fills, "B")):
        if any(str(fill.get("coin", "")) != wire_coin for fill in fills):
            reasons.append(f"{label} fill coin differs from the resolved HYPE/USDC market.")
        if any(str(fill.get("side", "")).upper() != expected_side for fill in fills):
            reasons.append(f"{label} fill side is not the expected side.")
        if any(Decimal(str(fill.get("px", "0"))) != target_price for fill in fills):
            reasons.append(f"{label} fill price differs from the planned exact price.")
        total_size = sum((Decimal(str(fill.get("sz", "0"))) for fill in fills), Decimal("0"))
        if total_size != plan.size:
            reasons.append(f"{label} filled size differs from the planned size.")

    public_trades = flatten_public_trades(ws_events)
    public_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for trade in public_trades:
        key = trade_key(trade)
        if key is not None:
            public_by_key.setdefault(key, []).append(trade)

    expected_users = [w2.master.lower(), w1.master.lower()]
    verified_public: list[dict[str, Any]] = []
    for key in sorted(maker_keys & taker_keys):
        maker_for_key = [fill for fill in maker_fills if trade_key(fill) == key]
        taker_for_key = [fill for fill in taker_fills if trade_key(fill) == key]
        maker_hashes = {str(fill.get("hash", "")).lower() for fill in maker_for_key if fill.get("hash")}
        taker_hashes = {str(fill.get("hash", "")).lower() for fill in taker_for_key if fill.get("hash")}
        maker_size = sum((Decimal(str(fill.get("sz", "0"))) for fill in maker_for_key), Decimal("0"))
        taker_size = sum((Decimal(str(fill.get("sz", "0"))) for fill in taker_for_key), Decimal("0"))
        if not maker_hashes or maker_hashes != taker_hashes:
            reasons.append(f"W1 and W2 fill hashes disagree for trade key {key}.")
        if maker_size != taker_size:
            reasons.append(f"W1 and W2 fill sizes disagree for trade key {key}.")
        candidates = public_by_key.get(key, [])
        matching = [
            trade
            for trade in candidates
            if [str(user).lower() for user in trade.get("users", [])] == expected_users
            and Decimal(str(trade.get("px", "0"))) == target_price
            and Decimal(str(trade.get("sz", "0"))) == maker_size
            and str(trade.get("coin", "")) == wire_coin
            and str(trade.get("hash", "")).lower() in maker_hashes
        ]
        if not matching:
            reasons.append(f"Public trade {key} does not exactly match W2 buyer, W1 seller, hash, price, and size.")
        else:
            verified_public.extend(matching)

    unique_public: dict[tuple[Any, ...], dict[str, Any]] = {}
    for trade in verified_public:
        identity = (
            trade.get("time"),
            trade.get("coin"),
            trade.get("tid"),
            str(trade.get("hash", "")).lower(),
            str(trade.get("px", "")),
            str(trade.get("sz", "")),
            tuple(str(user).lower() for user in trade.get("users", [])),
        )
        unique_public[identity] = trade
    verified_public = list(unique_public.values())
    public_total_size = sum(
        (Decimal(str(trade.get("sz", "0"))) for trade in verified_public),
        Decimal("0"),
    )
    if public_total_size != plan.size:
        reasons.append("Aggregate size of verified public trades differs from the planned size.")

    return {
        "classification": "VALID" if not reasons else "INVALID",
        "reasons": reasons,
        "expectedUsersBuyerSeller": expected_users,
        "makerFillCount": len(maker_fills),
        "takerFillCount": len(taker_fills),
        "verifiedPublicSize": decimal_string(public_total_size),
        "sharedTradeKeys": [
            {"time": key[0], "coin": key[1], "tid": key[2]}
            for key in sorted(maker_keys & taker_keys)
        ],
        "verifiedPublicTrades": verified_public,
    }


def write_artifact(
    evidence: dict[str, Any], output_dir: Path, wallets: tuple[preflight.WalletConfig, ...]
) -> Path:
    serialized = json.dumps(evidence, indent=2, ensure_ascii=False)
    preflight.ensure_no_secrets(serialized, wallets)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = output_dir / f"체결실험_{stamp}_{secrets.token_hex(4)}.json"
    with destination.open("x", encoding="utf-8") as handle:
        handle.write(serialized + "\n")
    return destination


# ---------------------------------------------------------------------------
# 6) Execute exactly one Testnet A1 run under the validated plan
# ---------------------------------------------------------------------------
def execute_a1(validated: preflight.ValidatedConfig, target: Decimal, output_dir: Path) -> tuple[Path, str]:
    # The SDK logs signed request payloads at DEBUG. Keep those out of the
    # console; the curated artifact stores only public responses and state.
    logging.disable(logging.DEBUG)
    if validated.api_url != preflight.EXPECTED_API_URL:
        raise RunnerError("Runner hard refusal: non-Testnet API URL.")
    wallets = validated.wallets
    by_slot = {wallet.slot: wallet for wallet in wallets}
    w1 = by_slot["W1"]
    w2 = by_slot["W2"]
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "experiment": "Experiment 2 A1 - spot orderbook value transfer pilot",
        "startedAt": now_iso(),
        "network": "testnet",
        "apiUrl": validated.api_url,
        "roles": {"W1": "maker/seller", "W2": "taker/buyer", "W3": "observer", "W4": "observer"},
        "countingConvention": "The paired maker/taker orders represent one economic transfer, so run notional is counted once.",
        "safetyCaps": validated.safety_caps,
        "capApplicability": {
            "orderNotional": "enforced",
            "runNotional": "enforced once for the paired economic transfer",
            "orderCount": "two order submissions; cleanup cancels are not orders",
            "slippageBps": "enforced before maker and again immediately before IOC",
            "openPositionUsd": "not applicable to this spot-only A1 pilot",
        },
        "classification": "ABORTED",
        "events": [],
    }
    phase = "initialize"
    maker_raw = ""
    taker_raw = ""
    maker_cloid: Cloid | None = None
    taker_cloid: Cloid | None = None
    maker_exchange: Exchange | None = None
    maker_may_rest = False
    capture: WsCapture | None = None
    submissions = SubmissionTracker()
    info: Info | None = None

    try:
        phase = "metadata"
        info = Info(validated.api_url, skip_ws=True)
        market = resolve_hype_usdc(info)
        evidence["market"] = {
            "displayName": market.display_name,
            "wireCoin": market.wire_coin,
            "derivedAssetId": market.asset_id,
            "baseToken": market.base_token,
            "quoteToken": market.quote_token,
            "szDecimals": market.sz_decimals,
        }

        phase = "pre-snapshot-and-role-verification"
        before = capture_all_accounts(info, wallets)
        before_book = public_book_snapshot(info, market)
        evidence["before"] = {"accounts": before, "book": before_book}

        phase = "websocket-start"
        capture = WsCapture(validated.api_url, market, wallets)
        capture.start()

        phase = "local-agent-construction"
        maker_wallet = Account.from_key(w1.private_key)
        taker_wallet = Account.from_key(w2.private_key)
        if maker_wallet.address.lower() != w1.agent or taker_wallet.address.lower() != w2.agent:
            raise RunnerError("Agent key/address invariant changed after preflight.")
        maker_exchange = Exchange(
            maker_wallet,
            validated.api_url,
            account_address=w1.master,
        )
        taker_exchange = Exchange(
            taker_wallet,
            validated.api_url,
            account_address=w2.master,
        )
        maker_raw, maker_cloid = new_cloid()
        taker_raw, taker_cloid = new_cloid()
        if maker_raw == taker_raw:
            raise RunnerError("Cloid uniqueness invariant failed.")
        evidence["cloids"] = {"maker": maker_raw, "taker": taker_raw}

        # Exchange construction fetches metadata, so it intentionally happens
        # before the final book read. This keeps the spread-to-order delay short.
        phase = "live-plan"
        live_book = public_book_snapshot(info, market)
        live_balances = {
            "W1": {"spotState": info.spot_user_state(w1.master)},
            "W2": {"spotState": info.spot_user_state(w2.master)},
        }
        order_cap = Decimal(str(validated.safety_caps["HL_MAX_ORDER_NOTIONAL_USD"]))
        run_cap = Decimal(str(validated.safety_caps["HL_MAX_RUN_NOTIONAL_USD"]))
        slippage_cap_bps = Decimal(str(validated.safety_caps["HL_MAX_SLIPPAGE_BPS"]))
        plan = build_plan(
            live_book["l2Book"], market, target, order_cap, run_cap, slippage_cap_bps
        )
        verify_balances(live_balances, market, plan)
        evidence["liveBookBeforeMaker"] = live_book
        evidence["liveBalancesBeforeMaker"] = live_balances
        evidence["plan"] = {
            "price": decimal_string(plan.price),
            "size": decimal_string(plan.size),
            "notionalUsd": decimal_string(plan.notional),
            "targetNotionalUsd": decimal_string(plan.target_notional),
            "bestBid": decimal_string(plan.best_bid),
            "bestAsk": decimal_string(plan.best_ask),
            "priceTick": decimal_string(plan.price_tick),
            "initialPriceDeviationBps": decimal_string(
                price_deviation_bps(plan.price, plan.best_bid, plan.best_ask)
            ),
            "slippageCapBps": decimal_string(slippage_cap_bps),
            "strictlyInsideSpread": plan.best_bid < plan.price < plan.best_ask,
            "makerOrderType": "Alo",
            "takerOrderType": "Ioc",
        }

        phase = "maker-alo"
        evidence["events"].append({"at": now_iso(), "phase": phase, "action": "submissionAttempt"})
        maker_response = attempt_signed_order(
            submissions,
            "maker",
            lambda: maker_exchange.order(
                market.display_name,
                False,
                float(plan.size),
                float(plan.price),
                {"limit": {"tif": "Alo"}},
                reduce_only=False,
                cloid=maker_cloid,
            ),
        )
        maker_may_rest = response_is_resting(maker_response, maker_raw)
        evidence["makerResponse"] = maker_response
        evidence["events"].append({"at": now_iso(), "phase": phase, "responseWasResting": maker_may_rest})
        if not maker_may_rest:
            raise RunnerError("W1 ALO response was not resting; W2 order was not sent.")

        phase = "maker-resting-confirmation"
        maker_status = wait_for_order_status(info, w1.master, maker_cloid, {"open"})
        evidence["makerStatusBeforeTaker"] = maker_status
        if order_status_value(maker_status) != "open":
            raise RunnerError("W1 order was not confirmed open before W2; W2 order was not sent.")
        remaining_size = query_status_remaining_size(maker_status)
        if remaining_size != plan.size:
            raise RunnerError("W1 order was partially filled or its remaining size is unverified; W2 was not sent.")

        phase = "fresh-pre-ioc-book"
        pre_ioc_book = public_book_snapshot(info, market)
        fresh_deviation_bps = enforce_pre_ioc_book(pre_ioc_book, plan, slippage_cap_bps)
        evidence["bookImmediatelyBeforeIoc"] = pre_ioc_book
        evidence["freshPreIocDeviationBps"] = decimal_string(fresh_deviation_bps)

        phase = "taker-ioc"
        evidence["events"].append({"at": now_iso(), "phase": phase, "action": "submissionAttempt"})
        taker_response = attempt_signed_order(
            submissions,
            "taker",
            lambda: taker_exchange.order(
                market.display_name,
                True,
                float(plan.size),
                float(plan.price),
                {"limit": {"tif": "Ioc"}},
                reduce_only=False,
                cloid=taker_cloid,
            ),
        )
        evidence["takerResponse"] = taker_response
        evidence["events"].append(
            {"at": now_iso(), "phase": phase, "responseWasFilled": response_is_filled(taker_response, taker_raw)}
        )
        time.sleep(POST_TRADE_CAPTURE_SECONDS)

    except (preflight.PreflightError, RunnerError) as exc:
        evidence["events"].append({"at": now_iso(), "phase": phase, "error": str(exc)})
        evidence["classification"] = "INVALID" if submissions.signed_order_attempts else "ABORTED"
    except Exception as exc:
        evidence["events"].append(
            {"at": now_iso(), "phase": phase, "error": f"Unexpected {type(exc).__name__}; details suppressed."}
        )
        evidence["classification"] = "INVALID" if submissions.signed_order_attempts else "ABORTED"
    finally:
        evidence["signedOrderRequestsAttempted"] = submissions.signed_order_attempts
        cleanup_confirmed = True
        if info is not None and maker_cloid is not None and maker_exchange is not None and submissions.maker_submission_attempted:
            try:
                evidence["makerCleanup"] = cleanup_maker_order(
                    info,
                    maker_exchange,
                    w1.master,
                    evidence["market"]["displayName"],
                    maker_cloid,
                )
            except Exception as exc:
                evidence["makerCleanup"] = {
                    "terminalConfirmed": False,
                    "error": f"Unexpected {type(exc).__name__}; details suppressed.",
                }
            cleanup_confirmed = bool(evidence["makerCleanup"]["terminalConfirmed"])
        evidence["cleanupTerminalConfirmed"] = cleanup_confirmed

        if info is not None and taker_cloid is not None:
            try:
                evidence["takerStatusAfter"] = info.query_order_by_cloid(w2.master, taker_cloid)
            except Exception as exc:
                evidence["events"].append(
                    {"at": now_iso(), "phase": "taker-status", "error": f"Status {type(exc).__name__}; details suppressed."}
                )

        if info is not None and "market" in evidence:
            try:
                evidence["after"] = {
                    "accounts": capture_all_accounts(info, wallets),
                    "book": public_book_snapshot(
                        info,
                        SpotMarket(
                            evidence["market"]["displayName"],
                            evidence["market"]["wireCoin"],
                            evidence["market"]["derivedAssetId"],
                            evidence["market"]["baseToken"],
                            evidence["market"]["quoteToken"],
                            evidence["market"]["szDecimals"],
                        ),
                    ),
                }
            except Exception as exc:
                evidence["events"].append(
                    {"at": now_iso(), "phase": "post-snapshot", "error": f"Snapshot {type(exc).__name__}; details suppressed."}
                )

        if capture is not None:
            evidence["websocket"] = capture.snapshot()
            capture.stop()

    if submissions.signed_order_attempts == 2 and "after" in evidence and "plan" in evidence and maker_raw and taker_raw:
        plan_for_validation = A1Plan(
            Decimal(evidence["plan"]["price"]),
            Decimal(evidence["plan"]["size"]),
            Decimal(evidence["plan"]["notionalUsd"]),
            Decimal(evidence["plan"]["bestBid"]),
            Decimal(evidence["plan"]["bestAsk"]),
            Decimal(evidence["plan"]["priceTick"]),
            Decimal(evidence["plan"]["targetNotionalUsd"]),
        )
        ws_events = evidence.get("websocket", {})
        maker_identity = resolve_order_oid(
            maker_raw,
            evidence.get("makerResponse"),
            [
                evidence.get("makerStatusBeforeTaker"),
                *[
                    attempt.get("statusBeforeCancel")
                    for attempt in evidence.get("makerCleanup", {}).get("attempts", [])
                    if isinstance(attempt, dict)
                ],
                evidence.get("makerCleanup", {}).get("finalStatus"),
            ],
            ws_events,
            "W1.orderUpdates",
        )
        taker_identity = resolve_order_oid(
            taker_raw,
            evidence.get("takerResponse"),
            [evidence.get("takerStatusAfter")],
            ws_events,
            "W2.orderUpdates",
        )
        evidence["orderIdentity"] = {"maker": maker_identity, "taker": taker_identity}
        if not maker_identity["consistent"] or not taker_identity["consistent"]:
            validation = {
                "classification": "INVALID",
                "reasons": ["Could not resolve each cloid to exactly one consistent oid."],
            }
        else:
            try:
                validation = validate_actual_pair(
                    evidence["after"]["accounts"],
                    ws_events,
                    w1,
                    w2,
                    str(maker_identity["oid"]),
                    str(taker_identity["oid"]),
                    plan_for_validation,
                    evidence["market"]["wireCoin"],
                )
            except Exception as exc:
                validation = {
                    "classification": "INVALID",
                    "reasons": [f"Public artifact validation raised {type(exc).__name__}; details suppressed."],
                }
        evidence["validation"] = validation
        evidence["classification"] = validation["classification"]
    elif submissions.signed_order_attempts:
        evidence["classification"] = "INVALID"

    if not evidence.get("cleanupTerminalConfirmed", True):
        evidence["classification"] = "CLEANUP_UNCONFIRMED"
        evidence.setdefault("validation", {}).setdefault("reasons", []).append(
            "W1 terminal order state could not be confirmed after bounded cancel retries."
        )

    evidence["finishedAt"] = now_iso()
    destination = write_artifact(evidence, output_dir, wallets)
    return destination, str(evidence["classification"])


# ---------------------------------------------------------------------------
# 7) CLI gate: explicit flag is mandatory before any signed action
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-testnet-a1", action="store_true")
    parser.add_argument("--env", type=Path, default=REPO_ROOT / ".env.local")
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "결과" / "테스트넷_A1"
    )
    args = parser.parse_args()

    # This check intentionally happens before .env.local is read.
    if not args.execute_testnet_a1:
        print("REFUSED: explicit --execute-testnet-a1 flag is required.", file=sys.stderr)
        raise SystemExit(2)

    try:
        _, validated, target = load_signed_config(args.env)
        destination, classification = execute_a1(validated, target, args.output_dir)
    except (preflight.PreflightError, RunnerError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        print("REFUSED: setup failed; sensitive details were suppressed.", file=sys.stderr)
        raise SystemExit(3) from None

    print(f"Experiment classification: {classification}")
    print(f"Redacted artifact: {destination.resolve()}")
    raise SystemExit(0 if classification == "VALID" else 4)


if __name__ == "__main__":
    main()

