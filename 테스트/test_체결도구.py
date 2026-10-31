from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import 체결도구 as a1
import 테스트넷_사전점검 as preflight


class FakeMetadataInfo:
    def __init__(self) -> None:
        self.name_to_coin = {"HYPE/USDC": "@dynamic"}
        self.coin_to_asset = {"@dynamic": 12345}
        self.asset_to_sz_decimals = {12345: 2}

    def spot_meta(self):
        return {
            "tokens": [
                {"index": 77, "name": "HYPE", "szDecimals": 2},
                {"index": 91, "name": "USDC", "szDecimals": 2},
            ],
            "universe": [{"index": 2345, "name": "@dynamic", "tokens": [77, 91]}],
        }


def market() -> a1.SpotMarket:
    return a1.SpotMarket("HYPE/USDC", "@dynamic", 12345, "HYPE", "USDC", 2)


def book(bid: str = "19.99", ask: str = "20.01"):
    return {
        "coin": "@dynamic",
        "levels": [
            [{"px": bid, "sz": "9", "n": 1}],
            [{"px": ask, "sz": "9", "n": 1}],
        ],
    }


def wallet(slot: str, master_byte: str, agent_byte: str) -> preflight.WalletConfig:
    return preflight.WalletConfig(
        slot=slot,
        master="0x" + master_byte * 20,
        agent="0x" + agent_byte * 20,
        private_key="11" * 32,
    )


class PlanningTests(unittest.TestCase):
    def test_resolves_hype_usdc_from_metadata_without_fixed_asset_id(self) -> None:
        resolved = a1.resolve_hype_usdc(FakeMetadataInfo())
        self.assertEqual(resolved.wire_coin, "@dynamic")
        self.assertEqual(resolved.asset_id, 12345)
        self.assertEqual(resolved.sz_decimals, 2)

    def test_builds_legal_inside_spread_plan_above_minimum(self) -> None:
        plan = a1.build_plan(
            book(), market(), Decimal("11"), Decimal("12"), Decimal("20"), Decimal("50")
        )
        self.assertGreater(plan.price, plan.best_bid)
        self.assertLess(plan.price, plan.best_ask)
        self.assertGreaterEqual(plan.notional, Decimal("10"))
        self.assertLessEqual(plan.notional, Decimal("12"))
        self.assertEqual(plan.size.as_tuple().exponent, -2)

    def test_refuses_spread_without_legal_inside_tick(self) -> None:
        with self.assertRaises(a1.RunnerError):
            a1.build_plan(
                book("20.000", "20.001"),
                market(),
                Decimal("11"),
                Decimal("12"),
                Decimal("20"),
                Decimal("50"),
            )

    def test_refuses_rounded_notional_over_cap(self) -> None:
        with self.assertRaises(a1.RunnerError):
            a1.build_plan(
                book(), market(), Decimal("11"), Decimal("10.50"), Decimal("20"), Decimal("50")
            )

    def test_balance_gate_requires_hype_and_usdc(self) -> None:
        plan = a1.build_plan(
            book(), market(), Decimal("11"), Decimal("12"), Decimal("20"), Decimal("50")
        )
        states = {
            "W1": {"spotState": {"balances": [{"coin": "HYPE", "total": "1", "hold": "0"}]}},
            "W2": {"spotState": {"balances": [{"coin": "USDC", "total": "100", "hold": "0"}]}},
        }
        a1.verify_balances(states, market(), plan)
        states["W1"]["spotState"]["balances"][0]["total"] = "0"
        with self.assertRaises(a1.RunnerError):
            a1.verify_balances(states, market(), plan)

    def test_fresh_pre_ioc_book_enforces_best_ask_and_slippage(self) -> None:
        plan = a1.build_plan(
            book(), market(), Decimal("11"), Decimal("12"), Decimal("20"), Decimal("50")
        )
        fresh = {"l2Book": book("19.99", a1.decimal_string(plan.price))}
        self.assertLessEqual(a1.enforce_pre_ioc_book(fresh, plan, Decimal("50")), Decimal("50"))
        stale = {"l2Book": book("19.98", "19.99")}
        with self.assertRaises(a1.RunnerError):
            a1.enforce_pre_ioc_book(stale, plan, Decimal("50"))


class PublicTradeValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w1 = wallet("W1", "11", "a1")
        self.w2 = wallet("W2", "22", "a2")
        self.plan = a1.A1Plan(
            price=Decimal("20"),
            size=Decimal("0.55"),
            notional=Decimal("11"),
            best_bid=Decimal("19.99"),
            best_ask=Decimal("20.01"),
            price_tick=Decimal("0.001"),
            target_notional=Decimal("11"),
        )
        self.maker_cloid = "0x" + "01" * 16
        self.taker_cloid = "0x" + "02" * 16
        self.maker_oid = "7001"
        self.taker_oid = "7002"
        self.after = {
            "W1": {
                "fills": [
                    {
                        "oid": 7001,
                        "tid": 77,
                        "time": 123456789,
                        "coin": "@dynamic",
                        "hash": "0xabc",
                        "side": "A",
                        "px": "20",
                        "sz": "0.55",
                    }
                ]
            },
            "W2": {
                "fills": [
                    {
                        "oid": 7002,
                        "tid": 77,
                        "time": 123456789,
                        "coin": "@dynamic",
                        "hash": "0xabc",
                        "side": "B",
                        "px": "20",
                        "sz": "0.55",
                    }
                ]
            },
        }

    def ws(self, users: list[str]):
        return {
            "market.trades": [
                {
                    "receivedAt": "test",
                    "message": {
                        "channel": "trades",
                        "data": [
                            {
                                "tid": 77,
                                "time": 123456789,
                                "coin": "@dynamic",
                                "hash": "0xabc",
                                "px": "20",
                                "sz": "0.55",
                                "users": users,
                            }
                        ],
                    },
                }
            ]
        }

    def test_valid_only_for_exact_w2_buyer_w1_seller(self) -> None:
        result = a1.validate_actual_pair(
            self.after,
            self.ws([self.w2.master, self.w1.master]),
            self.w1,
            self.w2,
            self.maker_oid,
            self.taker_oid,
            self.plan,
            "@dynamic",
        )
        self.assertEqual(result["classification"], "VALID")

    def test_third_party_or_reversed_users_is_invalid(self) -> None:
        result = a1.validate_actual_pair(
            self.after,
            self.ws([self.w1.master, self.w2.master]),
            self.w1,
            self.w2,
            self.maker_oid,
            self.taker_oid,
            self.plan,
            "@dynamic",
        )
        self.assertEqual(result["classification"], "INVALID")
        self.assertTrue(any("W2 buyer" in reason for reason in result["reasons"]))

    def test_public_trade_size_mismatch_is_invalid(self) -> None:
        ws = self.ws([self.w2.master, self.w1.master])
        ws["market.trades"][0]["message"]["data"][0]["sz"] = "0.54"
        result = a1.validate_actual_pair(
            self.after,
            ws,
            self.w1,
            self.w2,
            self.maker_oid,
            self.taker_oid,
            self.plan,
            "@dynamic",
        )
        self.assertEqual(result["classification"], "INVALID")
        self.assertTrue(any("Aggregate size" in reason for reason in result["reasons"]))

    def test_resolves_cloid_to_oid_without_cloid_in_user_fills(self) -> None:
        exchange_response = {
            "response": {
                "data": {
                    "statuses": [
                        {"resting": {"oid": 7001, "cloid": self.maker_cloid}}
                    ]
                }
            }
        }
        query_response = {
            "status": "order",
            "order": {
                "status": "filled",
                "order": {"oid": 7001, "cloid": self.maker_cloid},
            },
        }
        ws_events = {
            "W1.orderUpdates": [
                {
                    "message": {
                        "data": [
                            {"order": {"oid": 7001, "cloid": self.maker_cloid}}
                        ]
                    }
                }
            ]
        }
        resolved = a1.resolve_order_oid(
            self.maker_cloid,
            exchange_response,
            [query_response],
            ws_events,
            "W1.orderUpdates",
        )
        self.assertTrue(resolved["consistent"])
        self.assertEqual(resolved["oid"], "7001")
        # Official userFills may omit cloid; OID mapping still finds the fill.
        self.assertEqual(
            a1.fills_for_oid(self.after, "W1", resolved["oid"])[0]["tid"],
            77,
        )


class SequenceInfo:
    def __init__(self, responses):
        self.responses = list(responses)

    def query_order_by_cloid(self, master, cloid):
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class FakeExchange:
    def __init__(self):
        self.cancel_count = 0

    def cancel_by_cloid(self, market_name, cloid):
        self.cancel_count += 1
        return {"status": "ok", "cancel": self.cancel_count}


class CleanupAndArtifactTests(unittest.TestCase):
    def test_official_rejection_and_cancel_reasons_are_terminal(self) -> None:
        terminal_statuses = (
            "badAloPxRejected",
            "tickRejected",
            "insufficientSpotBalanceRejected",
            "selfTradeCanceled",
            "openInterestCapCanceled",
            "scheduledCancel",
        )
        for status in terminal_statuses:
            with self.subTest(status=status):
                response = {
                    "status": "order",
                    "order": {"status": status, "order": {}},
                }
                self.assertTrue(a1.is_terminal_order_status(response))
                exchange = FakeExchange()
                report = a1.cleanup_maker_order(
                    SequenceInfo([response]),
                    exchange,
                    "0x" + "11" * 20,
                    "HYPE/USDC",
                    a1.Cloid.from_str("0x" + "01" * 16),
                    sleep_fn=lambda _: None,
                )
                self.assertTrue(report["terminalConfirmed"])
                self.assertEqual(exchange.cancel_count, 0)

        self.assertFalse(
            a1.is_terminal_order_status(
                {"status": "order", "order": {"status": "open", "order": {}}}
            )
        )
        self.assertFalse(a1.is_terminal_order_status({"status": "unknownOid"}))

    def test_lost_maker_response_still_marks_attempt_and_runs_cleanup(self) -> None:
        tracker = a1.SubmissionTracker()

        def lost_response():
            raise TimeoutError("simulated response loss after possible acceptance")

        with self.assertRaises(TimeoutError):
            a1.attempt_signed_order(tracker, "maker", lost_response)
        self.assertTrue(tracker.maker_submission_attempted)
        self.assertEqual(tracker.signed_order_attempts, 1)

        info = SequenceInfo(
            [
                {"status": "order", "order": {"status": "open", "order": {}}},
                {"status": "order", "order": {"status": "canceled", "order": {}}},
            ]
        )
        exchange = FakeExchange()
        report = a1.cleanup_maker_order(
            info,
            exchange,
            "0x" + "11" * 20,
            "HYPE/USDC",
            a1.Cloid.from_str("0x" + "01" * 16),
            sleep_fn=lambda _: None,
        )
        self.assertTrue(report["terminalConfirmed"])
        self.assertEqual(exchange.cancel_count, 1)

    def test_cleanup_retries_until_terminal_state_is_confirmed(self) -> None:
        info = SequenceInfo(
            [
                {"status": "unknownOid"},
                {"status": "order", "order": {"status": "canceled", "order": {}}},
            ]
        )
        exchange = FakeExchange()
        report = a1.cleanup_maker_order(
            info,
            exchange,
            "0x" + "11" * 20,
            "HYPE/USDC",
            a1.Cloid.from_str("0x" + "01" * 16),
            sleep_fn=lambda _: None,
        )
        self.assertTrue(report["terminalConfirmed"])
        self.assertEqual(exchange.cancel_count, 1)

    def test_cleanup_unconfirmed_after_bounded_retries(self) -> None:
        info = SequenceInfo([{"status": "unknownOid"}])
        exchange = FakeExchange()
        report = a1.cleanup_maker_order(
            info,
            exchange,
            "0x" + "11" * 20,
            "HYPE/USDC",
            a1.Cloid.from_str("0x" + "01" * 16),
            sleep_fn=lambda _: None,
        )
        self.assertFalse(report["terminalConfirmed"])
        self.assertEqual(exchange.cancel_count, 3)

    def test_artifacts_use_unique_exclusive_names(self) -> None:
        wallets = (wallet("W1", "11", "a1"),)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            first = a1.write_artifact({"classification": "TEST"}, Path(temporary), wallets)
            second = a1.write_artifact({"classification": "TEST"}, Path(temporary), wallets)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_file() and second.is_file())


class CommandGateTests(unittest.TestCase):
    def test_missing_explicit_flag_refuses_before_env_read(self) -> None:
        with patch.object(sys, "argv", ["체결도구.py"]), patch.object(
            preflight, "read_config", side_effect=AssertionError("env must not be read")
        ), self.assertRaises(SystemExit) as caught:
            a1.main()
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

