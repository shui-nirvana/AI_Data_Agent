"""Local real A/C/D Docker smoke test; C uses hand-written test source, not model-generated code."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from backend.domain.spec import Action, ActionParams, Decision, freeze_spec
from backend.jobs.contracts import JobRequest, Stage
from workers.sandbox_manager.docker_backend import DockerExecutionBackend
from workers.sandbox_manager.runner import SandboxManager

TEST_CODE = """import csv
from datetime import datetime
with open('/input/data.csv', newline='', encoding='utf-8') as src:
    rows = list(csv.reader(src))
for row in rows[1:]:
    if row[1] and '/' in row[1]:
        row[1] = datetime.strptime(row[1], '%Y/%m/%d').strftime('%Y-%m-%d')
with open('/output/cleaned.csv', 'w', newline='', encoding='utf-8') as out:
    csv.writer(out).writerows(rows)
"""


def run_smoke(manifest: Path, output: Path) -> dict:
    configuration = json.loads(manifest.read_text(encoding="utf-8"))
    images = {Stage(key): value for key, value in configuration["images"].items()}
    # Use an independent directory for each round; never reuse prior task data or candidates.
    output = output / str(uuid4())
    output.mkdir(parents=True)
    original = output / "original.csv"
    with original.open("w", newline="", encoding="utf-8") as stream:
        csv.writer(stream).writerows([
            ["id", "date", "amount"], ["1", "2026/10/31", "-3"], ["2", "", "5"],
        ])
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    task_id = str(uuid4())
    backend = DockerExecutionBackend(output / "attempts", images)
    manager = SandboxManager(backend)
    records = []

    def run(job, sources):
        backend.prepare(job, sources)
        receipt = manager.submit(job)
        status = manager.status(receipt)
        records.append(json.loads(Path(status.result_ref).read_text(encoding="utf-8")))
        if status.state != "finished":
            raise RuntimeError(f"{job.stage.value} 烟测失败，参见私有 execution.json")
        return output / "attempts" / job.attempt_id / "result"

    profile_job = JobRequest(task_id, str(uuid4()), Stage.PROFILE_A, digest)
    results = run(profile_job, {"data.csv": original})
    profile = json.loads((results / "profile.json").read_text(encoding="utf-8"))
    issues = json.loads((results / "issues.json").read_text(encoding="utf-8"))
    decisions = tuple(
        Decision(issue_id=issue["issue_id"], column=issue["column"],
                 action=Action.NORMALIZE_DATE if issue["column"] == "date" else Action.PRESERVE,
                 predicate_id="parseable_nonstandard_date" if issue["column"] == "date" else "all_cells",
                 params=ActionParams(input_formats=("YYYY/MM/DD",))
                 if issue["column"] == "date" else ActionParams(),
                 actor_id="demo", task_version=1)
        for issue in issues
    )
    spec = freeze_spec(task_id=task_id, input_sha256=digest, owner_id="demo", task_version=1,
                       columns=tuple(profile["columns"]), decisions=decisions, issues=tuple(issues))
    spec_path, code_path = output / "spec.json", output / "clean.py"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    code_path.write_text(TEST_CODE, encoding="utf-8")
    clean_job = JobRequest(task_id, str(uuid4()), Stage.CLEAN_C, digest, spec.spec_sha256,
                           hashlib.sha256(code_path.read_bytes()).hexdigest())
    candidate = run(clean_job, {"data.csv": original, "spec.json": spec_path,
                               "clean.py": code_path}) / "cleaned.csv"
    candidate_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    verify_job = JobRequest(task_id, str(uuid4()), Stage.VERIFY_D, digest,
                           spec.spec_sha256, candidate_sha256=candidate_digest)
    validation_path = run(verify_job, {"data.csv": original, "spec.json": spec_path,
                                     "candidate.csv": candidate}) / "validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if (validation["verdict"] != "passed" or validation["output_sha256"] != candidate_digest
            or hashlib.sha256(original.read_bytes()).hexdigest() != digest):
        raise RuntimeError("D 报告、候选或原件哈希不一致")
    report = {"kind": "test_source_not_llm", "deployment_platform": configuration["deployment_platform"],
              "target_platform_verified": False, "attempts": records, "validation": validation}
    (output / "smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="运行真实 Docker A/C/D 测试源码烟测")
    parser.add_argument("--images", type=Path, default=Path(".runtime/docker/images.json"))
    parser.add_argument("--output", type=Path, default=Path(".runtime/docker/smoke"))
    args = parser.parse_args()
    report = run_smoke(args.images, args.output)
    print(f"A/C/D 烟测通过，D 改动数={report['validation']['changed_cells']}；不是 LLM 演示。")


if __name__ == "__main__":
    main()
