"""Independent single-host Graph Runner consuming the database and outbox without importing Docker or CSV parsers."""

import argparse
import asyncio
import os
from pathlib import Path
import time

from backend.orchestration.graph import build_graph
from backend.orchestration.store import TERMINAL, WorkflowStore, process_lock


class GraphRunner:
    def __init__(self, root: Path, generator=None, explainer=None):
        self.store = WorkflowStore(root)
        self.generator = generator
        if explainer is None:
            from backend.llm.generator import LLMCodeGenerator
            from backend.llm.validation_explanation import ValidationExplainer
            if isinstance(generator, LLMCodeGenerator):
                explainer = ValidationExplainer(generator.gateway)
        self.explainer = explainer
        # Older checkpoints ended at VALIDATED; revalidate their stored D binding
        # rather than silently treating them as already published.
        with self.store.db() as db:
            for row in db.execute("SELECT task_id FROM tasks WHERE state IN ('VALIDATED','PUBLISHING')"):
                self.store.wake(db, row["task_id"], f"publish:{row['task_id']}")

    def run_once(self) -> bool:
        # Do not advance the same SQLite checkpoint thread from two local processes.
        with process_lock(self.store.root, "graph-runner"):
            event = self.store.next_event()
            if event is None:
                return False
            task_id = event["task_id"]
            if event["dedup_key"] == f"explain:{task_id}":
                self._explain(task_id)
                self.store.ack(event["id"])
                return True
            task = self.store.task(task_id)
            if task["state"] in TERMINAL:
                self.store.ack(event["id"])
                return True
            from langgraph.checkpoint.sqlite import SqliteSaver
            from langgraph.types import Command

            # Disable environment-controlled workflow tracing by default.
            os.environ["LANGSMITH_TRACING"] = "false"
            os.environ["LANGCHAIN_TRACING_V2"] = "false"
            with SqliteSaver.from_conn_string(str(self.store.root / "checkpoints.sqlite3")) as saver:
                graph = build_graph(saver, self.store, self.generator)
                config = {"configurable": {"thread_id": task_id}, "callbacks": []}
                snapshot = graph.get_state(config)
                interrupts = [item for pending in snapshot.tasks for item in pending.interrupts]
                invocation = None
                if not snapshot.values:
                    invocation = {"task_id": task_id, "input_sha256": task["input_sha256"]}
                elif interrupts:
                    waiting = interrupts[0].value
                    if waiting["kind"] == "job":
                        job = self.store.job(waiting["job_id"])
                        ready = job is not None and job["state"] not in ("queued", "running")
                    else:
                        ready = bool(self.store.flow(task_id)["spec_json"])
                    if not ready:
                        self.store.ack(event["id"])
                        return True
                    invocation = Command(resume=True)
                try:
                    if snapshot.next or invocation is not None:
                        graph.invoke(invocation, config)
                    elif task["state"] in ("VALIDATED", "PUBLISHING"):
                        from backend.artifacts.publication import Publisher
                        Publisher(self.store).publish(task_id)
                except Exception as exc:
                    # Do not put source data, column names, or model exception messages in public events.
                    from backend.artifacts.publication import ArtifactIntegrityError
                    code = str(exc) if isinstance(exc, ArtifactIntegrityError) else type(exc).__name__
                    self.store.fail(task_id, code)
            # Commit the checkpoint before acknowledgement; replay resumes the persisted cursor.
            self.store.ack(event["id"])
            return True

    def _explain(self, task_id):
        from backend.llm.client import ModelUnavailableError
        from backend.llm.validation_explanation import InvalidExplanation
        facts = self.store.claim_validation_explanation(task_id)
        if facts is None:
            return
        try:
            if self.explainer is None:
                raise ModelUnavailableError("未配置验证解释模型")
            result = asyncio.run(self.explainer.explain(task_id, facts))
        except Exception as exc:
            error = ("INVALID_MODEL_EXPLANATION" if isinstance(exc, InvalidExplanation)
                     else "EXPLANATION_MODEL_UNAVAILABLE")
            usage = None
            if isinstance(exc, (InvalidExplanation, ModelUnavailableError)):
                if exc.input_tokens is not None or exc.output_tokens is not None:
                    usage = {"input_tokens": exc.input_tokens, "output_tokens": exc.output_tokens}
            self.store.finish_validation_explanation(task_id, result=usage, error_code=error)
            return
        self.store.finish_validation_explanation(task_id, result=result)


def main() -> None:
    parser = argparse.ArgumentParser(description="独立持久化 Graph Runner（不持有 Docker socket）")
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("AI_AGENT_DATA_DIR", ".runtime")))
    parser.add_argument("--once", action="store_true", help="只处理一条 outbox 后退出")
    args = parser.parse_args()
    from backend.bootstrap import build_generator
    runner = GraphRunner(args.data_dir, build_generator(args.data_dir))
    if args.once:
        print(f"outbox processed={runner.run_once()}")
        return
    try:
        from backend.local_runtime import heartbeat
        while True:
            heartbeat(runner.store.root, "runner")
            if not runner.run_once():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
