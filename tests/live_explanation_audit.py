"""Capture an opt-in live explanation without changing its request or response."""

import json

from backend.llm.client import ModelGateway, ModelUnavailableError
from backend.llm.validation_explanation import ValidationExplainer, validation_facts
from backend.orchestration.runner import GraphRunner


def audit_explanation(api, base, headers, root, task_id, client, audit):
    calls = []
    audit["calls"] = calls

    class Observer:
        async def complete(self, request):
            entry = {"request": request.model_dump(mode="json")}
            calls.append(entry)
            try:
                response = await client.complete(request)
            except Exception as exc:
                entry["error_type"] = type(exc).__name__
                if isinstance(exc, ModelUnavailableError):
                    entry.update(error_code=exc.safe_code, input_tokens=exc.input_tokens,
                                 output_tokens=exc.output_tokens)
                raise
            entry["response"] = response.model_dump(mode="json")
            return response

    before = api.get(base + "/evidence", headers=headers).json()
    audit["before_state"] = before["state"]
    queued = api.post(base + "/validation-explanation", headers=headers)
    audit["post_status_code"] = queued.status_code
    if queued.status_code != 202:
        audit["post_detail"] = queued.json()
        assert queued.status_code == 202
    runner = GraphRunner(root, explainer=ValidationExplainer(ModelGateway(Observer())))
    status = queued.json()
    for _ in range(10):
        if status["status"] in ("completed", "failed"):
            break
        assert runner.run_once()
        status = api.get(base + "/validation-explanation", headers=headers).json()
    audit["status"] = status
    after = api.get(base + "/evidence", headers=headers).json()
    audit["after_state"] = after["state"]
    assert after == before
    assert len(calls) == 1
    assert json.loads(calls[0]["request"]["messages"][-1]["content"]) == validation_facts(before["validation"])
    assert status["status"] == "completed", status
    assert calls[0]["response"]["provider"] == "vultr"
    assert status["result"]["provider"] == "vultr"
    repeated = api.post(base + "/validation-explanation", headers=headers)
    assert repeated.status_code == 202 and repeated.json() == status
    assert not runner.run_once()
    assert len(calls) == 1
