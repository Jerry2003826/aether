import json
import unittest
from pathlib import Path


class F01ReportTests(unittest.TestCase):
    def test_missing_weights_are_not_a_pass(self) -> None:
        report = json.loads(Path("reports/f01-probe.json").read_text(encoding="utf-8"))
        ml = report["ml"]
        if not ml["weights_present"]:
            self.assertIsNone(ml["dimension_probed"])
            self.assertNotEqual(ml["status"], "passed")
        self.assertEqual(report["openhands"]["live_model"], "not_run")
        self.assertIn(report["openhands"]["status"], {"passed", "blocked"})


if __name__ == "__main__":
    unittest.main()
