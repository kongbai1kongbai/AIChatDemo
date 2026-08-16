# Codex CLI Two-Stage Rule Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route customer-service file instructions through a strict Codex CLI semantic classifier before selecting a registered local processor or the existing full Codex file workflow.

**Architecture:** Add a read-only Codex CLI classifier primitive to `shared/ai_engine.py`, keep rule metadata and strict JSON parsing in a focused `customer_service/rule_router.py`, then replace the handler's keyword gate with the router decision. Classification never receives the attachment and every invalid, uncertain, unavailable, or failed path falls back to the existing full Codex workflow.

**Tech Stack:** Python 3.12, Codex CLI `exec`, dataclasses, JSON, `unittest`, `unittest.mock`.

## Global Constraints

- Use the configured `codexcli_path` and `codexcli_model` for classification.
- Classification uses `approval=never`, `sandbox=read-only`, no attachment, and a default timeout of 120 seconds.
- Only `decision=match`, `confidence=high`, a registered `rule_id`, and an allowed extension may select a local processor.
- Any ambiguity, malformed output, timeout, import failure, or local processor failure falls back to the existing full Codex file workflow.
- Classifier input and output must not be appended to customer conversation history.
- Preserve request-owned output directories, file/image delivery isolation, cross-drive fallback, and input cleanup.
- Follow red-green-refactor for every production change.

---

### Task 1: Read-Only Codex CLI Classification Primitive

**Files:**
- Modify: `shared/ai_engine.py:850-1039`
- Test: `tests/test_codexcli_rule_classifier.py`

**Interfaces:**
- Consumes: existing `_find_codexcli()`, `_build_codexcli_cmd()`, `_build_codexcli_env()`, `_run_codexcli_streaming()`, and `_read_codexcli_output()`.
- Produces: `generate_rule_classification_codexcli(prompt: str, model_config: dict) -> str | None`.

- [ ] **Step 1: Write failing tests for command isolation, timeout, output, and cleanup**

Create `tests/test_codexcli_rule_classifier.py` with tests that patch the existing runner helpers and assert:

```python
import os
import tempfile
import unittest
from unittest.mock import patch

from shared.ai_engine import generate_rule_classification_codexcli


class CodexCliRuleClassifierTests(unittest.TestCase):
    def test_classifier_uses_read_only_never_approval_without_attachment(self):
        with tempfile.TemporaryDirectory() as workspace:
            config = {
                "workspace_dir": workspace,
                "codexcli_path": "codex",
                "codexcli_model": "gpt-5.6-sol",
                "codexcli_rule_timeout": 37,
            }
            with patch("shared.ai_engine._run_codexcli_streaming", return_value=("", 0)) as run, \
                    patch("shared.ai_engine._read_codexcli_output", return_value='{"decision":"no_match","rule_id":null,"confidence":"high","reason":"different"}'):
                output = generate_rule_classification_codexcli("classify this", config)

            command = run.call_args.args[0]
            self.assertEqual(output[:20], '{"decision":"no_mat')
            self.assertIn("-a", command)
            self.assertEqual(command[command.index("-a") + 1], "never")
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertNotIn("--image", command)
            self.assertEqual(run.call_args.kwargs["timeout"], 37)
            self.assertEqual(run.call_args.kwargs["stdin_text"], "classify this")

    def test_classifier_returns_none_on_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as workspace, patch(
            "shared.ai_engine._run_codexcli_streaming", return_value=("failed", 1)
        ):
            self.assertIsNone(generate_rule_classification_codexcli(
                "classify", {"workspace_dir": workspace, "codexcli_path": "codex"}
            ))
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
python -m unittest tests.test_codexcli_rule_classifier -v
```

Expected: import failure because `generate_rule_classification_codexcli` does not exist.

- [ ] **Step 3: Implement the isolated classifier function**

Add a function beside `generate_reply_codexcli` that:

```python
def generate_rule_classification_codexcli(prompt, model_config):
    cli_path = model_config.get("codexcli_path") or _find_codexcli()
    workspace_dir = model_config.get("workspace_dir", r"D:\AI\workspace")
    if not os.path.isdir(workspace_dir):
        workspace_dir = os.getcwd()
    output_file = os.path.join(
        workspace_dir, f".codexcli_rule_{uuid.uuid4().hex}.txt"
    )
    command = _build_codexcli_cmd(
        cli_path, prompt, model_config, output_file, plan_only=True
    )
    command[1:1] = ["-a", "never"]
    try:
        stdout, returncode = _run_codexcli_streaming(
            command,
            _build_codexcli_env(model_config),
            timeout=model_config.get("codexcli_rule_timeout", 120),
            stdin_text=prompt,
        )
        if stdout is None or returncode != 0:
            return None
        return _read_codexcli_output(output_file, stdout) or None
    except Exception as exc:
        print(f"[rule-router] Codex classifier error: {exc}", flush=True)
        return None
    finally:
        try:
            if os.path.exists(output_file):
                os.remove(output_file)
        except Exception:
            pass
```

Print invocation and completion logs with the `[rule-router]` prefix, but do not print the full classification prompt by default.

- [ ] **Step 4: Run focused and existing Codex CLI tests**

Run:

```powershell
python -m unittest tests.test_codexcli_rule_classifier tests.test_codexcli_attachments -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add shared/ai_engine.py tests/test_codexcli_rule_classifier.py
git commit -m "Add read-only Codex rule classifier"
```

---

### Task 2: Strict Local Rule Registry and Decision Parser

**Files:**
- Create: `customer_service/rule_router.py`
- Test: `tests/test_rule_router.py`

**Interfaces:**
- Consumes: `generate_rule_classification_codexcli(prompt, model_config)` from Task 1.
- Produces: `RuleDefinition`, `RuleDecision`, `RULES`, `classify_file_rule(filename, instruction, model_config) -> RuleDecision`, and `load_rule_handler(rule_id) -> Callable`.

- [ ] **Step 1: Write failing parser and registry tests**

Create tests that establish the public contract:

```python
import unittest
from unittest.mock import patch

from customer_service.rule_router import (
    RuleDecision,
    classify_file_rule,
    load_rule_handler,
)


class RuleRouterTests(unittest.TestCase):
    def test_accepts_only_registered_high_confidence_match(self):
        response = '{"decision":"match","rule_id":"payment_match_v1","confidence":"high","reason":"same"}'
        with patch("customer_service.rule_router.generate_rule_classification_codexcli", return_value=response) as classify:
            decision = classify_file_rule("payments.xlsx", "same rule", {})
        self.assertEqual(decision.rule_id, "payment_match_v1")
        self.assertNotIn("payments.xlsx", classify.call_args.args[0])
        self.assertIn(".xlsx", classify.call_args.args[0])

    def test_rejects_low_confidence_unknown_rule_and_invalid_json(self):
        outputs = [
            '{"decision":"match","rule_id":"payment_match_v1","confidence":"low","reason":"uncertain"}',
            '{"decision":"match","rule_id":"unknown","confidence":"high","reason":"same"}',
            "```json\n{}\n```",
        ]
        for output in outputs:
            with self.subTest(output=output), patch(
                "customer_service.rule_router.generate_rule_classification_codexcli",
                return_value=output,
            ):
                self.assertIsNone(classify_file_rule("payments.xlsx", "rule", {}).rule_id)

    def test_no_output_returns_safe_no_match(self):
        with patch(
            "customer_service.rule_router.generate_rule_classification_codexcli",
            return_value=None,
        ):
            decision = classify_file_rule("payments.xlsx", "rule", {})
        self.assertEqual(decision.decision, "no_match")
```

- [ ] **Step 2: Run router tests and verify RED**

Run:

```powershell
python -m unittest tests.test_rule_router -v
```

Expected: import failure because `customer_service.rule_router` does not exist.

- [ ] **Step 3: Implement registry, strict prompt, parser, and lazy handler loading**

Implement immutable definitions using strings for processor imports so `openpyxl` remains optional at service startup. Define the complete first rule description in the same module:

```python
PAYMENT_MATCH_RULE_DESCRIPTION = """
输入必须是包含单据编号、流水号、日期、时间、帐簿编号、账簿名称、收款员、收款金额、找零和抹零列的 .xlsx 工作簿。
按日期和收款金额绝对值（精确到分）分组，将医保正收款与非医保负收款按时间升序一对一配对。
同日同额非医保正收款按配对顺序逐条使用，每条源流水最多属于一个组。
非医保正收款严格早于医保正收款时剔除该组；医保正收款更早或两者同一时间时保留该组，并包含非医保正收款。
结果按组集中展示，组内按日期和时间升序排列，原工作表保持不变。
""".strip()


@dataclass(frozen=True)
class RuleDefinition:
    rule_id: str
    name: str
    description: str
    extensions: tuple[str, ...]
    handler_path: str


@dataclass(frozen=True)
class RuleDecision:
    decision: str
    rule_id: str | None
    confidence: str
    reason: str


RULES = {
    "payment_match_v1": RuleDefinition(
        rule_id="payment_match_v1",
        name="医保正负收款匹配",
        description=PAYMENT_MATCH_RULE_DESCRIPTION,
        extensions=(".xlsx",),
        handler_path="customer_service.excel_payment_matcher:process_payment_workbook",
    )
}
```

The prompt must include the normalized file extension, user instruction, and complete rule descriptions, but not the filename, absolute path, attachment contents, conversation history, or workspace path. It must tell Codex to default to `no_match`, forbid tools and file access, and require one raw JSON object.

`classify_file_rule` must return a safe `no_match` decision for empty output, JSON errors, invalid fields, low confidence, unknown rule IDs, or extension mismatch. `load_rule_handler` must import the registered `module:function` lazily with `importlib`.

- [ ] **Step 4: Run router and optional-dependency tests**

Run:

```powershell
python -m unittest tests.test_rule_router tests.test_customer_service_delivery.CustomerServiceDeliveryTests.test_handler_import_succeeds_when_openpyxl_is_unavailable -v
```

Expected: all tests pass and importing `customer_service.handler` remains possible without `openpyxl`.

- [ ] **Step 5: Commit Task 2**

```powershell
git add customer_service/rule_router.py tests/test_rule_router.py
git commit -m "Add strict local rule registry"
```

---

### Task 3: Route Customer-Service File Processing Through Codex Classification

**Files:**
- Modify: `customer_service/handler.py:32-51,1342-1484`
- Modify: `tests/test_customer_service_delivery.py`
- Modify: `model_config.json.example`
- Modify: `customer_service/README.md`

**Interfaces:**
- Consumes: `classify_file_rule()` and `load_rule_handler()` from Task 2.
- Produces: two-stage file routing with unchanged `_finish_reply()` delivery behavior.

- [ ] **Step 1: Rewrite handler tests first to describe two-stage routing**

Replace keyword-router patches with rule-router decisions and add focused tests:

```python
match = RuleDecision("match", "payment_match_v1", "high", "same")
no_match = RuleDecision("no_match", None, "high", "changed condition")
```

The tests must assert:

- A high-confidence registered match calls the loaded local processor and never calls `generate_reply`.
- `no_match` calls `generate_reply` once with the original attachment.
- Classifier exceptions call `generate_reply` once.
- Local handler import or execution failures call `generate_reply` once.
- Classification prompt/result never appears in `handler.history`.
- Request output directories and original input cleanup remain unchanged.

- [ ] **Step 2: Run delivery tests and verify RED**

Run:

```powershell
python -m unittest tests.test_customer_service_delivery -v
```

Expected: failures because `_process_pending_file` still invokes `matches_payment_rule` directly.

- [ ] **Step 3: Replace the keyword gate with the strict router**

Import the router at module scope; it must not import `openpyxl`. In `_process_pending_file`:

```python
try:
    decision = classify_file_rule(filename, user_text, self.model_config)
except Exception as exc:
    decision = RuleDecision("no_match", None, "none", f"classifier error: {exc}")

if decision.rule_id:
    try:
        processor = load_rule_handler(decision.rule_id)
        result = processor(file_path, request_output_dir)
    except Exception as exc:
        print(
            f"[rule-router] Local rule {decision.rule_id} failed: {exc}; "
            "falling back to Codex",
            flush=True,
        )
    else:
        # Preserve the existing local-result reply and _finish_reply call.
        return
```

After any non-match or failure, continue into the existing `load_config()`, instruction construction, `generate_reply(..., image_path=file_path)`, and request-owned delivery path without modifying the original user request.

- [ ] **Step 4: Document the timeout override**

Add to `model_config.json.example`:

```json
"codexcli_rule_timeout": 120,
```

Document in `customer_service/README.md` that every file instruction is classified first by Codex CLI, exact registered matches use local processors, and all other cases use full Codex.

- [ ] **Step 5: Run focused integration tests**

Run:

```powershell
python -m unittest tests.test_rule_router tests.test_codexcli_rule_classifier tests.test_customer_service_delivery tests.test_excel_payment_matcher -v
```

Expected: all focused tests pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add customer_service/handler.py tests/test_customer_service_delivery.py model_config.json.example customer_service/README.md
git commit -m "Route file rules through Codex classifier"
```

---

### Task 4: Full Verification and Live Classifier Smoke Test

**Files:**
- No production files expected.
- Update tests only if the smoke test reveals a reproducible contract defect, using a new RED-GREEN cycle.

**Interfaces:**
- Consumes: complete two-stage router.
- Produces: verification evidence for unit behavior and the configured local Codex CLI.

- [ ] **Step 1: Run the entire automated suite**

Run:

```powershell
python -m unittest discover -s tests -v
python -m py_compile customer_service\handler.py customer_service\rule_router.py customer_service\excel_payment_matcher.py shared\ai_engine.py
git diff --check
```

Expected: zero failures, zero syntax errors, and zero whitespace errors.

- [ ] **Step 2: Run one live exact-match classifier call**

Load `D:\AI\workspace\model_config.json` and invoke `classify_file_rule` with the confirmed payment rule wording. Expected: `decision=match`, `rule_id=payment_match_v1`, `confidence=high`; no attachment path appears in the printed classifier prompt or command.

- [ ] **Step 3: Run one live changed-rule classifier call**

Invoke the classifier with an added condition such as “金额允许相差 0.01 元”. Expected: `decision=no_match` with a concise reason.

- [ ] **Step 4: Confirm repository state and push**

Run:

```powershell
git status --short
git push origin main
git rev-parse HEAD
git rev-parse origin/main
```

Expected: clean tracked working tree and identical local/remote commit IDs.
