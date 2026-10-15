import unittest
from datetime import UTC, datetime, timedelta

from aether_api.throttle import LoginThrottle, client_address


class ThrottleTests(unittest.TestCase):
    def test_window_expires(self) -> None:
        throttle = LoginThrottle(limit=2, window=timedelta(seconds=60))
        start = datetime(2026, 10, 15, tzinfo=UTC)
        self.assertFalse(throttle.blocked("203.0.113.4", start))
        throttle.record_failure("203.0.113.4", start)
        throttle.record_failure("203.0.113.4", start)
        self.assertTrue(throttle.blocked("203.0.113.4", start + timedelta(seconds=10)))
        self.assertFalse(throttle.blocked("203.0.113.4", start + timedelta(seconds=61)))

    def test_forwarded_for_is_opt_in(self) -> None:
        self.assertEqual(client_address("127.0.0.1", "203.0.113.8, 10.0.0.1", trust_forwarded_for=False), "127.0.0.1")
        self.assertEqual(client_address("127.0.0.1", "203.0.113.8, 10.0.0.1", trust_forwarded_for=True), "203.0.113.8")


if __name__ == "__main__":
    unittest.main()
