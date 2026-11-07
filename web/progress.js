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
      label: "Upload",
      status: "Complete",
      detail: "The original file is registered; detection has not necessarily passed.",
    },
    {
      id: "profile",
      label: "Fixed detection A",
      status: "Waiting",
      detail: "Check the CSV structure and run fixed tools.",
    },
    {
      id: "approval",
      label: "Human approval",
      status: "Waiting",
      detail: "Select and freeze rules for each column.",
    },
    {
      id: "generate",
      label: "Model generation",
      status: "Waiting",
      detail: "Generate complete source after modifications are approved.",
    },
    {
      id: "execute",
      label: "Isolated execution C",
      status: "Waiting",
      detail: "Run in a new container; eligible implementation failures may be repaired within limits.",
    },
    {
      id: "validate",
      label: "Independent validation D",
      status: "Waiting",
      detail: "Independently compare the original, specification and candidate.",
    },
    {
      id: "publish",
      label: "Publish downloads",
      status: "Waiting",
      detail: "Artifacts can be downloaded only after review and registration.",
    },
  ];
  const item = (id) => steps.find((step) => step.id === id);

  if (laterThanProfile) item("profile").status = "Complete";
  else if (task.state === "QUEUED") item("profile").status = "Queued";
  else if (task.state === "PROFILING") item("profile").status = "In progress";
  if (approvalDone) item("approval").status = "Complete";
  else if (task.state === "AWAITING_DECISIONS")
    item("approval").status = "Awaiting your approval";

  if (preserveOnly) {
    item("generate").status = "Skipped";
    item("generate").detail = "All columns are preserved; no model call is required.";
    item("execute").status = "Skipped";
    item("execute").detail = "All columns are preserved; no cleaning code is required.";
  } else {
    if (codes.length) {
      item("generate").status = "Complete";
      item("generate").detail =
        `${codes.length} code version(s) recorded; see the evidence below.`;
    } else if (task.state === "GENERATING") item("generate").status = "In progress";
    if (lastClean && lastClean.state === "finished") {
      item("execute").status = "Complete";
      item("execute").detail =
        `${cleaning.length} C attempt(s) recorded; independent validation is still required.`;
    } else if (task.state === "REPAIRING") {
      item("execute").status = "Repairing";
      item("execute").detail =
        "Generating new source for an eligible failure; the next C attempt uses a new container.";
    } else if (task.state === "EXECUTING") item("execute").status = "In progress";
  }
  if (report && report.verdict === "passed") {
    item("validate").status = "Complete";
    item("validate").detail =
      `D passed; ${report.changed_cells} changed cells, ${report.mismatch_count} mismatches.`;
  } else if (task.state === "VALIDATING") item("validate").status = "In progress";
  if (task.state === "SUCCEEDED") {
    item("publish").status = "Complete";
    item("publish").detail = "Downloadable artifacts are registered and checked again on download.";
  } else if (task.state === "VALIDATED" || task.state === "PUBLISHING") {
    item("publish").status = "In progress";
  }

  if (task.state === "FAILED" || task.state === "CANCELLED") {
    if (!states.includes(task.state)) {
      steps.push({
        id: "diagnosis",
        label: "Terminal state diagnosis",
        status: "Unconfirmed",
        detail:
          "The terminal event is not loaded yet; refresh and inspect all events before inferring the failure location.",
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
      target.status = task.state === "FAILED" ? "Failed" : "Cancelled";
      target.detail =
        task.state === "FAILED"
          ? `Last recorded stage: ${last}${error ? `; ${error}` : ""}. ${last === "SPEC_FROZEN" ? "The subsequent step is not yet known. " : ""}See events and job evidence below.`
          : `Task cancelled after ${last}.`;
      if (last === "SPEC_FROZEN" && task.state === "FAILED")
        target.status = "A subsequent step failed";
    } else {
      steps.push({
        id: "diagnosis",
        label: "Terminal state diagnosis",
        status: "Unconfirmed",
        detail: "The available state does not identify a step; inspect events and job evidence.",
      });
    }
  }
  return steps;
}

if (typeof module !== "undefined") module.exports = { summarizeProgress };
if (typeof window !== "undefined") window.summarizeProgress = summarizeProgress;
