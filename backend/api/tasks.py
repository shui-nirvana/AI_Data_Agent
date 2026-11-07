"""Restricted raw-body upload API for the initial milestone. It handles uploads, queuing, approvals, and status; InputFiles manages paths and is not an execution sandbox.


"""

import hashlib
import hmac
import json
import os
import shutil
from dataclasses import asdict
from urllib.parse import unquote
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.artifacts.files import InputFiles
from backend.artifacts.publication import ArtifactIntegrityError, Publisher
from backend.domain.spec import Action, ActionParams, Decision
from backend.domain.store import TaskStore
from backend.orchestration.store import WorkflowConflict, WorkflowStore


class DecisionChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_id: str = Field(min_length=1, max_length=100)
    column: str = Field(min_length=1, max_length=32768)
    action: Action
    predicate_id: str
    params: ActionParams = ActionParams()


class ApprovalSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_version: int = Field(ge=1)
    decisions: tuple[DecisionChoice, ...] = Field(max_length=100)


def task_router(store: TaskStore, inputs: InputFiles, token: str, max_bytes: int) -> APIRouter:
    """Build task routes using the metadata store, input manager, demo token, and upload byte limit.







    """
    router = APIRouter(prefix="/api/tasks", tags=["tasks"])
    workflow = WorkflowStore(store.path.parent)
    owner = "demo" # The MVP uses one shared demo owner ID.

    def authenticate(provided: str | None) -> None:
        """Perform simple demo authentication."""
        if provided is None or not hmac.compare_digest(token, provided):
            raise HTTPException(401, "Invalid demo token")

    def owned(task_id: str):
        """Ensure the task exists and belongs to the current owner."""
        task = store.get(task_id, owner)
        if task is None:
            raise HTTPException(404, "Task not found")
        return task

    @router.post("")
    async def create_task(
        request: Request,
        x_demo_token: str | None = Header(None),
        x_task_goal: str | None = Header(None),
    ):
        """Create a task and stream the raw CSV request body to disk while hashing it.


        """
        authenticate(x_demo_token)
        min_free = int(os.environ.get("AI_AGENT_MIN_FREE_DISK_BYTES", "134217728"))
        max_active = int(os.environ.get("AI_AGENT_MAX_ACTIVE_TASKS", "20"))
        if min_free < 0 or max_active < 1:
            raise HTTPException(503, "INVALID_CAPACITY_CONFIGURATION")
        if shutil.disk_usage(store.path.parent).free < min_free + max_bytes:
            raise HTTPException(507, "DISK_CAPACITY")
        
        # Decode and validate the business goal.
        try:
            goal = unquote(x_task_goal or "", encoding="utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "Invalid goal encoding") from exc
            
        if not goal or len(goal) > 500:
            raise HTTPException(400, "The goal must contain 1 to 500 characters")
            
        # Validate the content type.
        content_type = request.headers.get("content-type", "").split(";")[0].strip()
        if content_type not in ("text/csv", "application/octet-stream"):
            raise HTTPException(415, "Upload a raw CSV request body")
            
        # Check the declared length.
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > max_bytes):
            raise HTTPException(413, "CSV exceeds the upload limit")

        task_id = str(uuid4())
        temporary = inputs.temporary_path(task_id)
        destination = inputs.input_path(task_id)
        digest = hashlib.sha256()
        size = 0
        
        try:
            # Stream into a temporary file before atomic publication.
            with temporary.open("xb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(413, "CSV exceeds the upload limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno()) # Flush the file to disk.
                
            if size == 0:
                raise HTTPException(400, "CSV is empty")
                
            # Move the completed file to the input directory and record it.
            os.replace(temporary, destination)
            try:
                task = store.create(task_id, owner, goal, digest.hexdigest(), size, max_active)
            except OverflowError as exc:
                raise HTTPException(429, "TASK_CAPACITY") from exc
        except BaseException:
            # Clean up files left by failed uploads.
            temporary.unlink(missing_ok=True)
            if store.get(task_id, owner) is None:
                destination.unlink(missing_ok=True)
            raise
            
        return JSONResponse(asdict(task), status_code=202)

    @router.get("/{task_id}")
    def get_task(task_id: str, x_demo_token: str | None = Header(None)):
        """Get task details."""
        authenticate(x_demo_token)
        return asdict(owned(task_id))

    @router.get("/{task_id}/events")
    def get_events(task_id: str, after: int = 0, x_demo_token: str | None = Header(None)):
        """Get the task event stream."""
        authenticate(x_demo_token)
        owned(task_id)
        if after < 0:
            raise HTTPException(400, "Cursor must be non-negative")
        return {"events": [asdict(event) for event in store.events(task_id, after)]}

    @router.post("/{task_id}/cancel")
    def cancel_task(task_id: str, x_demo_token: str | None = Header(None)):
        """Cancel an active task."""
        authenticate(x_demo_token)
        task = store.cancel(task_id, owner)
        if task is None:
            raise HTTPException(404, "Task not found")
        return asdict(task)

    @router.post("/{task_id}/start", status_code=202)
    def start(task_id: str, x_demo_token: str | None = Header(None)):
        """Write only to the durable outbox; HTTP does not invoke the graph or runtime. Start the runner and worker separately."""
        authenticate(x_demo_token)
        owned(task_id)
        try:
            workflow.start(task_id, owner)
        except WorkflowConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return asdict(owned(task_id))

    @router.get("/{task_id}/review")
    def review(task_id: str, x_demo_token: str | None = Header(None)):
        authenticate(x_demo_token)
        task = owned(task_id)
        try:
            flow = workflow.flow(task_id)
            if not flow["profile_json"]:
                raise WorkflowConflict("Detection A has not completed")
        except WorkflowConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        import json
        return {"task_version": task.version, "profile": json.loads(flow["profile_json"]),
                "issues": json.loads(flow["issues_json"]), "spec_frozen": bool(flow["spec_json"])}

    @router.post("/{task_id}/decisions")
    async def decisions(task_id: str, request: Request, x_demo_token: str | None = Header(None)):
        """Bind identity server-side and commit approval, specification, and resume outbox together; do not accept a client-provided findings set."""
        authenticate(x_demo_token)
        owned(task_id)
        try:
            flow = workflow.flow(task_id)
            if not flow["profile_json"]:
                raise WorkflowConflict("Detection A has not completed")
        except WorkflowConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 262144:
                raise HTTPException(413, "Approval request exceeds the size limit")
        try:
            submission = ApprovalSubmission.model_validate_json(bytes(content))
            choices = tuple(Decision(**choice.model_dump(), actor_id=owner,
                                     task_version=submission.task_version)
                            for choice in submission.decisions)
            spec = workflow.approve(task_id, owner, submission.task_version, choices)
        except WorkflowConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, "Invalid approval parameters or decision set") from exc
        return {"task": asdict(owned(task_id)), "spec_sha256": spec.spec_sha256}

    @router.get("/{task_id}/artifacts/{artifact_id}")
    def artifact(task_id: str, artifact_id: str, x_demo_token: str | None = Header(None)):
        """Return verified bytes, never reopen an unchecked path in FileResponse."""
        authenticate(x_demo_token)
        owned(task_id)
        try:
            data, name, media_type, sha256 = Publisher(workflow).download(task_id, artifact_id)
        except WorkflowConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ArtifactIntegrityError, OSError, ValueError) as exc:
            raise HTTPException(409, "ARTIFACT_INTEGRITY") from exc
        return Response(data, media_type=media_type, headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-SHA256": sha256, "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    @router.get("/{task_id}/validation-explanation")
    def get_validation_explanation(task_id: str, x_demo_token: str | None = Header(None)):
        authenticate(x_demo_token)
        owned(task_id)
        return workflow.validation_explanation(task_id)

    @router.post("/{task_id}/validation-explanation", status_code=202)
    def request_validation_explanation(task_id: str, x_demo_token: str | None = Header(None)):
        authenticate(x_demo_token)
        owned(task_id)
        try:
            workflow.request_validation_explanation(task_id, owner)
        except WorkflowConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return workflow.validation_explanation(task_id)

    @router.get("/{task_id}/evidence")
    def evidence(task_id: str, x_demo_token: str | None = Header(None)):
        """Expose explicit metadata only; exclude raw logs, source and host paths."""
        authenticate(x_demo_token)
        task = owned(task_id)
        with workflow.db() as db:
            jobs = db.execute("SELECT * FROM workflow_jobs WHERE task_id=? ORDER BY rowid",
                              (task_id,)).fetchall()
            codes = db.execute("SELECT attempt_no,code_json FROM workflow_codes WHERE task_id=? ORDER BY attempt_no",
                               (task_id,)).fetchall()
            flow = db.execute("SELECT validation_json FROM workflows WHERE task_id=?",
                              (task_id,)).fetchone()
            artifacts = db.execute("SELECT artifact_id,sha256,bytes,media_type FROM workflow_artifacts WHERE task_id=?",
                                   (task_id,)).fetchall()
        attempts = []
        for job in jobs:
            item = {k: job[k] for k in ("job_id", "stage", "attempt_no", "state")}
            recorded = json.loads(job["evidence_json"] or "{}")
            item.update({k: recorded.get(k) for k in (
                "exit_code", "timed_out", "limit_reason", "image_id",
                "elapsed_seconds", "runtime_category", "runtime_diagnostic",
                "executed_source_sha256")})
            item["error_code"] = job["error_code"]
            if recorded.get("validation"):
                item["validation"] = recorded["validation"]
            attempts.append(item)
        versions = []
        for row in codes:
            code = json.loads(row["code_json"])
            versions.append({"attempt_no": row["attempt_no"], **{k: code.get(k) for k in (
                "code_sha256", "generation_kind", "provider", "model",
                "input_tokens", "output_tokens", "request_id")}})
        report = json.loads(flow["validation_json"]) if flow and flow["validation_json"] else None
        if report is None:
            report = next((item["validation"] for item in reversed(attempts)
                           if item.get("validation")), None)
        return {"task_id": task_id, "state": task.state, "attempts": attempts,
                "codes": versions, "validation": report,
                "artifacts": [dict(row) for row in artifacts] if task.state == "SUCCEEDED" else [],
                "billing": "Tokens are recorded when provided; monetary cost is not inferred."}

    return router
