"""Durable A -> approval -> C/D orchestration; CSV parsing and source execution occur only in workers."""

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import TypedDict

from backend.domain.models import TaskState
from backend.artifacts.publication import Publisher, freeze_candidate
from backend.domain.spec import Action, FrozenSpec
from backend.jobs.contracts import Stage
from backend.jobs.execution import ExecutedSource
from backend.llm.generator import CodeVersion, UnconfiguredGenerator
from backend.orchestration.contracts import file_hash, read_json, validate_profile
from backend.orchestration.store import WorkflowStore, new_job


class GraphState(TypedDict, total=False):
    """LangGraph state containing workflow references and digests."""
    task_id: str          # Task ID.
    input_sha256: str     # Input digest.
    profile_ref: str      # Profile reference.
    issue_ref: str        # Findings reference.
    spec_ref: str         # Cleaning specification reference.
    spec_sha256: str      # Specification digest.
    code_ref: str         # Source reference.
    code_sha256: str
    generation_kind: str
    attempt_id: str       # Current attempt ID.
    attempt_no: int
    repair_error: str
    candidate_ref: str    # Candidate artifact reference.
    candidate_sha256: str
    validation_ref: str   # Validation result reference.


def build_graph(checkpointer: object, store: WorkflowStore, generator=None):
    """Deduplicate node side effects with database stage uniqueness; resume values are wake-up signals only."""
    if checkpointer is None:
        raise ValueError("A persistent LangGraph checkpointer is required")
        
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    generator = generator if generator is not None else UnconfiguredGenerator()

    def private(path: str) -> Path:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(store.root):
            raise ValueError("Reference is outside the private task directory")
        return resolved

    def materialize(task_id: str, name: str, content: str) -> str:
        directory = store.root / "workflows" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        temporary = directory / f"{name}.part"
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return str(path)

    def wait_job(job, sources, task_state, attempt_no=1):
        stored = store.schedule(job, sources, task_state, attempt_no)
        if stored["state"] in ("queued", "running"):
            interrupt({"kind": "job", "task_id": job.task_id, "job_id": stored["job_id"]})
            stored = store.job(stored["job_id"])
        store.check_active(job.task_id)
        return stored

    def successful_result(stored):
        if stored["state"] != "finished" or not stored["result_dir"]:
            raise ValueError("Isolated job did not succeed")
        return private(stored["result_dir"])

    def profile(state: GraphState):
        task_id = state["task_id"]
        flow = store.flow(task_id)
        if flow["profile_json"]:
            return {"profile_ref": materialize(task_id, "profile.json", flow["profile_json"]),
                    "issue_ref": materialize(task_id, "issues.json", flow["issues_json"])}
        job = new_job(task_id, Stage.PROFILE_A, state["input_sha256"])
        source = store.root / "inputs" / f"{task_id}.csv"
        result = successful_result(wait_job(job, {"data.csv": str(source)}, TaskState.PROFILING))
        profile_data, issues = validate_profile(read_json(result / "profile.json"),
                                               read_json(result / "issues.json"),
                                               state["input_sha256"])
        refs = {"profile_ref": materialize(task_id, "profile.json", json.dumps(profile_data)),
                "issue_ref": materialize(task_id, "issues.json", json.dumps(issues))}
        store.accept_profile(task_id, profile_data, issues)
        return refs

    def approval(state: GraphState):
        task_id = state["task_id"]
        flow = store.flow(task_id)
        if not flow["spec_json"]:
            interrupt({"kind": "approval", "task_id": task_id, "issues": state["issue_ref"]})
            flow = store.flow(task_id)
        store.check_active(task_id)
        if not flow["spec_json"]:
            raise ValueError("Frozen approval has not been submitted")
        spec = FrozenSpec.model_validate_json(flow["spec_json"])
        return {"spec_ref": materialize(task_id, "spec.json", flow["spec_json"]),
                "spec_sha256": spec.spec_sha256}

    def route(state):
        spec = FrozenSpec.model_validate_json(store.flow(state["task_id"])["spec_json"])
        return "generate" if any(d.action != Action.PRESERVE for d in spec.decisions) else "validate"

    def generate(state):
        task_id = state["task_id"]
        store.check_active(task_id)
        code = store.code(task_id, 1)
        if code is None:
            store.generating(task_id)
            version = asyncio.run(generator.generate(state["spec_ref"], state["profile_ref"]))
            code = asdict(version)
            if (code["spec_sha256"] != state["spec_sha256"]
                    or file_hash(private(code["source_ref"]), 262144) != code["code_sha256"]):
                raise ValueError("Code version does not match the frozen specification or hash")
            store.save_code(task_id, code)
        return code_state(code, 1)

    def code_state(code, attempt_no):
        return {"code_ref": str(private(code["source_ref"])), "code_sha256": code["code_sha256"],
                "generation_kind": code["generation_kind"], "attempt_no": attempt_no, "repair_error": ""}

    def execute(state):
        job = new_job(state["task_id"], Stage.CLEAN_C, state["input_sha256"],
                      spec_sha256=state["spec_sha256"], code_sha256=state["code_sha256"])
        source = store.root / "inputs" / f"{state['task_id']}.csv"
        stored = wait_job(job, {"data.csv": str(source), "spec.json": state["spec_ref"],
                               "clean.py": state["code_ref"]}, TaskState.EXECUTING,
                          state.get("attempt_no", 1))
        if stored["state"] != "finished":
            evidence = json.loads(stored["evidence_json"]) if stored["evidence_json"] else {}
            error = stored["error_code"]
            eligible = (error in ("RUNTIME_ERROR", "MISSING_OUTPUT")
                        and evidence.get("task_id") == state["task_id"]
                        and evidence.get("attempt_id") == stored["job_id"]
                        and evidence.get("code_sha256") == state["code_sha256"]
                        and evidence.get("spec_sha256") == state["spec_sha256"]
                        and evidence.get("input_sha256") == state["input_sha256"]
                        and evidence.get("timed_out") is False
                        and evidence.get("limit_reason") is None
                        and evidence.get("exit_code") == (1 if error == "RUNTIME_ERROR" else 0)
                        and evidence.get("runtime_category") ==
                        ("runtime_error" if error == "RUNTIME_ERROR" else "completed"))
            if eligible and state.get("attempt_no", 1) < 3:
                return {"repair_error": error, "attempt_id": stored["job_id"]}
            raise ValueError("Stage C failure is not eligible for automatic repair or the attempt budget is exhausted")
        result = successful_result(stored)
        candidate = result / "cleaned.csv"
        return {"candidate_ref": str(candidate), "candidate_sha256": file_hash(candidate, 12_000_000),
                "repair_error": "", "attempt_id": stored["job_id"]}

    def repair(state):
        store.check_active(state["task_id"])
        attempt_no = state.get("attempt_no", 1) + 1
        if attempt_no > 3 or state["repair_error"] not in ("RUNTIME_ERROR", "MISSING_OUTPUT"):
            raise ValueError("Repair is not authorized")
        code = store.code(state["task_id"], attempt_no)
        if code is None:
            previous = store.code(state["task_id"], attempt_no - 1)
            if previous is None:
                raise ValueError("Failed job's executed source record is missing")
            job = store.job(state["attempt_id"])
            evidence = json.loads(job["evidence_json"]) if job and job["evidence_json"] else {}
            request = json.loads(job["request_json"]) if job else {}
            if (not job or job["task_id"] != state["task_id"]
                    or job["stage"] != Stage.CLEAN_C.value or job["state"] != "failed"
                    or job["attempt_no"] != attempt_no - 1
                    or job["error_code"] != state["repair_error"]
                    or evidence.get("task_id") != state["task_id"]
                    or evidence.get("attempt_id") != state["attempt_id"]
                    or evidence.get("code_sha256") != request.get("code_sha256")
                    or evidence.get("spec_sha256") != state["spec_sha256"]
                    or evidence.get("input_sha256") != state["input_sha256"]):
                raise ValueError("Repair evidence does not match the source attempt")
            ref = evidence.get("executed_source_ref")
            digest = evidence.get("executed_source_sha256")
            if not isinstance(ref, str) or not isinstance(digest, str):
                raise ValueError("Failed job's executed source snapshot is missing")
            expected = store.root / "worker_jobs" / state["attempt_id"] / "input" / "clean.py"
            if Path(ref) != expected or Path(ref).is_symlink():
                raise ValueError("Executed source snapshot does not match the job")
            source = private(ref)
            if file_hash(source, 32768) != digest:
                raise ValueError("Executed source snapshot hash does not match")
            executed_source = ExecutedSource(str(source), digest, state["spec_ref"], state["profile_ref"])
            diagnostic = evidence.get("runtime_diagnostic") if state["repair_error"] == "RUNTIME_ERROR" else None
            store.repairing(state["task_id"])
            version = asyncio.run(generator.repair(
                CodeVersion(**previous), state["repair_error"], diagnostic, executed_source
            ))
            code = asdict(version)
            if code["spec_sha256"] != state["spec_sha256"] or code["code_sha256"] == digest:
                raise ValueError("Repair must produce new source for the same specification")
            store.save_code(state["task_id"], code, attempt_no)
        return code_state(code, attempt_no)

    def validate(state):
        task_id = state["task_id"]
        source = store.root / "inputs" / f"{task_id}.csv"
        candidate = private(state.get("candidate_ref", str(source)))
        candidate_digest = state.get("candidate_sha256", state["input_sha256"])
        candidate = freeze_candidate(store.root, task_id, candidate, candidate_digest)
        job = new_job(task_id, Stage.VERIFY_D, state["input_sha256"],
                      spec_sha256=state["spec_sha256"], candidate_sha256=candidate_digest)
        result = successful_result(wait_job(
            job, {"data.csv": str(source), "spec.json": state["spec_ref"],
                  "candidate.csv": str(candidate)}, TaskState.VALIDATING,
            state.get("attempt_no", 1)))
        report = read_json(result / "validation.json")
        expected_checks = {"input_hash", "same_columns", "same_row_count", "zero_change_bytes",
                           "exact_authorized_cells_and_row_positions"}
        if (report.get("schema_version") != "1.0" or report.get("validator_version") != "1.0"
                or report.get("verdict") != "passed" or report.get("mismatch_count") != 0
                or report.get("input_sha256") != state["input_sha256"]
                or report.get("spec_sha256") != state["spec_sha256"]
                or report.get("output_sha256") != candidate_digest
                or len(report.get("checks", [])) != len(expected_checks)
                or {c.get("id") for c in report["checks"]} != expected_checks
                or any(c.get("passed") is not True for c in report["checks"])
                or file_hash(candidate, 12_000_000) != candidate_digest):
            raise ValueError("Stage D evidence or candidate hash violates the contract")
        store.validated(task_id, report)
        return {"candidate_ref": str(candidate), "candidate_sha256": candidate_digest,
                "validation_ref": str(result / "validation.json")}

    def publish(state):
        Publisher(store).publish(state["task_id"])
        return {}

    # Initialize the state graph.
    graph = StateGraph(GraphState)
    
    # Add nodes.
    for name, node in (
        ("profile", profile),
        ("approval", approval),
        ("generate", generate),
        ("execute", execute),
        ("repair", repair),
        ("validate", validate),
        ("publish", publish),
    ):
        graph.add_node(name, node)
        
    # Define the graph edges.
    graph.add_edge(START, "profile")
    graph.add_edge("profile", "approval")
    graph.add_conditional_edges("approval", route, {"generate": "generate", "validate": "validate"})
    graph.add_edge("generate", "execute")
    graph.add_conditional_edges("execute", lambda state: "repair" if state.get("repair_error") else "validate",
                                {"repair": "repair", "validate": "validate"})
    graph.add_edge("repair", "execute")
    graph.add_edge("validate", "publish")
    graph.add_edge("publish", END)
    
    return graph.compile(checkpointer=checkpointer)
