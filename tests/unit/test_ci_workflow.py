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


if __name__ == "__main__":
    unittest.main()
