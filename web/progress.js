// Derive a conservative, human-readable summary from recorded events and evidence.
function summarizeProgress(task, events, evidence) {
  const states = events.map((event) => event.kind).filter(Boolean);
  const seen = new Set([...states, task.state]);
  const attempts = evidence.attempts || [];
  const codes = evidence.codes || [];
  const byStage = (name) => attempts.filter((item) => item.stage === name);
  const cleaning = byStage("CLEAN_C");
  const verifying = byStage("VERIFY_D");
  const laterThanProfile = [
    "AWAITING_DECISIONS",
    "SPEC_FROZEN",
    "GENERATING",
    "EXECUTING",
    "REPAIRING",
    "VALIDATING",
    "VALIDATED",
    "PUBLISHING",
    "SUCCEEDED",
  ].some((state) => seen.has(state));
  const approvalDone = [
    "SPEC_FROZEN",
    "GENERATING",
    "EXECUTING",
    "REPAIRING",
    "VALIDATING",
    "VALIDATED",
    "PUBLISHING",
    "SUCCEEDED",
  ].some((state) => seen.has(state));
  const preserveOnly =
    approvalDone && verifying.length > 0 && cleaning.length === 0;
  const lastClean = cleaning.at(-1);
  const report = evidence.validation;

  const steps = [
    {
      id: "upload",
      label: "上传登记",
      status: "已完成",
      detail: "原始文件已登记；尚未代表检测通过。",
    },
    {
      id: "profile",
      label: "固定检测 A",
      status: "等待中",
      detail: "检查 CSV 结构并运行固定工具。",
    },
    {
      id: "approval",
      label: "人工审批",
      status: "等待中",
      detail: "由你逐列选择并冻结规则。",
    },
    {
      id: "generate",
      label: "模型生成",
      status: "等待中",
      detail: "获准修改后生成完整源码。",
    },
    {
      id: "execute",
      label: "隔离执行 C",
      status: "等待中",
      detail: "在新容器执行；允许的实现故障可有限修复。",
    },
    {
      id: "validate",
      label: "独立验证 D",
      status: "等待中",
      detail: "独立核对原件、规范与候选结果。",
    },
    {
      id: "publish",
      label: "发布下载",
      status: "等待中",
      detail: "复核并登记产物后才可下载。",
    },
  ];
  const item = (id) => steps.find((step) => step.id === id);

  if (laterThanProfile) item("profile").status = "已完成";
  else if (task.state === "QUEUED") item("profile").status = "等待调度";
  else if (task.state === "PROFILING") item("profile").status = "进行中";
  if (approvalDone) item("approval").status = "已完成";
  else if (task.state === "AWAITING_DECISIONS")
    item("approval").status = "等待你的审批";

  if (preserveOnly) {
    item("generate").status = "已跳过";
    item("generate").detail = "全部保留，无需调用模型。";
    item("execute").status = "已跳过";
    item("execute").detail = "全部保留，无需运行清洗代码。";
  } else {
    if (codes.length) {
      item("generate").status = "已完成";
      item("generate").detail =
        `已记录 ${codes.length} 个代码版本，详细信息见下方证据。`;
    } else if (task.state === "GENERATING") item("generate").status = "进行中";
    if (lastClean && lastClean.state === "finished") {
      item("execute").status = "已完成";
      item("execute").detail =
        `已记录 ${cleaning.length} 次 C 尝试，独立验证仍需通过。`;
    } else if (task.state === "REPAIRING") {
      item("execute").status = "修复中";
      item("execute").detail =
        "正在按允许的错误类型生成新源码，下一次 C 使用新容器。";
    } else if (task.state === "EXECUTING") item("execute").status = "进行中";
  }
  if (report && report.verdict === "passed") {
    item("validate").status = "已完成";
    item("validate").detail =
      `D 通过；修改单元格 ${report.changed_cells}，不匹配 ${report.mismatch_count}。`;
  } else if (task.state === "VALIDATING") item("validate").status = "进行中";
  if (task.state === "SUCCEEDED") {
    item("publish").status = "已完成";
    item("publish").detail = "已登记可下载产物；下载时再次核对哈希。";
  } else if (task.state === "VALIDATED" || task.state === "PUBLISHING") {
    item("publish").status = "进行中";
  }

  if (task.state === "FAILED" || task.state === "CANCELLED") {
    if (!states.includes(task.state)) {
      steps.push({
        id: "diagnosis",
        label: "终态定位",
        status: "待确认",
        detail:
          "当前事件尚未加载到终态，请继续刷新并查看完整事件，暂不推断失败位置。",
      });
      return steps;
    }
    const location = {
      QUEUED: "profile",
      PROFILING: "profile",
      AWAITING_DECISIONS: "approval",
      SPEC_FROZEN: "generate",
      GENERATING: "generate",
      EXECUTING: "execute",
      REPAIRING: "execute",
      VALIDATING: "validate",
      VALIDATED: "publish",
      PUBLISHING: "publish",
    };
    const last = [...states].reverse().find((state) => location[state]);
    const error = [...states]
      .reverse()
      .find((state) => state.startsWith("ERROR:"));
    if (last) {
      const target = item(location[last]);
      target.status = task.state === "FAILED" ? "失败" : "已取消";
      target.detail =
        task.state === "FAILED"
          ? `最后记录阶段：${last}${error ? `；${error}` : ""}。${last === "SPEC_FROZEN" ? "具体后续步骤尚不能确定。" : ""}请查看下方事件与作业证据。`
          : `任务在 ${last} 后取消。`;
      if (last === "SPEC_FROZEN" && task.state === "FAILED")
        target.status = "后续步骤失败";
    } else {
      steps.push({
        id: "diagnosis",
        label: "终态定位",
        status: "待确认",
        detail: "现有状态不足以确定步骤，请查看事件与作业证据。",
      });
    }
  }
  return steps;
}

if (typeof module !== "undefined") module.exports = { summarizeProgress };
if (typeof window !== "undefined") window.summarizeProgress = summarizeProgress;
