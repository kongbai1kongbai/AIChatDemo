# Codex CLI 两阶段规则路由设计

## 目标

客服收到文件和处理指令后，先由当前配置的 Codex CLI 判断该指令是否与已登记的本地规则完全等价。只有高置信度精确命中时才执行本地确定性处理器；其他情况统一回退现有完整 Codex 文件处理流程。

首个登记规则为 `payment_match_v1`，对应现有医保正收款与非医保负收款匹配处理器。

## 设计原则

- AI 负责语义路由，本地处理器负责确定性计算。
- 采用严格匹配：新增、删除、修改、冲突或含糊的条件均不得命中已有规则。
- 分类失败必须安全回退，不得中断客服文件流程。
- 分类不读取附件、不执行命令、不修改文件，也不写入客户对话历史。
- 文件请求目录隔离、输入清理和附件回传逻辑保持不变。

## 规则注册表

规则以稳定 `rule_id` 注册，每条规则包含：

- `rule_id`：例如 `payment_match_v1`。
- `name`：用于日志和诊断的名称。
- `description`：完整、无歧义的业务规则说明，供 Codex 分类。
- `extensions`：允许的文件扩展名。
- `handler`：命中后调用的本地处理函数。

注册表是本地快速处理器的唯一入口。AI 返回未知 `rule_id` 时必须回退完整 Codex。

## 两阶段流程

1. 客服接收文件并取得用户处理指令。
2. 创建本次请求独立输出目录。
3. 将文件扩展名、用户指令和所有已登记规则描述交给 Codex CLI 分类器；不传文件名或路径。
4. 分类器不接收附件，使用只读沙箱和禁止审批策略。
5. 解析分类器的严格 JSON 输出。
6. 仅当 `decision=match`、`confidence=high`、`rule_id` 已登记且扩展名符合时，调用该规则的本地处理器。
7. 分类为 `no_match`、输出无效、超时、异常或本地处理失败时，将原文件和原始用户要求交给现有完整 Codex 流程。
8. 结果仍只从本次请求输出目录回传。

## 分类输出契约

分类器只允许输出一个 JSON 对象，不得包含 Markdown 代码块或额外文字：

```json
{
  "decision": "match",
  "rule_id": "payment_match_v1",
  "confidence": "high",
  "reason": "用户规则与已有规则完全一致"
}
```

未命中时使用：

```json
{
  "decision": "no_match",
  "rule_id": null,
  "confidence": "high",
  "reason": "用户增加了金额允许误差条件"
}
```

解析器只接受 `match` 和 `no_match`。`match` 必须同时具备高置信度和有效规则编号，否则按未命中处理。

## Codex CLI 分类调用

- 使用当前 `codexcli_path` 和 `codexcli_model`。
- 新增独立分类入口，不复用完整文件处理提示词。
- 不传附件路径，工作目录只用于 Codex CLI 正常启动。
- 使用 `read-only` 沙箱、`approval=never`，且提示词明确禁止工具调用和文件访问。
- 默认超时 120 秒，通过 `codexcli_rule_timeout` 配置覆盖。
- 分类结果不加入 `history`，避免污染后续完整处理上下文。

## 日志

终端打印可追踪但不泄露附件内容的日志：

```text
[rule-router] Classifying instruction with Codex CLI...
[rule-router] Matched payment_match_v1 in 6.2s
```

未命中或异常时打印原因并明确进入回退：

```text
[rule-router] No exact match: changed amount condition; falling back to Codex
[rule-router] Invalid classifier output; falling back to Codex
```

## 错误处理

- Codex CLI 不可用、退出非零、超时或空输出：回退完整 Codex。
- JSON 解码失败、字段缺失、未知枚举或未知规则：回退完整 Codex。
- 规则扩展名不匹配：回退完整 Codex。
- 本地依赖缺失、工作簿缺列或处理异常：回退完整 Codex。
- 所有路径均保留现有 `finally` 输入清理和请求目录隔离。

## 性能预期

- 已知规则：Codex 分类耗时加本地处理耗时；当前真实工作簿本地处理约 4 秒。
- 未知规则：Codex 分类耗时加现有完整 Codex 文件处理耗时。
- 分类不上传或读取附件，以缩短第一阶段耗时并降低误操作风险。

## 测试

- 完全相同规则和语义等价改写命中 `payment_match_v1`。
- 新增、删除或修改任一业务条件时回退完整 Codex。
- 含糊指令、低置信度、非法 JSON、未知规则和超时均回退。
- 分类调用不接收附件，且分类内容不进入客户历史。
- 命中本地规则时不调用完整 Codex。
- 本地处理失败后完整 Codex 仍收到原文件和原始指令。
- 请求级输出隔离、图片和文档回传、跨盘降级及输入清理测试继续通过。
