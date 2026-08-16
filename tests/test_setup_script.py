import unittest
from pathlib import Path


class SetupScriptTests(unittest.TestCase):
    def test_dependency_install_exit_codes_are_captured_and_checked_separately(self):
        script = (Path(__file__).parents[1] / "setup.ps1").read_text(encoding="utf-8")

        base_install = "& $pyCmd -m pip install wxautoz requests ddgs --quiet 2>&1"
        base_capture = "$baseDepsExitCode = $LASTEXITCODE"
        requirements_install = (
            '& $pyCmd -m pip install -r "$CODE_DIR\\requirements-customer-service.txt" '
            "--quiet 2>&1"
        )
        requirements_capture = "$customerDepsExitCode = $LASTEXITCODE"
        combined_check = (
            "if ($baseDepsExitCode -eq 0 -and $customerDepsExitCode -eq 0)"
        )

        positions = [
            script.find(base_install),
            script.find(base_capture),
            script.find(requirements_install),
            script.find(requirements_capture),
            script.find(combined_check),
        ]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
