import os
import unittest
from unittest import mock

from aether_api.settings import bind_is_allowed


class BindTests(unittest.TestCase):
    def test_loopback_is_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(bind_is_allowed("127.0.0.1"))
            self.assertFalse(bind_is_allowed("0.0.0.0"))

    def test_public_bind_requires_explicit_flag(self) -> None:
        with mock.patch.dict(os.environ, {"AETHER_ALLOW_NON_LOOPBACK": "1"}, clear=True):
            self.assertTrue(bind_is_allowed("0.0.0.0"))


if __name__ == "__main__":
    unittest.main()
