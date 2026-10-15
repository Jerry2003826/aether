import json
import unittest
from pathlib import Path

from scripts.probe_f01 import openhands_probe_status


class F01ReportTests(unittest.TestCase):
    def test_import_without_model_call_is_partial(self) -> None:
        self.assertEqual(openhands_probe_status(imports_ok=True, model_called=False), "partial")
        self.assertEqual(openhands_probe_status(imports_ok=False, model_called=False), "blocked")
        self.assertEqual(openhands_probe_status(imports_ok=True, model_called=True), "passed")

    def test_missing_weights_are_not_a_pass(self) -> None:
        report = json.loads(Path("reports/f01-probe.json").read_text(encoding="utf-8"))
        ml = report["ml"]
        if not ml["weights_present"]:
            self.assertIsNone(ml.get("dimension_probed"))
            self.assertNotEqual(ml["status"], "passed")
        self.assertEqual(report["openhands"]["live_model"], "not_run")
        self.assertNotEqual(report["openhands"]["status"], "passed")
        self.assertEqual(report["openhands"]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
