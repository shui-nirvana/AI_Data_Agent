// All server/user strings enter the DOM as text, never interpolated HTML.
const $ = (id) => document.getElementById(id);
const terminal = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);
const names = {
  preserve: "Preserve",
  normalize_date: "Normalize dates",
  fill_null_with_literal: "Fill empty strings",
};
let taskId = new URL(location.href).searchParams.get("task");
let reviewVersion = null;
let cards = [];
let refreshing = false;
let eventTask = null;
let eventCursor = 0;
let eventLines = [];
let eventItems = [];
let explanationPending = false;

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-Demo-Token": $("token").value, ...options.headers },
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${response.status})`);
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
    `Task ${task.state}${task.state === "SUCCEEDED" ? ": independently validated and published." : ""}`;
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
    select.setAttribute("aria-label", `${issue.column} cleaning action`);
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
        label.textContent = "Fill value (replaces empty strings only, not spaces or NA)";
        const input = document.createElement("input");
        input.type = "text";
        input.maxLength = 1024;
        input.setAttribute("aria-label", `${issue.column} fill value`);
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

function renderExplanation(explanation) {
  explanationPending = ["pending", "started"].includes(explanation.status);
  $("explain").hidden = ["completed", "failed"].includes(explanation.status);
  $("explain").disabled = explanationPending;
  $("explanation-text").textContent = explanation.result?.text || "";
  const messages = {
    not_requested: "No explanation requested yet.",
    pending: "Explanation queued for the Graph Runner.",
    started: "The model is preparing the validation explanation.",
    completed: "Explanation generated; the D report remains authoritative.",
    failed: "Explanation unavailable; the D verdict and downloads are unaffected. This task will not retry automatically.",
  };
  $("explanation-status").textContent = messages[explanation.status] || "Unknown explanation status.";
}

async function refreshTask() {
  if (!taskId || !$("token").value || refreshing) return;
  refreshing = true;
  const current = taskId;
  try {
    const task = await (await request(taskPath())).json();
    if (current !== taskId) return;
    showTask(task);
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
      if (task.state !== "AWAITING_DECISIONS") $("review-form").hidden = true;
      const review = await (await request(taskPath("/review"))).json();
      if (current !== taskId) return;
      renderReview(review);
    } else {
      $("review-section").hidden = true;
    }
    if (eventTask !== taskId) {
      eventTask = taskId;
      eventCursor = 0;
      eventLines = [];
      eventItems = [];
    }
    const events = await (
      await request(taskPath(`/events?after=${eventCursor}`))
    ).json();
    eventItems.push(...events.events);
    eventLines.push(
      ...events.events.map((e) => `${e.sequence}  ${e.created_at}  ${e.kind}`),
    );
    if (events.events.length) eventCursor = events.events.at(-1).sequence;
    $("events").textContent = eventLines.join("\n");
    const evidence = await (await request(taskPath("/evidence"))).json();
    if (current !== taskId) return;
    $("evidence").textContent = JSON.stringify(evidence, null, 2);
    $("evidence-section").hidden = false;
    $("progress-list").replaceChildren();
    for (const stage of window.summarizeProgress(task, eventItems, evidence)) {
      const entry = document.createElement("li");
      const title = document.createElement("strong");
      title.textContent = `${stage.label}: ${stage.status}`;
      const detail = document.createElement("p");
      detail.textContent = stage.detail;
      entry.append(title, detail);
      $("progress-list").append(entry);
    }
    const canExplain = ["SUCCEEDED", "FAILED"].includes(task.state) && !!evidence.validation;
    $("explanation-section").hidden = !canExplain;
    explanationPending = false;
    if (canExplain) {
      const explanation = await (await request(taskPath("/validation-explanation"))).json();
      if (current !== taskId) return;
      renderExplanation(explanation);
    }
    $("downloads").replaceChildren();
    for (const artifact of evidence.artifacts) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `Download ${artifact.artifact_id}`;
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

async function withBusyButton(id, pendingText, action) {
  const button = $(id);
  if (button.disabled) return;
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = pendingText;
  try {
    await action();
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

$("file").addEventListener("change", () => {
  $("file-name").textContent = $("file").files[0]?.name || "No file selected";
});

$("upload-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if ($("upload").disabled) return;
  $("upload-error").textContent = "";
  for (const [id, message] of [
    ["token", "Enter the demo access token."],
    ["goal", "Enter a business goal."],
    ["file", "Choose a CSV file."],
  ]) {
    if (id === "file" ? !$("file").files.length : !$(id).value.trim()) {
      $("upload-error").textContent = message;
      $(id).focus();
      return;
    }
  }
  run(() => withBusyButton("upload", "Uploading...", async () => {
    const response = await request("/api/tasks", {
      method: "POST",
      headers: {
        "Content-Type": "text/csv",
        "X-Task-Goal": encodeURIComponent($("goal").value.trim()),
      },
      body: $("file").files[0],
    });
    reviewVersion = null;
    $("review-section").hidden = true;
    showTask(await response.json());
    await refreshTask();
  }));
});
$("refresh").addEventListener("click", () => run(refreshTask));
$("explain").addEventListener("click", () =>
  run(async () => {
    const current = taskId;
    $("explain").disabled = true;
    try {
      const explanation = await (
        await request(taskPath("/validation-explanation"), { method: "POST" })
      ).json();
      if (current === taskId) renderExplanation(explanation);
    } finally {
      if (current === taskId && !explanationPending) $("explain").disabled = false;
    }
  }),
);
$("restore").addEventListener("click", () =>
  run(async () => {
    const requestedTask = $("restore-id").value.trim();
    if (taskId !== requestedTask) {
      taskId = requestedTask;
      reviewVersion = null;
      $("review-section").hidden = true;
    }
    await refreshTask();
  }),
);
for (const action of ["start", "cancel"]) {
  $(action).addEventListener("click", () =>
    run(() => withBusyButton(action, action === "start" ? "Starting..." : "Cancelling...", async () => {
      await request(taskPath(`/${action}`), { method: "POST" });
      await refreshTask();
    })),
  );
}
$("review-form").addEventListener("submit", (event) => {
  event.preventDefault();
  $("review-error").textContent = "";
  if (!$("consent").checked) {
    $("review-error").textContent = "Confirm the decisions and model data-sharing notice.";
    $("consent").focus();
    return;
  }
  run(() => withBusyButton("approve", "Submitting approval...", async () => {
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
        throw new Error("Select at least one date input format.");
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
    await request(taskPath("/decisions"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_version: reviewVersion, decisions }),
    });
    await refreshTask();
  }));
});
if (taskId) $("restore-id").value = taskId;
setInterval(() => {
  if (!document.hidden && taskId && (!$("cancel").hidden || explanationPending)) run(refreshTask);
}, 2000);
