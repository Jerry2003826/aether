import unittest
from pathlib import Path


class ApiUiDocTests(unittest.TestCase):
    def test_coding_jobs_are_explicitly_unsupported(self) -> None:
        text = Path("docs/08_api_ui.md").read_text(encoding="utf-8")
        line = next(row for row in text.splitlines() if "coding-jobs" in row)
        self.assertIn("unsupported", line)
        self.assertNotIn("QUEUED", line)


if __name__ == "__main__":
    unittest.main()
