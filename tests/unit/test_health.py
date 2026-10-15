import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from aether_api.health import create_app


class HealthTests(unittest.TestCase):
    def test_live_does_not_claim_dependencies(self) -> None:
        client = TestClient(create_app())
        response = client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["dependencies"], {})

    def test_ready_is_unavailable_without_services(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            client = TestClient(create_app())
            response = client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "unavailable")
        self.assertFalse(body["dependencies"]["postgres"]["probed"])
        self.assertFalse(body["dependencies"]["temporal"]["probed"])
        self.assertIsNone(body["dependencies"]["postgres"]["version"])


if __name__ == "__main__":
    unittest.main()
