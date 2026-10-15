import unittest
from pathlib import Path

from aether_api.auth_service import confine_bootstrap_path
from aether_api.errors import ApiError
from scripts.doctor import confine_report_path


class PathConfinementTests(unittest.TestCase):
    def test_bootstrap_file_must_stay_under_secrets(self) -> None:
        allowed = confine_bootstrap_path(Path("secrets/bootstrap.json"))
        self.assertEqual(allowed.name, "bootstrap.json")
        self.assertEqual(allowed.parent.name, "secrets")
        with self.assertRaises(ApiError) as raised:
            confine_bootstrap_path(Path("/tmp/bootstrap.json"))
        self.assertEqual(raised.exception.code, "bootstrap_path_rejected")
        custom = confine_bootstrap_path(Path("secrets") / "custom-bootstrap.json")
        self.assertEqual(custom.name, "custom-bootstrap.json")
        self.assertEqual(custom.parent, Path("secrets").resolve())
        with self.assertRaises(ApiError):
            confine_bootstrap_path(Path("secrets") / ".." / "pyproject.toml")

    def test_doctor_report_must_stay_under_reports(self) -> None:
        allowed = confine_report_path(Path("reports") / "doctor-unit.json")
        self.assertEqual(allowed.parent.name, "reports")
        with self.assertRaises(SystemExit):
            confine_report_path(Path("/tmp/doctor.json"))


if __name__ == "__main__":
    unittest.main()
