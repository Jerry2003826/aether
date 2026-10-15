import unittest
from pathlib import Path

import yaml


class CiWorkflowTests(unittest.TestCase):
    def test_service_containers_and_nonzero_failure(self) -> None:
        workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
        integration = workflow["jobs"]["integration"]
        services = integration["services"]
        self.assertIn("postgres", services)
        self.assertIn("temporal", services)
        self.assertIn("pgvector/pgvector", services["postgres"]["image"])
        self.assertIn("start-dev", services["temporal"]["command"])
        text = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("continue-on-error", text)
        self.assertNotIn("|| true", text)
        self.assertNotIn("uses: actions/checkout@v", text)
        self.assertNotIn("uses: actions/setup-node@v", text)
        self.assertNotIn("uses: astral-sh/setup-uv@v", text)
        self.assertIn("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", text)
        self.assertIn("actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020", text)
        self.assertIn("astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86", text)
        self.assertIn("uv sync --locked --no-build", text)
        self.assertNotIn("uv sync --frozen", text)
        self.assertNotIn("uv run python", text)
        self.assertNotIn("uv run pytest", text)


if __name__ == "__main__":
    unittest.main()
