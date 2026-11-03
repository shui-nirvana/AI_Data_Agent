"""独立单机 Graph Runner；只消费 DB/outbox，不导入 Docker 或 CSV 解析器。"""

import argparse
import os
from pathlib import Path
import time

from backend.orchestration.graph import build_graph
from backend.orchestration.store import TERMINAL, WorkflowStore, process_lock


class GraphRunner:
    def __init__(self, root: Path, generator=None):
        self.store = WorkflowStore(root)
        self.generator = generator
        # Older checkpoints ended at VALIDATED; revalidate their stored D binding
        # rather than silently treating them as already published.
        with self.store.db() as db:
            for row in db.execute("SELECT task_id FROM tasks WHERE state IN ('VALIDATED','PUBLISHING')"):
                self.store.wake(db, row["task_id"], f"publish:{row['task_id']}")

    def run_once(self) -> bool:
        # SQLite checkpoint 的同一 thread_id 不允许被两个本地进程同时推进。
        with process_lock(self.store.root, "graph-runner"):
            event = self.store.next_event()
            if event is None:
                return False
            task_id = event["task_id"]
            task = self.store.task(task_id)
            if task["state"] in TERMINAL:
                self.store.ack(event["id"])
                return True
            from langgraph.checkpoint.sqlite import SqliteSaver
            from langgraph.types import Command

            # 默认禁止工作流状态被环境中的 tracing 开关外发。
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
                    # 源数据、字段名、模型异常消息都不写入公开事件。
                    from backend.artifacts.publication import ArtifactIntegrityError
                    code = str(exc) if isinstance(exc, ArtifactIntegrityError) else type(exc).__name__
                    self.store.fail(task_id, code)
            # 先提交 checkpoint 再 ack。崩溃后重复事件只恢复已持久化的游标。
            self.store.ack(event["id"])
            return True


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
