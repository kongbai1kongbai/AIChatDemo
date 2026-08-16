# Excel Payment Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 10 分钟内一次读取、内存匹配校验并一次导出医保正负收款匹配结果，同时可靠回传生成文件。

**Architecture:** 新建纯业务处理模块负责识别、匹配、校验和导出；`CSHandler` 只负责路由和回传。匹配处理优先走本地确定性路径，其他 Excel 请求继续走 Codex CLI。

**Tech Stack:** Python 3.12、openpyxl 3.1、unittest、企业微信客服 API

## Global Constraints

- 工作簿只读取一次、只导出一次。
- 金额转换为整数分后比较，不使用浮点数直接相等。
- 原始工作表不得修改。
- 只抽查渲染结果页，不渲染完整原表。
- 总处理时间目标小于 10 分钟。

---

### Task 1: 本地匹配处理器

**Files:**
- Create: `customer_service/excel_payment_matcher.py`
- Create: `tests/test_excel_payment_matcher.py`

**Interfaces:**
- Produces: `matches_payment_rule(filename, instruction) -> bool`
- Produces: `process_payment_workbook(input_path, output_dir) -> PaymentMatchResult`

- [ ] **Step 1: 写失败测试**，使用临时工作簿构造以下记录并断言结果：`医保 +100 -> 现金 -100` 保留两条；追加稍后的 `微信 +100` 时保留三条；将 `微信 +100` 移到医保之前时整组剔除；三个医保正收款只选择最早一条。

```python
result = process_payment_workbook(source, output_dir)
assert result.candidate_groups == 3
assert result.kept_groups == 2
assert result.removed_groups == 1
assert result.output_rows == 5
```

- [ ] **Step 2: 验证红灯**。

```powershell
python -m unittest tests.test_excel_payment_matcher -v
```

预期：`ModuleNotFoundError: customer_service.excel_payment_matcher`。

- [ ] **Step 3: 实现最小处理器**。`PaymentMatchResult` 包含 `output_path`、`candidate_groups`、`kept_groups`、`removed_groups`、`output_rows`、`timings`；金额通过 `Decimal(str(value))` 转为整数分，结果页写入标题、规则、汇总、表头和按时间排序的数据。
- [ ] **Step 4: 验证绿灯**，再次运行 `python -m unittest tests.test_excel_payment_matcher -v`，预期全部通过。

### Task 2: 客服流程集成与文件兜底回传

**Files:**
- Modify: `customer_service/handler.py`
- Modify: `setup.ps1`
- Create: `requirements-customer-service.txt`
- Modify: `tests/test_customer_service_delivery.py`

**Interfaces:**
- Consumes: `matches_payment_rule`、`process_payment_workbook`
- Produces: `_find_recent_workspace_files(since_ts, exclude_paths=None) -> list[str]`

- [ ] **Step 1: 写本地路由失败测试**。模拟待处理 `.xlsx`，断言 `matches_payment_rule(...)` 为真时调用 `process_payment_workbook(...)`，并断言 `generate_reply` 未被调用。
- [ ] **Step 2: 写文件发现失败测试**。在临时 `workspace/outputs` 中创建新的中文名 `.xlsx`，调用 `_finish_reply` 且回复中不含路径，断言 `_upload_media_file(path, "file")` 被调用。
- [ ] **Step 3: 验证红灯**。

```powershell
python -m unittest tests.test_customer_service_delivery -v
```

预期：本地路由与最近文档发现测试失败。

- [ ] **Step 4: 集成处理器**。匹配成功后回复耗时摘要并让 `_finish_reply` 上传结果；扫描目录限定为 `workspace`、`outputs`、`generated` 和 `cs_files`，排除输入文件。
- [ ] **Step 5: 声明依赖**。在 `requirements-customer-service.txt` 固定 `openpyxl>=3.1,<4`，并让 `setup.ps1` 安装该文件。
- [ ] **Step 6: 验证绿灯**。运行 `python -m unittest discover -s tests -v`，预期零失败。

### Task 3: 实表计时与结果验证

**Files:**
- Output: `D:\AI\outputs\payment-match-20260722\东园主单_正负收款匹配_快速处理.xlsx`

**Interfaces:**
- Consumes: 用户提供的 33,002 行工作簿
- Produces: 可下载、可审计的最终结果文件和分阶段耗时

- [ ] **Step 1: 实表计时**。调用 `process_payment_workbook` 一次，打印 `timings`；预期总耗时小于 600 秒。
- [ ] **Step 2: 数量核对**。预期候选 101 组、保留 85 组、剔除 16 组、结果 209 行。
- [ ] **Step 3: 结果页抽查**。用表格工具导入最终文件，只检查 `匹配结果!A1:M20` 和末尾 10 行，并渲染 `匹配结果!A1:M20`。
- [ ] **Step 4: 文件完整性**。重新打开导出文件，扫描 `#REF!|#DIV/0!|#VALUE!|#NAME?|#N/A`，预期零结果。
