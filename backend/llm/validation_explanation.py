"""Model-assisted presentation of D facts; never changes validation or publication."""

import hashlib
import json

from backend.llm.client import Message, ModelGateway, ModelRequest
from backend.llm.generator import parse_model_json

STRUCTURAL = ("input_hash", "same_columns", "same_row_count", "zero_change_bytes")
CELL_CHECK = "exact_authorized_cells_and_row_positions"
CHECK_LABELS = {
    "input_hash": ("输入文件与批准时记录的文件指纹一致", "输入文件指纹一致"),
    "same_columns": ("输出列与输入及批准规范一致", "列结构一致"),
    "same_row_count": ("输出行数与输入一致", "行数一致"),
    "zero_change_bytes": ("全保留时的输出字节一致性要求", "全保留字节检查"),
    "exact_authorized_cells_and_row_positions": (
        "每格内容及行位置符合批准规则", "每格内容及行位置符合批准规则"),
}
BYTE_CHECK_LABEL = "输出字节一致性条件"
BYTE_CHECK_NOTE = "仅全保留时要求输入与输出的完整字节相同；批准修改时不要求字节相同。"
SYSTEM = (
    "You organize a Chinese explanation of an independent CSV validation report. "
    "Return ONLY JSON with exactly one key items: a list containing every supplied "
    "check exactly once, each as {\"check\": its id, \"style\": \"plain\" or \"brief\"}. "
    "Order the checks by relevance, placing failed checks first. Do not add any other "
    "content or infer facts. The server renders verified facts and fixed wording."
)


def validation_facts(report):
    """Project only validated, non-identifying facts; reject inconsistent reports."""
    if not isinstance(report, dict) or report.get("schema_version") != "1.0" \
            or report.get("validator_version") != "1.0":
        raise ValueError("INVALID_VALIDATION_REPORT")
    checks = report.get("checks")
    if not isinstance(checks, list):
        raise ValueError("INVALID_VALIDATION_REPORT")
    results = {}
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("INVALID_VALIDATION_REPORT")
        key, passed = check.get("id"), check.get("passed")
        if not isinstance(key, str) or key not in CHECK_LABELS or key in results or type(passed) is not bool:
            raise ValueError("INVALID_VALIDATION_REPORT")
        results[key] = passed
    if not set(STRUCTURAL).issubset(results):
        raise ValueError("INVALID_VALIDATION_REPORT")
    structural_ok = all(results[key] for key in STRUCTURAL)
    expected = set(STRUCTURAL) | ({CELL_CHECK} if structural_ok else set())
    if set(results) != expected:
        raise ValueError("INVALID_VALIDATION_REPORT")
    verdict = report.get("verdict")
    expected_verdict = "passed" if structural_ok and results[CELL_CHECK] else "rejected"
    if verdict != expected_verdict:
        raise ValueError("INVALID_VALIDATION_REPORT")
    facts = {"verdict": verdict, "checks": [{"id": k, "passed": results[k]}
                                             for k in (*STRUCTURAL, CELL_CHECK) if k in results]}
    if structural_ok:
        changed, mismatch = report.get("changed_cells"), report.get("mismatch_count")
        if (type(changed) is not int or type(mismatch) is not int
                or not 0 <= changed <= 2**63 - 1 or not 0 <= mismatch <= 2**63 - 1
                or (mismatch == 0) != results[CELL_CHECK]):
            raise ValueError("INVALID_VALIDATION_REPORT")
        facts.update(changed_cells=changed, mismatch_count=mismatch)
    return facts


def render_explanation(facts, text):
    output = parse_model_json(text)
    if not isinstance(output, dict) or set(output) != {"items"} or not isinstance(output["items"], list):
        raise ValueError("INVALID_MODEL_EXPLANATION")
    checks = {check["id"]: check["passed"] for check in facts["checks"]}
    seen, sentences = set(), []
    for item in output["items"]:
        if not isinstance(item, dict) or set(item) != {"check", "style"}:
            raise ValueError("INVALID_MODEL_EXPLANATION")
        key, style = item["check"], item["style"]
        if not isinstance(key, str) or key not in checks or key in seen or style not in ("plain", "brief"):
            raise ValueError("INVALID_MODEL_EXPLANATION")
        seen.add(key)
        label = BYTE_CHECK_LABEL if key == "zero_change_bytes" else CHECK_LABELS[key][0 if style == "plain" else 1]
        sentence = f"{label}：{'通过' if checks[key] else '未通过'}。"
        if key == "zero_change_bytes":
            sentence += BYTE_CHECK_NOTE
        sentences.append(sentence)
    if seen != set(checks):
        raise ValueError("INVALID_MODEL_EXPLANATION")
    verdict = "通过" if facts["verdict"] == "passed" else "未通过"
    lines = [f"D 独立验证结论：{verdict}。"]
    if CELL_CHECK in checks:
        lines.append(f"逐格比较：实际变更 {facts['changed_cells']} 格，"
                     f"不匹配 {facts['mismatch_count']} 格。")
        lines.append("D 报告提供的是变更总数，不包含各列变更数量。")
    else:
        lines.append("结构检查未全部通过，未进行逐格比较，不能据此判断变更或不匹配数量。")
    lines.extend(sentences)
    lines.append("验证只针对批准规则及报告中的检查，不代表发现或修复了数据中的所有问题。")
    return "\n".join(lines)


def clarify_byte_check(text, all_preserved=None):
    """Update legacy wording and add locally verified applicability when available."""
    if not isinstance(text, str):
        return text
    text = text.replace("输入与批准规范的哈希一致", "输入文件与批准时记录的文件指纹一致")
    text = text.replace("输入哈希匹配", "输入文件指纹一致")
    text = text.replace("逐格结果及行位置符合批准规则", "每格内容及行位置符合批准规则")
    text = text.replace("逐格与行位置检查", "每格内容及行位置符合批准规则")
    for old in ("全保留时的输出字节一致性要求", "全保留字节检查"):
        text = text.replace(old + "：通过。", BYTE_CHECK_LABEL + "：通过。" + BYTE_CHECK_NOTE)
        text = text.replace(old + "：未通过。", BYTE_CHECK_LABEL + "：未通过。" + BYTE_CHECK_NOTE)
    if all_preserved is None:
        return text
    detail = ("本次全部保留，要求输入与输出的完整字节相同。"
              if all_preserved else
              "本次批准了修改，不要求输入与输出的完整字节相同。")
    return text.replace(BYTE_CHECK_NOTE, detail)


class InvalidExplanation(ValueError):
    def __init__(self, response):
        super().__init__("INVALID_MODEL_EXPLANATION")
        self.input_tokens = response.input_tokens
        self.output_tokens = response.output_tokens


class ValidationExplainer:
    def __init__(self, gateway: ModelGateway):
        self.gateway = gateway

    async def explain(self, task_id, facts):
        request_id = hashlib.sha256(f"validation-explanation:{task_id}".encode()).hexdigest()
        response = await self.gateway.complete(ModelRequest(
            request_id=request_id,
            messages=(Message(role="system", content=SYSTEM),
                      Message(role="user", content=json.dumps(facts, ensure_ascii=True))),
            max_output_tokens=512,
            timeout_seconds=30,
        ))
        try:
            text = render_explanation(facts, response.text)
        except (ValueError, TypeError) as exc:
            raise InvalidExplanation(response) from exc
        return {"text": text, "provider": response.provider, "model": response.model,
                "input_tokens": response.input_tokens, "output_tokens": response.output_tokens}
