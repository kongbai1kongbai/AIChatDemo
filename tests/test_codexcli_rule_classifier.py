import os
import tempfile
import unittest
from unittest import mock

from shared.ai_engine import generate_rule_classification_codexcli


class CodexCliRuleClassifierTests(unittest.TestCase):
    def test_runs_in_read_only_mode_with_never_approval_and_rule_timeout(self):
        with tempfile.TemporaryDirectory() as workspace:
            config = {
                "workspace_dir": workspace,
                "codexcli_path": "codex-test",
                "codexcli_model": "test-model",
                "codexcli_rule_timeout": 17,
                "codexcli_dangerously_bypass": True,
            }
            captured = {}

            def fake_run(cmd, clean_env, timeout, stdin_text, stream_output):
                captured.update(
                    cmd=cmd,
                    clean_env=clean_env,
                    timeout=timeout,
                    stdin_text=stdin_text,
                    stream_output=stream_output,
                )
                output_file = cmd[cmd.index("--output-last-message") + 1]
                with open(output_file, "w", encoding="utf-8") as stream:
                    stream.write('{"decision":"no_match","rule_id":null,'
                                 '"confidence":"high","reason":"changed"}')
                return "ignored stdout", 0

            with mock.patch(
                "shared.ai_engine._run_codexcli_streaming",
                side_effect=fake_run,
            ):
                result = generate_rule_classification_codexcli(
                    "classifier prompt", config
                )

            self.assertIn('"decision":"no_match"', result)
            self.assertEqual(captured["timeout"], 17)
            self.assertEqual(captured["stdin_text"], "classifier prompt")
            self.assertFalse(captured["stream_output"])
            self.assertEqual(captured["cmd"][:4], [
                "codex-test", "-a", "never", "exec",
            ])
            self.assertIn("--sandbox", captured["cmd"])
            self.assertEqual(
                captured["cmd"][captured["cmd"].index("--sandbox") + 1],
                "read-only",
            )
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox",
                             captured["cmd"])
            self.assertNotIn("--image", captured["cmd"])
            output_file = captured["cmd"][
                captured["cmd"].index("--output-last-message") + 1
            ]
            self.assertFalse(os.path.exists(output_file))

    def test_returns_none_when_codex_cli_fails(self):
        with tempfile.TemporaryDirectory() as workspace:
            config = {
                "workspace_dir": workspace,
                "codexcli_path": "codex-test",
            }
            with mock.patch(
                "shared.ai_engine._run_codexcli_streaming",
                return_value=("failure", 1),
            ):
                result = generate_rule_classification_codexcli("prompt", config)

            self.assertIsNone(result)
            self.assertEqual(
                [name for name in os.listdir(workspace)
                 if name.startswith(".codexcli_rule_")],
                [],
            )


if __name__ == "__main__":
    unittest.main()
