import json
import unittest
from pathlib import Path

from aether_domain.locks import lock_state


class LockStateTests(unittest.TestCase):
    def test_null_is_not_locked(self) -> None:
        self.assertEqual(lock_state(None), "unlocked")
        self.assertEqual(lock_state("  "), "unlocked")
        self.assertEqual(lock_state("1.9.1"), "locked")

    def test_example_upstream_lock_is_not_an_actual_lock(self) -> None:
        payload = json.loads(Path("config/upstream.lock.example.json").read_text(encoding="utf-8"))
        self.assertFalse(payload["is_actual_lock"])
        self.assertTrue(all(entry["version"] is None for entry in payload["entries"]))


if __name__ == "__main__":
    unittest.main()
