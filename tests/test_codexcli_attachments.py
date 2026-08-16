import os
import tempfile
import unittest

from shared.ai_engine import _build_codexcli_cmd, _build_codexcli_prompt


class CodexCliAttachmentTests(unittest.TestCase):
    def test_image_cli_argument_is_absolute_but_prompt_path_is_relative(self):
        with tempfile.TemporaryDirectory() as workspace:
            media_dir = os.path.join(workspace, "cs_media")
            os.makedirs(media_dir)
            image_path = os.path.join(media_dir, "customer.png")
            with open(image_path, "wb") as stream:
                stream.write(b"test-image")

            config = {
                "workspace_dir": workspace,
                "codexcli_model": "test-model",
            }
            cmd = _build_codexcli_cmd(
                "codex",
                "prompt",
                config,
                os.path.join(workspace, "reply.txt"),
                image_path=image_path,
            )
            cli_image_path = cmd[cmd.index("--image") + 1]
            self.assertTrue(os.path.isabs(cli_image_path))
            self.assertEqual(os.path.normcase(cli_image_path),
                             os.path.normcase(os.path.abspath(image_path)))

            prompt = _build_codexcli_prompt(
                [{"role": "user", "content": "识别图片"}],
                "",
                image_path=image_path,
                workspace_dir=workspace,
            )
            relative_path = os.path.join("cs_media", "customer.png")
            self.assertIn(f"附件图片路径：{relative_path}", prompt)
            self.assertNotIn(os.path.abspath(workspace), prompt)


if __name__ == "__main__":
    unittest.main()
