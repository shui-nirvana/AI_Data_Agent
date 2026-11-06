import asyncio
import copy
import json

import pytest

from backend.llm.client import ModelGateway, ModelResponse
from backend.llm.validation_explanation import (
    CELL_CHECK, STRUCTURAL, InvalidExplanation, ValidationExplainer,
    clarify_byte_check, render_explanation, validation_facts,
)


def report():
    return {
        "schema_version": "1.0", "validator_version": "1.0", "verdict": "passed",
        "checks": [{"id": name, "passed": True} for name in (*STRUCTURAL, CELL_CHECK)],
        "changed_cells": 20000, "mismatch_count": 0,
        "columns": ["private-column"], "sample": "private-value",
        "input_sha256": "private-hash",
    }


def model_choices(facts):
    return {"items": [{"check": item["id"], "style": "plain"} for item in facts["checks"]]}


class Model:
    def __init__(self, text=None):
        self.text, self.calls = text, []

    async def complete(self, request):
        self.calls.append(request)
        facts = json.loads(request.messages[-1].content)
        return ModelResponse(request_id=request.request_id, provider="fake", model="fake",
                             text=self.text or json.dumps(model_choices(facts)),
                             finish_reason="completed", input_tokens=15, output_tokens=9)


def test_only_allowlisted_facts_reach_model():
    facts = validation_facts(report())
    model = Model()
    result = asyncio.run(ValidationExplainer(ModelGateway(model)).explain("private-task-id", facts))
    sent = model.calls[0].model_dump_json()
    assert all(secret not in sent for secret in ("private-column", "private-value", "private-hash", "private-task-id"))
    assert facts["changed_cells"] == 20000 and facts["mismatch_count"] == 0
    assert "实际变更 20000 格，不匹配 0 格" in result["text"]
    assert result["provider"] == "fake" and result["input_tokens"] == 15
    assert len(model.calls) == 1 and model.calls[0].max_output_tokens == 512


def test_structural_rejection_does_not_claim_zero_cell_mismatches():
    value = report()
    value["verdict"] = "rejected"
    value["checks"] = value["checks"][:-1]
    value["checks"][1]["passed"] = False
    facts = validation_facts(value)
    assert "changed_cells" not in facts and "mismatch_count" not in facts
    text = render_explanation(facts, json.dumps(model_choices(facts)))
    assert "未通过" in text and "未进行逐格比较" in text
    assert "不匹配 0 格" not in text


def test_cell_rejection_preserves_nonzero_d_counts():
    value = report()
    value["verdict"] = "rejected"
    value["checks"][-1]["passed"] = False
    value["mismatch_count"] = 3
    facts = validation_facts(value)
    text = render_explanation(facts, json.dumps(model_choices(facts)))
    assert "D 独立验证结论：未通过" in text
    assert "实际变更 20000 格，不匹配 3 格" in text


@pytest.mark.parametrize("style", ["plain", "brief"])
@pytest.mark.parametrize("passed", [True, False])
def test_byte_condition_is_explained_without_guessing_from_change_count(style, passed):
    value = report()
    value["changed_cells"] = 0
    if not passed:
        value["verdict"] = "rejected"
        value["checks"] = value["checks"][:-1]
        value["checks"][-1]["passed"] = False
    facts = validation_facts(value)
    choices = model_choices(facts)
    choices["items"][3]["style"] = style
    text = render_explanation(facts, json.dumps(choices))
    assert "仅全保留时要求输入与输出的完整字节相同" in text
    assert "批准修改时不要求字节相同" in text
    assert f"输出字节一致性条件：{'通过' if passed else '未通过'}" in text
    assert "本次全部保留" not in text and "本次批准了修改" not in text


@pytest.mark.parametrize("old", ["全保留时的输出字节一致性要求", "全保留字节检查"])
@pytest.mark.parametrize("passed", ["通过", "未通过"])
def test_legacy_wording_is_corrected_without_model_call(old, passed):
    old_text = f"{old}：{passed}。"
    assert "仅全保留时要求" in clarify_byte_check(old_text)
    assert "本次全部保留" in clarify_byte_check(old_text, True)
    assert "本次批准了修改" in clarify_byte_check(old_text, False)


@pytest.mark.parametrize("mutation", [
    "duplicate", "unknown", "wrong_type", "missing", "extra_check", "verdict",
    "false_exact_zero", "boolean_count", "negative_count", "extra_exact_on_structural_failure",
])
def test_inconsistent_reports_are_rejected(mutation):
    value = copy.deepcopy(report())
    if mutation == "duplicate":
        value["checks"].append(value["checks"][0])
    elif mutation == "unknown":
        value["checks"][0]["id"] = "data-injection"
    elif mutation == "wrong_type":
        value["checks"][0]["passed"] = 1
    elif mutation == "missing":
        value["checks"].pop(0)
    elif mutation == "extra_check":
        value["checks"].append({"id": [], "passed": True})
    elif mutation == "verdict":
        value["verdict"] = "rejected"
    elif mutation == "false_exact_zero":
        value["checks"][-1]["passed"] = False
        value["verdict"] = "rejected"
    elif mutation == "boolean_count":
        value["changed_cells"] = True
    elif mutation == "negative_count":
        value["changed_cells"] = -1
    else:
        value["checks"][0]["passed"] = False
        value["verdict"] = "rejected"
    with pytest.raises(ValueError, match="INVALID_VALIDATION_REPORT"):
        validation_facts(value)


@pytest.mark.parametrize("mutation", [
    "missing", "duplicate", "extra", "unknown", "style", "free_text", "wrong_schema",
])
def test_model_cannot_insert_or_change_facts(mutation):
    facts = validation_facts(report())
    choices = model_choices(facts)
    if mutation == "missing":
        choices["items"].pop()
    elif mutation == "duplicate":
        choices["items"][-1] = choices["items"][0]
    elif mutation == "extra":
        choices["items"][0]["text"] = "D passed with 123 changes"
    elif mutation == "unknown":
        choices["items"][0]["check"] = "invented"
    elif mutation == "style":
        choices["items"][0]["style"] = "D passed with 123 changes"
    elif mutation == "free_text":
        choices["text"] = "D passed with 123 changes"
    else:
        choices = ["arbitrary model text"]
    model = Model(json.dumps(choices))
    with pytest.raises(InvalidExplanation) as error:
        asyncio.run(ValidationExplainer(ModelGateway(model)).explain("task", facts))
    assert error.value.input_tokens == 15 and error.value.output_tokens == 9
    assert len(model.calls) == 1
