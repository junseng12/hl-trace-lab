import importlib.util
import unittest
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "02_오더북_가치이전" / "node_archive_join.py"
SPEC = importlib.util.spec_from_file_location("node_archive_join", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class NodeArchiveTests(unittest.TestCase):
    def test_current_block_envelope(self):
        a = "0x" + "11" * 20
        b = "0x" + "22" * 20
        envelope = {
            "block_number": 7,
            "block_time": "2026-01-01T00:00:00",
            "events": [
                [a, {"coin": "BTC", "time": 10, "tid": 99, "side": "B"}],
                [b, {"coin": "BTC", "time": 10, "tid": 99, "side": "A"}],
            ],
        }
        rows = list(MODULE.current_events(envelope))
        self.assertEqual([row["user"] for row in rows], [a, b])
        self.assertEqual(MODULE.fill_key(rows[0]["fill"]), ("BTC", "10", "99"))

    def test_legacy_side_info(self):
        record = {
            "coin": "COMP", "side_info": [
                {"user": "0x" + "11" * 20},
                {"user": "0x" + "22" * 20},
            ],
        }
        self.assertEqual(len(MODULE.legacy_trade(record)["users"]), 2)


if __name__ == "__main__":
    unittest.main()
