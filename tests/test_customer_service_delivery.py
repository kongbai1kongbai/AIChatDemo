import inspect
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from customer_service.handler import CSHandler
from customer_service.excel_payment_matcher import PaymentMatchResult
from customer_service.rule_router import RuleDecision


MATCH_DECISION = RuleDecision(
    decision="match",
    rule_id="payment_match_v1",
    confidence="high",
    reason="exact match",
)
NO_MATCH_DECISION = RuleDecision(
    decision="no_match",
    rule_id=None,
    confidence="high",
    reason="not an exact match",
)


class _Response:
    def json(self):
        return {"errcode": 0, "errmsg": "ok"}


class CustomerServiceDeliveryTests(unittest.TestCase):
    def _handler(self, workspace=None):
        handler = CSHandler.__new__(CSHandler)
        handler.client = SimpleNamespace(access_token="test-token")
        handler.model_config = {"workspace_dir": workspace or os.getcwd()}
        handler.quota = {}
        handler.QUOTA_LIMIT = 5
        handler.QUOTA_WINDOW = 48 * 3600
        handler.outbound_messages = {}
        handler.outbound_lock = threading.Lock()
        handler.history = defaultdict(list)
        handler.total_replies = 0
        handler.forward_userid = ""
        handler.forward_chatid = ""
        handler.agent_id = ""
        return handler

    def test_pending_payment_workbook_uses_local_matcher_without_ai_reply(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "待处理.xlsx"
            source.write_bytes(b"source")
            handler = self._handler(workspace)
            handler.pending_files = {
                "kf:user": {
                    "file_path": str(source),
                    "file_bytes": b"source",
                    "filename": source.name,
                    "timestamp": 0,
                }
            }
            events = []

            def record_start_time():
                events.append("started")
                return 100.0

            def complete_match(source_path, output_dir):
                events.append("processed")
                return PaymentMatchResult(
                    output_path=Path(output_dir) / "待处理_匹配结果_test.xlsx",
                    candidate_groups=3,
                    kept_groups=2,
                    removed_groups=1,
                    output_rows=5,
                    timings={"total_seconds": 0.25},
                )

            process = Mock(side_effect=complete_match)
            with patch(
                    "customer_service.handler.classify_file_rule",
                    return_value=MATCH_DECISION,
            ) as classify, patch(
                    "customer_service.handler.load_rule_handler",
                    return_value=process,
            ) as load_handler, \
                    patch("customer_service.handler.time.time", side_effect=record_start_time), \
                    patch("customer_service.handler.generate_reply") as generate, \
                    patch.object(handler, "_finish_reply") as finish:
                handler._process_pending_file(
                    "kf", "user", "kf:user", "请按医保正收款和非医保负收款完成匹配"
                )

            classify.assert_called_once_with(
                source.name,
                "请按医保正收款和非医保负收款完成匹配",
                handler.model_config,
            )
            load_handler.assert_called_once_with("payment_match_v1")
            process.assert_called_once()
            request_output_dir = Path(process.call_args.args[1])
            self.assertEqual(request_output_dir.parent, Path(workspace) / "outputs")
            self.assertTrue(request_output_dir.name.startswith("request_"))
            generate.assert_not_called()
            finish.assert_called_once()
            self.assertEqual(events, ["started", "processed"])
            self.assertIn("耗时", finish.call_args.args[4])
            self.assertIn("待处理_匹配结果_test.xlsx`", finish.call_args.args[4])
            self.assertEqual(finish.call_args.kwargs["generation_started_at"], 100.0)
            self.assertEqual(
                finish.call_args.kwargs["request_output_dirs"],
                [request_output_dir],
            )

    def test_chinese_relative_document_path_is_resolved_while_url_is_ignored(self):
        with tempfile.TemporaryDirectory() as workspace:
            output_dir = Path(workspace) / "outputs"
            output_dir.mkdir()
            output_path = output_dir / "东园主单_匹配结果.xlsx"
            output_path.write_bytes(b"output")
            handler = self._handler(workspace)

            reply = (
                "远程链接 https://example.test/outputs/东园主单_匹配结果.xlsx 不应作为附件，"
                "本地结果是 outputs/东园主单_匹配结果.xlsx。"
            )

            self.assertEqual(handler._extract_file_paths(reply), [str(output_path)])

    def test_ai_file_fallback_excludes_source_from_recent_document_scan(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "待处理.xlsx"
            source.write_bytes(b"source")
            handler = self._handler(workspace)
            handler.pending_files = {
                "kf:user": {
                    "file_path": str(source),
                    "file_bytes": b"source",
                    "filename": source.name,
                    "timestamp": 0,
                }
            }

            with patch(
                    "customer_service.handler.classify_file_rule",
                    return_value=NO_MATCH_DECISION,
            ), patch(
                    "customer_service.handler.load_rule_handler",
            ) as load_handler, \
                    patch("customer_service.handler.load_config", return_value={"dm_system_prompt": ""}), \
                    patch("customer_service.handler.generate_reply", return_value="处理完成。"), \
                    patch.object(handler, "_finish_reply") as finish:
                handler._process_pending_file("kf", "user", "kf:user", "请整理数据")

            load_handler.assert_not_called()
            self.assertEqual(finish.call_args.kwargs["exclude_paths"], [str(source)])
            request_output_dir = finish.call_args.kwargs["request_output_dirs"][0]
            self.assertEqual(request_output_dir.parent, Path(workspace) / "outputs")

    def test_missing_local_matcher_dependency_falls_back_to_ai_workflow(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "待处理.xlsx"
            source.write_bytes(b"source")
            handler = self._handler(workspace)
            handler.pending_files = {
                "kf:user": {
                    "file_path": str(source),
                    "file_bytes": b"source",
                    "filename": source.name,
                    "timestamp": 0,
                }
            }

            with patch(
                "customer_service.handler.classify_file_rule",
                return_value=MATCH_DECISION,
            ), patch(
                "customer_service.handler.load_rule_handler",
                side_effect=ModuleNotFoundError("No module named 'openpyxl'"),
            ), patch(
                "customer_service.handler.load_config",
                return_value={"dm_system_prompt": ""},
            ), patch(
                "customer_service.handler.generate_reply",
                return_value="处理完成。",
            ) as generate, patch.object(handler, "_finish_reply") as finish:
                handler._process_pending_file(
                    "kf", "user", "kf:user", "请按医保正收款和非医保负收款完成匹配"
                )

            generate.assert_called_once()
            self.assertEqual(generate.call_args.kwargs["image_path"], str(source))
            self.assertIn("request_", generate.call_args.args[0][-1]["content"])
            self.assertEqual(len(finish.call_args.kwargs["request_output_dirs"]), 1)

    def test_rule_classifier_exception_falls_back_without_polluting_history(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "说明.xlsx"
            source.write_bytes(b"source")
            handler = self._handler(workspace)
            handler.pending_files = {
                "kf:user": {
                    "file_path": str(source),
                    "file_bytes": b"source",
                    "filename": source.name,
                    "timestamp": 0,
                }
            }

            with patch(
                "customer_service.handler.classify_file_rule",
                side_effect=RuntimeError("classifier unavailable"),
            ), patch(
                "customer_service.handler.load_rule_handler",
            ) as load_handler, patch(
                "customer_service.handler.load_config",
                return_value={"dm_system_prompt": ""},
            ), patch(
                "customer_service.handler.generate_reply",
                return_value="已解释。",
            ) as generate, patch.object(handler, "_finish_reply"):
                handler._process_pending_file(
                    "kf", "user", "kf:user", "请解释表格内容"
                )

            load_handler.assert_not_called()
            generate.assert_called_once()
            self.assertEqual(generate.call_args.kwargs["image_path"], str(source))
            history = generate.call_args.args[0]
            self.assertEqual(len(history), 1)
            self.assertNotIn("classifier unavailable", history[0]["content"])
            self.assertNotIn("rule_id", history[0]["content"])

    def test_local_rule_processor_error_falls_back_with_original_attachment(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "待处理.xlsx"
            source.write_bytes(b"source")
            handler = self._handler(workspace)
            handler.pending_files = {
                "kf:user": {
                    "file_path": str(source),
                    "file_bytes": b"source",
                    "filename": source.name,
                    "timestamp": 0,
                }
            }
            process = Mock(side_effect=ValueError("invalid workbook"))

            with patch(
                "customer_service.handler.classify_file_rule",
                return_value=MATCH_DECISION,
            ), patch(
                "customer_service.handler.load_rule_handler",
                return_value=process,
            ), patch(
                "customer_service.handler.load_config",
                return_value={"dm_system_prompt": ""},
            ), patch(
                "customer_service.handler.generate_reply",
                return_value="已改用 Codex 处理。",
            ) as generate, patch.object(handler, "_finish_reply"):
                handler._process_pending_file(
                    "kf", "user", "kf:user",
                    "请按医保正收款和非医保负收款完成匹配",
                )

            process.assert_called_once()
            generate.assert_called_once()
            self.assertEqual(generate.call_args.kwargs["image_path"], str(source))

    def test_finish_reply_uploads_recent_workspace_document_when_reply_omits_path(self):
        with tempfile.TemporaryDirectory() as workspace:
            input_path = Path(workspace) / "原始数据.xlsx"
            input_path.write_bytes(b"input")
            output_dir = Path(workspace) / "outputs"
            output_dir.mkdir()
            output_path = output_dir / "处理结果.xlsx"
            output_path.write_bytes(b"output")
            handler = self._handler(workspace)

            with patch.object(handler, "_send_reply", return_value=True), \
                    patch.object(handler, "_upload_media_file", return_value=None) as upload:
                handler._finish_reply(
                    "kf", "user", "kf:user", "处理文件", "处理完成。",
                    generation_started_at=0,
                    exclude_paths=[input_path],
                    request_output_dirs=[output_dir],
                )

            upload.assert_called_once_with(str(output_path), "file")

    def test_finish_reply_excludes_explicit_source_path_and_does_not_scan_shared_outputs(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "原始数据.xlsx"
            source.write_bytes(b"input")
            outputs = Path(workspace) / "outputs"
            other_request_dir = outputs / "request_other"
            other_request_dir.mkdir(parents=True)
            other_request_file = other_request_dir / "其他请求.xlsx"
            other_request_file.write_bytes(b"other")
            request_dir = outputs / "request_current"
            request_dir.mkdir()
            handler = self._handler(workspace)

            with patch.object(handler, "_send_reply", return_value=True), \
                    patch.object(handler, "_upload_media_file", return_value=None) as upload:
                handler._finish_reply(
                    "kf", "user", "kf:user", "处理文件",
                    f"源文件：`{source}`，其他任务：`{other_request_file}`",
                    generation_started_at=0,
                    exclude_paths=[source],
                    request_output_dirs=[request_dir],
                )

            upload.assert_not_called()

    def test_file_workflow_does_not_scan_images_outside_request_directory(self):
        with tempfile.TemporaryDirectory() as workspace:
            shared_image = Path(workspace) / "outputs" / "其他请求.png"
            shared_image.parent.mkdir()
            shared_image.write_bytes(b"image")
            request_dir = shared_image.parent / "request_current"
            request_dir.mkdir()
            handler = self._handler(workspace)

            with patch.object(handler, "_send_reply", return_value=True), \
                    patch.object(handler, "_upload_media_file", return_value=None) as upload:
                handler._finish_reply(
                    "kf", "user", "kf:user", "处理文件", "处理完成。",
                    generation_started_at=1,
                    request_output_dirs=[request_dir],
                )

            upload.assert_not_called()

    def test_request_output_directories_are_unique(self):
        with tempfile.TemporaryDirectory() as workspace:
            handler = self._handler(workspace)

            first = handler._create_request_output_dir()
            second = handler._create_request_output_dir()

            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, Path(workspace) / "outputs")
            self.assertEqual(second.parent, Path(workspace) / "outputs")

    def test_finish_reply_uses_request_output_dirs_parameter(self):
        parameters = inspect.signature(CSHandler._finish_reply).parameters

        self.assertIn("request_output_dirs", parameters)

    def test_concurrent_same_named_inputs_produce_distinct_request_output_paths(self):
        with tempfile.TemporaryDirectory() as workspace:
            handler = self._handler(workspace)
            handler.pending_files = {}
            for user in ("user-a", "user-b"):
                input_dir = Path(workspace) / f"input-{user}"
                input_dir.mkdir()
                source = input_dir / "同名输入.xlsx"
                source.write_bytes(b"source")
                handler.pending_files[f"kf:{user}"] = {
                    "file_path": str(source),
                    "file_bytes": b"source",
                    "filename": source.name,
                    "timestamp": 0,
                }

            output_paths = []
            output_paths_lock = threading.Lock()

            def process(_source_path, output_dir):
                output_path = Path(output_dir) / "同名输入_匹配结果.xlsx"
                with output_paths_lock:
                    output_paths.append(output_path)
                return PaymentMatchResult(
                    output_path=output_path,
                    candidate_groups=1,
                    kept_groups=1,
                    removed_groups=0,
                    output_rows=2,
                    timings={"total_seconds": 0.01},
                )

            with patch(
                "customer_service.handler.classify_file_rule",
                return_value=MATCH_DECISION,
            ), patch(
                "customer_service.handler.load_rule_handler",
                return_value=process,
            ), patch.object(handler, "_finish_reply") as finish:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(
                            handler._process_pending_file,
                            "kf",
                            user,
                            f"kf:{user}",
                            "请按医保正收款和非医保负收款完成匹配",
                        )
                        for user in ("user-a", "user-b")
                    ]
                    for future in futures:
                        future.result()

            self.assertEqual(len(set(output_paths)), 2)
            self.assertEqual(
                {path.parent.parent for path in output_paths},
                {Path(workspace) / "outputs"},
            )
            self.assertEqual(
                {
                    Path(call.kwargs["request_output_dirs"][0])
                    for call in finish.call_args_list
                },
                {path.parent for path in output_paths},
            )

    def test_codex_instruction_always_restricts_generated_files_to_request_directory(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "说明.xlsx"
            source.write_bytes(b"source")
            handler = self._handler(workspace)
            handler.pending_files = {
                "kf:user": {
                    "file_path": str(source),
                    "file_bytes": b"source",
                    "filename": source.name,
                    "timestamp": 0,
                }
            }

            with patch(
                "customer_service.handler.classify_file_rule",
                return_value=NO_MATCH_DECISION,
            ), patch(
                "customer_service.handler.load_config",
                return_value={"dm_system_prompt": ""},
            ), patch(
                "customer_service.handler.generate_reply", return_value="这是表格说明。"
            ) as generate, patch.object(handler, "_finish_reply") as finish:
                handler._process_pending_file("kf", "user", "kf:user", "请解释表格内容")

            instruction = generate.call_args.args[0][-1]["content"]
            request_dir = Path(finish.call_args.kwargs["request_output_dirs"][0])
            relative_dir = request_dir.relative_to(workspace).as_posix()
            self.assertIn(f"保存到相对目录 `{relative_dir}`", instruction)

    def test_request_output_creation_failure_falls_back_to_temporary_directory(self):
        with tempfile.TemporaryDirectory() as workspace:
            source_dir = Path(workspace) / "input"
            source_dir.mkdir()
            source = source_dir / "说明.xlsx"
            source.write_bytes(b"source")
            fallback_dir = Path(workspace) / "fallback-request"
            fallback_dir.mkdir()
            handler = self._handler(workspace)
            handler.pending_files = {
                "kf:user": {
                    "file_path": str(source),
                    "file_bytes": b"source",
                    "filename": source.name,
                    "timestamp": 0,
                }
            }

            with patch.object(
                handler, "_create_request_output_dir", side_effect=OSError("denied")
            ), patch(
                "customer_service.handler.tempfile.mkdtemp", return_value=str(fallback_dir)
            ), patch(
                "customer_service.handler.classify_file_rule",
                return_value=NO_MATCH_DECISION,
            ), patch(
                "customer_service.handler.load_config",
                return_value={"dm_system_prompt": ""},
            ), patch(
                "customer_service.handler.generate_reply", return_value="处理完成。"
            ) as generate, patch.object(handler, "_finish_reply") as finish:
                handler._process_pending_file("kf", "user", "kf:user", "请解释表格内容")

            generate.assert_called_once()
            self.assertEqual(
                finish.call_args.kwargs["request_output_dirs"], [fallback_dir]
            )
            self.assertFalse(source.exists())

    def test_cross_drive_temporary_output_does_not_break_local_match_reply(self):
        with tempfile.TemporaryDirectory() as input_dir:
            source = Path(input_dir) / "待处理.xlsx"
            source.write_bytes(b"source")
            fallback_dir = Path(input_dir) / "fallback-request"
            output_path = fallback_dir / "匹配结果.xlsx"
            handler = self._handler(r"D:\AI\workspace")
            handler.pending_files = {
                "kf:user": {
                    "file_path": str(source),
                    "file_bytes": b"source",
                    "filename": source.name,
                    "timestamp": 0,
                }
            }
            result = PaymentMatchResult(
                output_path=output_path,
                candidate_groups=1,
                kept_groups=1,
                removed_groups=0,
                output_rows=2,
                timings={"total_seconds": 0.01},
            )

            with patch.object(
                handler, "_create_request_output_dir", side_effect=OSError("denied")
            ), patch(
                "customer_service.handler.tempfile.mkdtemp", return_value=str(fallback_dir)
            ), patch(
                "customer_service.handler.classify_file_rule",
                return_value=MATCH_DECISION,
            ), patch(
                "customer_service.handler.load_rule_handler",
                return_value=Mock(return_value=result),
            ), patch.object(handler, "_finish_reply") as finish:
                handler._process_pending_file(
                    "kf", "user", "kf:user", "请按医保正收款和非医保负收款完成匹配"
                )

            reply = finish.call_args.args[4]
            self.assertIn("`匹配结果.xlsx`", reply)
            self.assertNotIn(f"`{output_path}`", reply)
            self.assertEqual(finish.call_args.kwargs["request_output_dirs"], [fallback_dir])

    def test_cross_drive_ai_fallback_prompt_has_no_relative_path_contradiction(self):
        with tempfile.TemporaryDirectory() as input_dir:
            source = Path(input_dir) / "说明.xlsx"
            source.write_bytes(b"source")
            fallback_dir = Path(input_dir) / "fallback-request"
            handler = self._handler(r"D:\AI\workspace")
            handler.pending_files = {
                "kf:user": {
                    "file_path": str(source),
                    "file_bytes": b"source",
                    "filename": source.name,
                    "timestamp": 0,
                }
            }

            with patch.object(
                handler, "_create_request_output_dir", side_effect=OSError("denied")
            ), patch(
                "customer_service.handler.tempfile.mkdtemp", return_value=str(fallback_dir)
            ), patch(
                "customer_service.handler.classify_file_rule",
                return_value=NO_MATCH_DECISION,
            ), patch(
                "customer_service.handler.load_config",
                return_value={"dm_system_prompt": ""},
            ), patch(
                "customer_service.handler.generate_reply", return_value="处理完成。"
            ) as generate, patch.object(handler, "_finish_reply"):
                handler._process_pending_file("kf", "user", "kf:user", "请解释表格内容")

            instruction = generate.call_args.args[0][-1]["content"]
            self.assertIn(f"保存到目录 `{fallback_dir.as_posix()}`", instruction)
            self.assertIn("回复中只包含生成文件的文件名", instruction)
            self.assertNotIn("保存到相对目录", instruction)

    def test_handler_import_succeeds_when_openpyxl_is_unavailable(self):
        script = """
import builtins
real_import = builtins.__import__
def blocked_import(name, *args, **kwargs):
    if name == 'customer_service.excel_payment_matcher' or name.startswith('openpyxl'):
        raise ModuleNotFoundError("No module named 'openpyxl'")
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked_import
import customer_service.handler
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_long_chinese_reply_is_split_below_wecom_byte_limit(self):
        handler = self._handler()
        reply = "加盟前需要核实经营主体、商标和真实流水。" * 90

        with patch("customer_service.handler.requests.post", return_value=_Response()) as post:
            self.assertTrue(handler._send_reply("kf", "user", reply))

        self.assertGreater(post.call_count, 1)
        self.assertLessEqual(post.call_count, 4)
        message_ids = set()
        for call in post.call_args_list:
            payload = call.kwargs["json"]
            content = payload["text"]["content"]
            self.assertLessEqual(len(content.encode("utf-8")), 2048)
            message_ids.add(payload["msgid"])
        self.assertEqual(len(message_ids), post.call_count)

    def test_web_pdf_link_is_not_treated_as_local_file(self):
        with tempfile.TemporaryDirectory() as workspace:
            local_file = os.path.join(workspace, "report.pdf")
            with open(local_file, "wb") as stream:
                stream.write(b"%PDF-test")

            handler = self._handler(workspace)
            reply = (
                "参考[税务公示](https://example.test/download?filename=remote.pdf)，"
                "本地结果文件是 `report.pdf`。"
            )

            self.assertEqual(handler._extract_file_paths(reply), [local_file])

    def test_send_failure_event_is_handled_before_origin_filter(self):
        handler = self._handler()
        handler.seen_msgids = set()
        handler.MAX_SEEN = 1000
        event = {
            "msgid": "event-1",
            "msgtype": "event",
            "origin": 4,
            "event": {
                "event_type": "msg_send_fail",
                "external_userid": "user",
                "fail_msgid": "cs-1",
                "fail_type": 6,
            },
        }

        with patch.object(handler, "_handle_event") as handle_event:
            handler._dispatch(event)

        handle_event.assert_called_once_with(event)

    def test_type_13_failure_retries_only_the_missing_chunk_as_plain_text(self):
        handler = self._handler()
        handler.outbound_messages["cs-1"] = {
            "open_kfid": "kf",
            "external_userid": "user",
            "chunk_text": "核实[医院通报](https://example.test/a)和 `检查结果`。" * 40,
            "chunk_index": 1,
            "total_chunks": 3,
            "retry_count": 0,
            "timestamp": 1,
        }
        event = {
            "event": {
                "event_type": "msg_send_fail",
                "external_userid": "user",
                "fail_msgid": "cs-1",
                "fail_type": 13,
            }
        }

        with patch.object(handler, "_send_text_content", return_value=True) as send:
            handler._handle_event(event)

        self.assertGreaterEqual(send.call_count, 1)
        self.assertLessEqual(send.call_count, 2)
        for call in send.call_args_list:
            content = call.args[2]
            self.assertIn("补发原第 1/3 段", content)
            self.assertNotIn("https://", content)
            self.assertNotIn("`", content)
            self.assertEqual(call.kwargs["retry_count"], 1)
        self.assertNotIn("cs-1", handler.outbound_messages)

    def test_type_13_retry_is_not_retried_twice(self):
        handler = self._handler()
        handler.outbound_messages["retry-1"] = {
            "open_kfid": "kf",
            "external_userid": "user",
            "chunk_text": "补发内容",
            "chunk_index": 1,
            "total_chunks": 1,
            "retry_count": 1,
            "timestamp": 1,
        }
        event = {
            "event": {
                "event_type": "msg_send_fail",
                "external_userid": "user",
                "fail_msgid": "retry-1",
                "fail_type": 13,
            }
        }

        with patch.object(handler, "_send_text_content") as send:
            handler._handle_event(event)

        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
