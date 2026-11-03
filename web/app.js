// All server/user strings enter the DOM as text, never interpolated HTML.
const $ = (id) => document.getElementById(id);
const terminal = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);
const names = {
  preserve: "保留原样",
  normalize_date: "规范日期",
  fill_null_with_literal: "填充空字符串",
};
let taskId = new URL(location.href).searchParams.get("task");
let reviewVersion = null;
let cards = [];
let refreshing = false;
let eventTask = null;
let eventCursor = 0;
let eventLines = [];

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-Demo-Token": $("token").value, ...options.headers },
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `请求失败 (${response.status})`);
  }
  return response;
}

function taskPath(suffix = "") {
  return `/api/tasks/${encodeURIComponent(taskId)}${suffix}`;
}

function showTask(task) {
  taskId = task.task_id;
  const url = new URL(location.href);
  url.searchParams.set("task", taskId);
  history.replaceState(null, "", url);
  $("restore-id").value = taskId;
  $("status").textContent =
    `任务 ${task.state}${task.state === "SUCCEEDED" ? "：独立验证通过，已发布。" : ""}`;
  $("details").textContent = JSON.stringify(task, null, 2);
  $("refresh").hidden = false;
  $("start").hidden = task.state !== "UPLOADED";
  $("cancel").hidden = terminal.has(task.state);
}

function renderReview(review) {
  $("review-section").hidden = false;
  $("profile").textContent = JSON.stringify(review.profile, null, 2);
  $("review-form").hidden = review.spec_frozen;
  if (reviewVersion === review.task_version) return;
  reviewVersion = review.task_version;
  cards = [];
  $("decisions").replaceChildren();
  $("consent").checked = false;
  for (const issue of review.issues) {
    const field = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = issue.column;
    const facts = document.createElement("pre");
    facts.textContent = JSON.stringify(issue.findings, null, 2);
    const select = document.createElement("select");
    select.setAttribute("aria-label", `${issue.column} 清洗动作`);
    for (const action of issue.actions) {
      const option = document.createElement("option");
      option.value = action;
      option.textContent = names[action] || action;
      select.append(option);
    }
    select.value = "preserve";
    const params = document.createElement("div");
    const card = { issue, select, params, formats: [], literal: null };
    function updateParams() {
      params.replaceChildren();
      card.formats = [];
      card.literal = null;
      if (select.value === "normalize_date") {
        for (const format of ["YYYY-MM-DD", "YYYY/MM/DD", "DD-MM-YYYY"]) {
          const label = document.createElement("label");
          const input = document.createElement("input");
          input.type = "checkbox";
          input.value = format;
          input.checked =
            format !== "YYYY-MM-DD" &&
            (review.profile.date_formats_by_column[issue.column][format] || 0) >
              0;
          label.append(input, document.createTextNode(format));
          params.append(label);
          card.formats.push(input);
        }
      } else if (select.value === "fill_null_with_literal") {
        const label = document.createElement("label");
        label.textContent = "明确填充值（只替换空字符串，不替换空格或 NA）";
        const input = document.createElement("input");
        input.type = "text";
        input.maxLength = 1024;
        input.setAttribute("aria-label", `${issue.column} 填充值`);
        params.append(label, input);
        card.literal = input;
      }
    }
    select.addEventListener("change", updateParams);
    updateParams();
    field.append(legend, facts, select, params);
    $("decisions").append(field);
    cards.push(card);
  }
}

async function refreshTask() {
  if (!taskId || !$("token").value || refreshing) return;
  refreshing = true;
  const current = taskId;
  try {
    const task = await (await request(taskPath())).json();
    if (current !== taskId) return;
    showTask(task);
    $("review-section").hidden = true;
    if (
      [
        "AWAITING_DECISIONS",
        "SPEC_FROZEN",
        "GENERATING",
        "EXECUTING",
        "REPAIRING",
        "VALIDATING",
        "VALIDATED",
        "PUBLISHING",
        "SUCCEEDED",
      ].includes(task.state)
    ) {
      renderReview(await (await request(taskPath("/review"))).json());
    }
    if (eventTask !== taskId) {
      eventTask = taskId;
      eventCursor = 0;
      eventLines = [];
    }
    const events = await (
      await request(taskPath(`/events?after=${eventCursor}`))
    ).json();
    eventLines.push(
      ...events.events.map((e) => `${e.sequence}  ${e.created_at}  ${e.kind}`),
    );
    if (events.events.length) eventCursor = events.events.at(-1).sequence;
    $("events").textContent = eventLines.join("\n");
    const evidence = await (await request(taskPath("/evidence"))).json();
    $("evidence").textContent = JSON.stringify(evidence, null, 2);
    $("evidence-section").hidden = false;
    $("downloads").replaceChildren();
    for (const artifact of evidence.artifacts) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `下载 ${artifact.artifact_id}`;
      button.addEventListener("click", () =>
        run(async () => {
          const response = await request(
            taskPath(`/artifacts/${encodeURIComponent(artifact.artifact_id)}`),
          );
          const url = URL.createObjectURL(await response.blob());
          const link = document.createElement("a");
          link.href = url;
          link.download = {
            output: "cleaned.csv",
            validation: "validation.json",
            manifest: "manifest.json",
          }[artifact.artifact_id];
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }),
      );
      $("downloads").append(button);
    }
  } finally {
    refreshing = false;
  }
}

async function run(action) {
  try {
    await action();
  } catch (error) {
    $("status").textContent = error.message;
  }
}

$("upload-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    const response = await request("/api/tasks", {
      method: "POST",
      headers: {
        "Content-Type": "text/csv",
        "X-Task-Goal": encodeURIComponent($("goal").value.trim()),
      },
      body: $("file").files[0],
    });
    reviewVersion = null;
    showTask(await response.json());
    await refreshTask();
  });
});
$("refresh").addEventListener("click", () => run(refreshTask));
$("restore").addEventListener("click", () =>
  run(async () => {
    taskId = $("restore-id").value.trim();
    reviewVersion = null;
    await refreshTask();
  }),
);
for (const action of ["start", "cancel"]) {
  $(action).addEventListener("click", () =>
    run(async () => {
      await request(taskPath(`/${action}`), { method: "POST" });
      await refreshTask();
    }),
  );
}
$("review-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    const decisions = cards.map((card) => {
      const action = card.select.value;
      const params =
        action === "normalize_date"
          ? {
              input_formats: card.formats
                .filter((f) => f.checked)
                .map((f) => f.value),
            }
          : action === "fill_null_with_literal"
            ? { literal: card.literal.value }
            : {};
      if (action === "normalize_date" && !params.input_formats.length)
        throw new Error("请选择至少一种日期输入格式。");
      return {
        issue_id: card.issue.issue_id,
        column: card.issue.column,
        action,
        predicate_id:
          action === "preserve"
            ? "all_cells"
            : action === "normalize_date"
              ? "parseable_nonstandard_date"
              : "empty_string",
        params,
      };
    });
    $("approve").disabled = true;
    try {
      await request(taskPath("/decisions"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_version: reviewVersion, decisions }),
      });
      await refreshTask();
    } finally {
      $("approve").disabled = false;
    }
  });
});
if (taskId) $("restore-id").value = taskId;
setInterval(() => {
  if (!document.hidden && taskId && !$("cancel").hidden) run(refreshTask);
}, 2000);
