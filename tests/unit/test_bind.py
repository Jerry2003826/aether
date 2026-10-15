import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aether_api.settings import bind_is_allowed


class BindTests(unittest.TestCase):
    def test_loopback_is_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(bind_is_allowed("127.0.0.1"))
            self.assertFalse(bind_is_allowed("0.0.0.0"))

    def test_public_bind_requires_tls_and_secure_cookie(self) -> None:
        with mock.patch.dict(os.environ, {"AETHER_ALLOW_NON_LOOPBACK": "1"}, clear=True):
            self.assertFalse(bind_is_allowed("0.0.0.0"))
        with tempfile.TemporaryDirectory() as directory:
            certificate = Path(directory) / "tls.crt"
            key = Path(directory) / "tls.key"
            certificate.write_text("certificate", encoding="utf-8")
            key.write_text("key", encoding="utf-8")
            secure = {
                "AETHER_ALLOW_NON_LOOPBACK": "1",
                "AETHER_COOKIE_SECURE": "1",
                "AETHER_TLS_CERTFILE": str(certificate),
                "AETHER_TLS_KEYFILE": str(key),
            }
            with mock.patch.dict(os.environ, secure, clear=True):
                self.assertTrue(bind_is_allowed("0.0.0.0"))
            missing_cookie = dict(secure)
            missing_cookie["AETHER_COOKIE_SECURE"] = "0"
            with mock.patch.dict(os.environ, missing_cookie, clear=True):
                self.assertFalse(bind_is_allowed("0.0.0.0"))
            missing_cert = dict(secure)
            del missing_cert["AETHER_TLS_CERTFILE"]
            with mock.patch.dict(os.environ, missing_cert, clear=True):
                self.assertFalse(bind_is_allowed("0.0.0.0"))


if __name__ == "__main__":
    unittest.main()
