import importlib
import json
import os
from dataclasses import dataclass

from shared.ai_engine import generate_rule_classification_codexcli


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


RULES = (
    RuleDefinition(
        rule_id="payment_match_v1",
        name="医保正收款与非医保负收款匹配",
        description=(
            "输入必须是包含单据编号、流水号、日期、时间、帐簿编号、账簿名称、"
            "收款员、收款金额、找零和抹零列的 .xlsx 工作簿。\n"
            "按日期和收款金额绝对值（精确到分）分组，将医保正收款与非医保负收款"
            "按时间升序一对一配对。\n"
            "同日同额非医保正收款按配对顺序逐条使用，每条源流水最多属于一个组。\n"
            "非医保正收款严格早于医保正收款时剔除该组；医保正收款更早或两者同一"
            "时间时保留该组，并包含非医保正收款。\n"
            "结果按组集中展示，组内按日期和时间升序排列，原工作表保持不变。"
        ),
        extensions=(".xlsx",),
        handler_path=(
            "customer_service.excel_payment_matcher:process_payment_workbook"
        ),
    ),
)

_RULES_BY_ID = {rule.rule_id: rule for rule in RULES}
_DECISION_FIELDS = {"decision", "rule_id", "confidence", "reason"}


def _no_match(reason):
    return RuleDecision(
        decision="no_match",
        rule_id=None,
        confidence="low",
        reason=reason,
    )


def build_rule_classification_prompt(extension, instruction):
    """Build a classifier prompt without file names, paths, or conversation data."""
    rule_sections = []
    for rule in RULES:
        extensions = ", ".join(rule.extensions)
        rule_sections.append(
            f"rule_id: {rule.rule_id}\n"
            f"名称: {rule.name}\n"
            f"支持扩展名: {extensions}\n"
            f"完整规则:\n{rule.description}"
        )

    rules_text = "\n\n".join(rule_sections)
    return (
        "你是文件处理规则分类器。只根据下面给出的文件扩展名、用户指令和规则"
        "说明判断，不要使用工具，不要访问文件或外部信息。\n"
        "用户明确请求某条规则对应的业务处理，且没有增删、修改或冲突条件时，"
        "可以返回 match。不要求用户逐条复述输入字段和内部处理约束，未提及的细节"
        "按登记规则执行。用户增加、删除、修改任何规则条件，表达含糊，或无法确认"
        "具体业务规则时，一律返回 no_match。\n\n"
        f"文件扩展名:\n{extension or '(none)'}\n\n"
        f"用户指令:\n{instruction or '(empty)'}\n\n"
        f"已登记规则:\n{rules_text}\n\n"
        "只输出一个裸 JSON 对象，不要 Markdown 代码块，不要解释，不要添加字段。\n"
        "命中格式: "
        '{"decision":"match","rule_id":"<rule_id>",'
        '"confidence":"high","reason":"<brief reason>"}\n'
        "不命中格式: "
        '{"decision":"no_match","rule_id":null,'
        '"confidence":"high|medium|low","reason":"<brief reason>"}'
    )


def parse_rule_decision(raw_output, extension):
    """Strictly validate classifier output and default every ambiguity to no-match."""
    if not isinstance(raw_output, str) or not raw_output.strip():
        return _no_match("empty classifier output")

    raw_output = raw_output.strip()
    if not raw_output.startswith("{") or not raw_output.endswith("}"):
        return _no_match("classifier output is not raw JSON")

    try:
        payload = json.loads(raw_output)
    except (TypeError, ValueError):
        return _no_match("invalid classifier JSON")

    if not isinstance(payload, dict) or set(payload) != _DECISION_FIELDS:
        return _no_match("classifier JSON schema mismatch")

    decision = payload.get("decision")
    rule_id = payload.get("rule_id")
    confidence = payload.get("confidence")
    reason = payload.get("reason")
    if decision not in {"match", "no_match"}:
        return _no_match("invalid decision")
    if confidence not in {"high", "medium", "low"}:
        return _no_match("invalid confidence")
    if not isinstance(reason, str) or not reason.strip():
        return _no_match("invalid reason")

    if decision == "no_match":
        if rule_id is not None:
            return _no_match("no-match response contains a rule")
        return RuleDecision("no_match", None, confidence, reason.strip())

    if confidence != "high" or not isinstance(rule_id, str):
        return _no_match("match is not high confidence")
    rule = _RULES_BY_ID.get(rule_id)
    if rule is None:
        return _no_match("unknown rule")

    normalized_extension = (extension or "").lower()
    if normalized_extension not in rule.extensions:
        return _no_match("file extension is not supported by the rule")

    return RuleDecision("match", rule_id, confidence, reason.strip())


def classify_file_rule(filename, instruction, model_config):
    """Classify a file request; failures deliberately return a no-match decision."""
    extension = os.path.splitext(filename or "")[1].lower()
    prompt = build_rule_classification_prompt(extension, instruction)
    try:
        raw_output = generate_rule_classification_codexcli(prompt, model_config)
        decision = parse_rule_decision(raw_output, extension)
    except Exception as exc:
        decision = _no_match(f"classifier exception: {exc}")

    print(f"[rule-router] decision={decision.decision} "
          f"rule_id={decision.rule_id or '-'} "
          f"confidence={decision.confidence} reason={decision.reason}",
          flush=True)
    return decision


def load_rule_handler(rule_id):
    """Load a registered local processor only after its rule has matched."""
    rule = _RULES_BY_ID.get(rule_id)
    if rule is None:
        raise KeyError(f"Unknown rule: {rule_id}")
    module_name, attribute = rule.handler_path.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attribute)
