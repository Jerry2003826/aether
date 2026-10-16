import unittest
from pathlib import Path


class ApiUiDocTests(unittest.TestCase):
    def test_coding_jobs_reach_a_sandbox_terminal_state(self) -> None:
        text = Path("docs/08_api_ui.md").read_text(encoding="utf-8")
        line = next(row for row in text.splitlines() if "coding-jobs" in row)
        self.assertIn("202", line)
        self.assertIn("FINISHED", line)
        self.assertIn("FAILED", line)
        self.assertNotIn("unsupported", line)

    def test_skill_evaluate_cannot_be_scored_by_the_actor(self) -> None:
        text = Path("docs/08_api_ui.md").read_text(encoding="utf-8")
        line = next(row for row in text.splitlines() if "/skills/{skill_id}/evaluate" in row)
        self.assertIn("不能指定最终分数", line)
        self.assertNotIn("unsupported", line)


if __name__ == "__main__":
    unittest.main()
