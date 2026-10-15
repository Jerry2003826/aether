import json
import unittest
from pathlib import Path

from scripts.probe_f01 import openhands_probe_status


class F01ReportTests(unittest.TestCase):
    def test_import_without_model_call_is_partial(self) -> None:
        self.assertEqual(openhands_probe_status(imports_ok=True, model_called=False), "partial")
        self.assertEqual(openhands_probe_status(imports_ok=False, model_called=False), "blocked")
        self.assertEqual(openhands_probe_status(imports_ok=True, model_called=True), "passed")

    def test_bge_dimension_comes_from_a_cpu_encode(self) -> None:
        report = json.loads(Path("reports/f01-probe.json").read_text(encoding="utf-8"))
        ml = report["ml"]
        self.assertEqual(ml["status"], "passed")
        self.assertTrue(ml["weights_present"])
        self.assertEqual(ml["dimension_probed"], 1024)
        self.assertEqual(ml["device"], "cpu")
        self.assertRegex(ml["embedding_revision"], r"^[0-9a-f]{40}$")
        self.assertGreater(ml["sample_norm"], 0)
        self.assertEqual(report["openhands"]["live_model"], "not_run")
        self.assertEqual(report["openhands"]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
