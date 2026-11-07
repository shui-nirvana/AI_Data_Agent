"""Model-assisted presentation of D facts; never changes validation or publication."""

import hashlib
import json
import re

from backend.llm.client import Message, ModelGateway, ModelRequest
from backend.llm.generator import parse_model_json

STRUCTURAL = ("input_hash", "same_columns", "same_row_count", "zero_change_bytes")
CELL_CHECK = "exact_authorized_cells_and_row_positions"
CHECK_LABELS = {
    "input_hash": ("The input file matches the fingerprint recorded at approval", "Input fingerprint matches"),
    "same_columns": ("Output columns match the input and approved specification", "Column structure matches"),
    "same_row_count": ("Output row count matches the input", "Row count matches"),
    "zero_change_bytes": ("Output byte consistency when all columns are preserved", "All-preserve byte check"),
    "exact_authorized_cells_and_row_positions": (
        "Every cell and row position follows the approved rules", "Cells and row positions follow approved rules"),
}
BYTE_CHECK_LABEL = "Output byte consistency condition"
BYTE_CHECK_NOTE = " The complete input and output bytes must match only when all columns are preserved; approved changes do not require identical bytes."
SYSTEM = (
    "You organize an English explanation of an independent CSV validation report. "
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
        sentence = f"{label}: {'passed' if checks[key] else 'failed'}."
        if key == "zero_change_bytes":
            sentence += BYTE_CHECK_NOTE
        sentences.append(sentence)
    if seen != set(checks):
        raise ValueError("INVALID_MODEL_EXPLANATION")
    verdict = "passed" if facts["verdict"] == "passed" else "failed"
    lines = [f"Independent validation D: {verdict}."]
    if CELL_CHECK in checks:
        changed, mismatches = facts["changed_cells"], facts["mismatch_count"]
        lines.append(f"Cell comparison: {changed} changed cell{'s' if changed != 1 else ''}, "
                     f"{mismatches} mismatch{'es' if mismatches != 1 else ''}.")
        lines.append("The D report gives the total changes, not counts by column.")
    else:
        lines.append("Structural checks did not all pass, so cells were not compared and change or mismatch counts cannot be inferred.")
    lines.extend(sentences)
    lines.append("Validation covers only the approved rules and reported checks; it does not mean all data issues were found or fixed.")
    return "\n".join(lines)


def clarify_byte_check(text, all_preserved=None):
    """Update legacy wording and add locally verified applicability when available."""
    if not isinstance(text, str):
        return text
    labels = {
        "输入与批准规范的哈希一致": CHECK_LABELS["input_hash"][0],
        "输入哈希匹配": CHECK_LABELS["input_hash"][1],
        "输入文件与批准时记录的文件指纹一致": CHECK_LABELS["input_hash"][0],
        "输入文件指纹一致": CHECK_LABELS["input_hash"][1],
        "输出列与输入及批准规范一致": CHECK_LABELS["same_columns"][0],
        "列结构一致": CHECK_LABELS["same_columns"][1],
        "输出行数与输入一致": CHECK_LABELS["same_row_count"][0],
        "行数一致": CHECK_LABELS["same_row_count"][1],
        "逐格结果及行位置符合批准规则": CHECK_LABELS[CELL_CHECK][0],
        "逐格与行位置检查": CHECK_LABELS[CELL_CHECK][1],
        "每格内容及行位置符合批准规则": CHECK_LABELS[CELL_CHECK][0],
    }
    for old, label in labels.items():
        for status, english in (("通过", "passed"), ("未通过", "failed")):
            text = text.replace(f"{old}：{status}。", f"{label}: {english}.")
    byte_labels = ("全保留时的输出字节一致性要求", "全保留字节检查", "输出字节一致性条件")
    old_note = "仅全保留时要求输入与输出的完整字节相同；批准修改时不要求字节相同。"
    for old in byte_labels:
        for status, english in (("通过", "passed"), ("未通过", "failed")):
            old_sentence = f"{old}：{status}。"
            new_sentence = f"{BYTE_CHECK_LABEL}: {english}." + BYTE_CHECK_NOTE
            text = text.replace(old_sentence + old_note, new_sentence)
            text = text.replace(old_sentence, new_sentence)
    text = text.replace("D 独立验证结论：通过。", "Independent validation D: passed.")
    text = text.replace("D 独立验证结论：未通过。", "Independent validation D: failed.")
    text = re.sub(
        r"逐格比较：实际变更 ([0-9]+) 格，不匹配 ([0-9]+) 格。",
        lambda m: f"Cell comparison: {m[1]} changed cell{'s' if int(m[1]) != 1 else ''}, "
                  f"{m[2]} mismatch{'es' if int(m[2]) != 1 else ''}.",
        text,
    )
    text = text.replace("D 报告提供的是变更总数，不包含各列变更数量。",
                        "The D report gives the total changes, not counts by column.")
    text = text.replace("结构检查未全部通过，未进行逐格比较，不能据此判断变更或不匹配数量。",
                        "Structural checks did not all pass, so cells were not compared and change or mismatch counts cannot be inferred.")
    text = text.replace("验证只针对批准规则及报告中的检查，不代表发现或修复了数据中的所有问题。",
                        "Validation covers only the approved rules and reported checks; it does not mean all data issues were found or fixed.")
    if all_preserved is None:
        return text
    detail = (" All columns were preserved; the complete input and output bytes must match."
              if all_preserved else
              " Changes were approved; the complete input and output bytes need not match.")
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
