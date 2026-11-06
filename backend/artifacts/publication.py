"""Bounded byte storage and publication; never parse CSV or execute model code."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

from backend.domain.models import TaskState
from backend.domain.spec import Action, FrozenSpec
from backend.orchestration.store import WorkflowConflict

CHECKS = {"input_hash", "same_columns", "same_row_count", "zero_change_bytes",
          "exact_authorized_cells_and_row_positions"}
LIMIT = 12_000_000
ARTIFACTS = {"output": ("cleaned.csv", "text/csv", LIMIT),
             "validation": ("validation.json", "application/json", 1_000_000),
             "manifest": ("manifest.json", "application/json", 1_000_000)}


class ArtifactIntegrityError(ValueError):
    """Public fixed error code, with no sensitive file contents."""


def redirected(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_private(root: Path, path: Path, limit: int) -> bytes:
    # Reject links in every component, not merely at the final filename.
    root = root.resolve()
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(root):
        raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
    for part in (path, *path.parents):
        if part == root:
            break
        if redirected(part):
            raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
    return data


def write_atomic(root: Path, path: Path, data: bytes):
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(root.resolve()):
        raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
    for part in (path, *path.parents):
        if part == root.resolve():
            break
        if redirected(part):
            raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or redirected(path):
        if read_private(root, path, len(data)) != data:
            raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
        return
    # Check directories before writing, including redirects through parent links.
    if path.parent.resolve() != Path(os.path.abspath(path.parent)):
        raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
    temporary = path.parent / f".{uuid4()}.part"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o444)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def freeze_candidate(root: Path, task_id: str, candidate: Path, expected: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
    data = read_private(root, candidate, LIMIT)
    if digest(data) != expected:
        raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
    target = root / "snapshots" / str(UUID(task_id)) / f"{expected}.csv"
    write_atomic(root, target, data)
    return target


def check_report(report: dict, spec: FrozenSpec, output_hash: str):
    keys = {"schema_version", "validator_version", "verdict", "mismatch_count",
            "input_sha256", "spec_sha256", "output_sha256", "changed_cells", "checks"}
    if not isinstance(report, dict) or set(report) != keys:
        raise ArtifactIntegrityError("VALIDATION_REJECTED")
    checks = report.get("checks", [])
    if (report.get("schema_version") != "1.0" or report.get("validator_version") != "1.0"
            or report.get("verdict") != "passed" or report.get("mismatch_count") != 0
            or report.get("input_sha256") != spec.input_sha256
            or report.get("spec_sha256") != spec.spec_sha256
            or report.get("output_sha256") != output_hash
            or type(report.get("changed_cells")) is not int or report["changed_cells"] < 0
            or not isinstance(checks, list) or len(checks) != len(CHECKS)
            or any(not isinstance(c, dict) or c.get("passed") is not True for c in checks)
            or {c.get("id") for c in checks} != CHECKS):
        raise ArtifactIntegrityError("VALIDATION_REJECTED")


class Publisher:
    def __init__(self, store):
        self.store, self.root = store, store.root

    def publish(self, task_id: str):
        """Replay-safe; DB lock serializes cancellation and publication registration.

        Files made before a crash remain private and are verified on replay.
        No download is possible until the registration transaction commits.
        """
        task_id = str(UUID(task_id))
        with self.store.db() as db:
            task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise WorkflowConflict("任务不存在")
            if task["state"] == TaskState.SUCCEEDED:
                return
            if task["state"] not in (TaskState.VALIDATED, TaskState.PUBLISHING):
                raise WorkflowConflict("任务未经验证或已经取消")
            flow = db.execute("SELECT * FROM workflows WHERE task_id=?", (task_id,)).fetchone()
            spec = FrozenSpec.model_validate_json(flow["spec_json"])
            jobs = db.execute("SELECT * FROM workflow_jobs WHERE task_id=? ORDER BY rowid",
                              (task_id,)).fetchall()
            validations = [j for j in jobs if j["stage"] == "VERIFY_D"]
            if not validations or validations[-1]["state"] != "finished":
                raise ArtifactIntegrityError("VALIDATION_REJECTED")
            job = validations[-1]
            request, sources = json.loads(job["request_json"]), json.loads(job["sources_json"])
            output = read_private(self.root, Path(sources["candidate.csv"]), LIMIT)
            output_hash = digest(output)
            report = json.loads(read_private(self.root, Path(job["result_dir"]) / "validation.json",
                                             1_000_000))
            check_report(report, spec, output_hash)
            if (task["input_sha256"] != spec.input_sha256 or str(spec.task_id) != task_id
                    or request["spec_sha256"] != spec.spec_sha256
                    or request["input_sha256"] != spec.input_sha256
                    or request["candidate_sha256"] != output_hash
                    or digest(read_private(self.root, Path(sources["data.csv"]), LIMIT)) != spec.input_sha256
                    or FrozenSpec.model_validate_json(read_private(
                        self.root, Path(sources["spec.json"]), 262144)) != spec
                    or json.loads(flow["validation_json"]) != report):
                raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
            codes = db.execute("SELECT attempt_no,code_json FROM workflow_codes WHERE task_id=? ORDER BY attempt_no",
                               (task_id,)).fetchall()
            if not codes and flow["code_json"]:
                codes = [{"attempt_no": 1, "code_json": flow["code_json"]}]
            changes = any(d.action != Action.PRESERVE for d in spec.decisions)
            if changes:
                cleaning = [j for j in jobs if j["stage"] == "CLEAN_C"]
                if not cleaning or cleaning[-1]["state"] != "finished":
                    raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
                c_job = cleaning[-1]
                c_request = json.loads(c_job["request_json"])
                code = next((json.loads(c["code_json"]) for c in codes
                             if c["attempt_no"] == c_job["attempt_no"]), None)
                if code is None and c_job["attempt_no"] == 1 and flow["code_json"]:
                    code = json.loads(flow["code_json"])
                if (code is None or c_job["attempt_no"] != job["attempt_no"]
                        or c_request["spec_sha256"] != spec.spec_sha256
                        or c_request["input_sha256"] != spec.input_sha256
                        or c_request["code_sha256"] != code["code_sha256"]
                        or code["spec_sha256"] != spec.spec_sha256
                        or digest(read_private(self.root, Path(c_job["result_dir"]) /
                                               "cleaned.csv", LIMIT)) != output_hash):
                    raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
            elif output_hash != spec.input_sha256:
                raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
            code_versions = []
            for row in codes:
                code = json.loads(row["code_json"])
                code_versions.append({k: code.get(k) for k in (
                    "code_sha256", "generation_kind", "provider", "model", "request_id",
                    "prompt_sha256", "input_tokens", "output_tokens")})
                code_versions[-1]["attempt_no"] = row["attempt_no"]
            attempts = []
            for j in jobs:
                req = json.loads(j["request_json"])
                evidence = json.loads(j["evidence_json"] or "{}")
                attempts.append({"attempt_id": j["job_id"], "stage": j["stage"],
                                 "attempt_no": j["attempt_no"], "state": j["state"],
                                 "code_sha256": req.get("code_sha256"),
                                 **{k: evidence.get(k) for k in (
                                     "exit_code", "timed_out", "limit_reason", "image_id",
                                     "elapsed_seconds", "runtime_category", "runtime_diagnostic",
                                     "executed_source_sha256")}})
            report_bytes = json.dumps(report, sort_keys=True, ensure_ascii=True).encode()
            manifest = {"schema_version": "1.0", "task_id": task_id,
                        "input_sha256": spec.input_sha256, "spec_sha256": spec.spec_sha256,
                        "output_sha256": output_hash, "validation_sha256": digest(report_bytes),
                        "validation_attempt_id": job["job_id"], "attempts": attempts,
                        "codes": code_versions, "unmodified": not changes,
                        "warning": "CSV may contain spreadsheet formulas; business decisions are human-approved."}
            payloads = {"output": output, "validation": report_bytes,
                        "manifest": json.dumps(manifest, sort_keys=True, ensure_ascii=True).encode()}
            self.store.transition(db, task_id, TaskState.PUBLISHING)
            for artifact_id, data in payloads.items():
                name, media_type, _ = ARTIFACTS[artifact_id]
                path = self.root / "published" / task_id / name
                write_atomic(self.root, path, data)
                db.execute("""INSERT OR IGNORE INTO workflow_artifacts
                    (task_id,artifact_id,sha256,bytes,media_type) VALUES(?,?,?,?,?)""",
                           (task_id, artifact_id, digest(data), len(data), media_type))
            self.store.transition(db, task_id, TaskState.SUCCEEDED)

    def download(self, task_id: str, artifact_id: str):
        if artifact_id not in ARTIFACTS:
            raise WorkflowConflict("产物不存在")
        task_id = str(UUID(task_id))
        with self.store.db() as db:
            task = db.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            row = db.execute("SELECT * FROM workflow_artifacts WHERE task_id=? AND artifact_id=?",
                             (task_id, artifact_id)).fetchone()
            if task is None or task["state"] != TaskState.SUCCEEDED or row is None:
                raise WorkflowConflict("产物尚未发布")
        name, media_type, limit = ARTIFACTS[artifact_id]
        data = read_private(self.root, self.root / "published" / task_id / name, limit)
        if digest(data) != row["sha256"] or len(data) != row["bytes"]:
            raise ArtifactIntegrityError("ARTIFACT_INTEGRITY")
        return data, name, media_type, row["sha256"]
