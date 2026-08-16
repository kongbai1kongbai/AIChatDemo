import unittest
from unittest import mock

from customer_service.rule_router import (
    RuleDecision,
    classify_file_rule,
    load_rule_handler,
    parse_rule_decision,
)


MATCH_JSON = (
    '{"decision":"match","rule_id":"payment_match_v1",'
    '"confidence":"high","reason":"The request exactly matches the rule."}'
)


class RuleRouterTests(unittest.TestCase):
    def test_classifier_prompt_contains_only_extension_instruction_and_rules(self):
        with mock.patch(
            "customer_service.rule_router.generate_rule_classification_codexcli",
            return_value=MATCH_JSON,
        ) as generate:
            decision = classify_file_rule(
                r"C:\secret\patient-ledger.xlsx",
                "按医保正负收款规则处理",
                {"codexcli_model": "test-model"},
            )

        self.assertEqual(decision.rule_id, "payment_match_v1")
        prompt = generate.call_args.args[0]
        self.assertIn(".xlsx", prompt)
        self.assertIn("按医保正负收款规则处理", prompt)
        self.assertIn("payment_match_v1", prompt)
        self.assertIn("非医保正收款严格早于医保正收款时剔除", prompt)
        self.assertIn("不要求用户逐条复述", prompt)
        self.assertIn("未提及的细节按登记规则执行", prompt)
        self.assertNotIn("patient-ledger", prompt)
        self.assertNotIn(r"C:\secret", prompt)
        self.assertNotIn("对话记录", prompt)
        self.assertNotIn("附件", prompt)

    def test_accepts_only_high_confidence_registered_rule_for_extension(self):
        decision = parse_rule_decision(MATCH_JSON, ".xlsx")

        self.assertEqual(decision, RuleDecision(
            decision="match",
            rule_id="payment_match_v1",
            confidence="high",
            reason="The request exactly matches the rule.",
        ))

    def test_rejects_fenced_json(self):
        decision = parse_rule_decision(f"```json\n{MATCH_JSON}\n```", ".xlsx")

        self.assertEqual(decision.decision, "no_match")
        self.assertIsNone(decision.rule_id)

    def test_rejects_low_confidence_unknown_rule_and_extension_mismatch(self):
        cases = [
            MATCH_JSON.replace('"high"', '"medium"'),
            MATCH_JSON.replace("payment_match_v1", "unknown_rule"),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_rule_decision(raw, ".xlsx").rule_id)

        self.assertIsNone(parse_rule_decision(MATCH_JSON, ".xls").rule_id)

    def test_rejects_malformed_or_schema_incompatible_output(self):
        cases = [
            "",
            "not json",
            '{"decision":"match","rule_id":"payment_match_v1",'
            '"confidence":"high"}',
            MATCH_JSON[:-1] + ',"extra":true}',
            MATCH_JSON.replace('"decision":"match"', '"decision":"maybe"'),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                decision = parse_rule_decision(raw, ".xlsx")
                self.assertEqual(decision.decision, "no_match")
                self.assertIsNone(decision.rule_id)

    def test_classifier_failure_returns_no_match(self):
        with mock.patch(
            "customer_service.rule_router.generate_rule_classification_codexcli",
            return_value=None,
        ):
            decision = classify_file_rule(
                "input.xlsx", "按原规则处理", {}
            )

        self.assertEqual(decision.decision, "no_match")
        self.assertIsNone(decision.rule_id)

    def test_rule_handler_is_imported_lazily(self):
        expected = object()
        module = mock.Mock(process_payment_workbook=expected)
        with mock.patch(
            "customer_service.rule_router.importlib.import_module",
            return_value=module,
        ) as import_module:
            handler = load_rule_handler("payment_match_v1")

        self.assertIs(handler, expected)
        import_module.assert_called_once_with(
            "customer_service.excel_payment_matcher"
        )


if __name__ == "__main__":
    unittest.main()
