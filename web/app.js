/**
 * 前端交互逻辑脚本。
 * 处理文件上传、任务状态查询以及界面更新。
 */

const form = document.querySelector("#upload-form");
const status = document.querySelector("#status");
const details = document.querySelector("#details");
const refresh = document.querySelector("#refresh");
let taskId = null;

/**
 * 更新界面以显示任务详情。
 * @param {Object} task 任务对象。
 */
function showTask(task) {
  taskId = task.task_id;
  status.textContent = `任务 ${task.state}。${
    task.state === "VALIDATED"
      ? "独立验证已通过，尚未发布。"
      : "编排启动及审批通过 API 操作，需要独立 Runner 和 worker。"
  }`;
  details.textContent = JSON.stringify(task, null, 2);
  refresh.hidden = false;
}

/**
 * 格式化并抛出 API 错误。
 * @param {Response} response Fetch 响应对象。
 */
async function showError(response) {
  const body = await response.json().catch(() => ({}));
  throw new Error(body.detail || `请求失败 (${response.status})`);
}

/**
 * 处理表单提交（任务创建与上传）。
 */
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = document.querySelector("#file").files[0];
  const token = document.querySelector("#token").value;
  const goal = document.querySelector("#goal").value.trim();

  if (!file || !goal || !token) return;

  status.textContent = "正在登记文件...";
  details.textContent = "";

  try {
    // 发送流式上传请求
    const response = await fetch("/api/tasks", {
      method: "POST",
      headers: {
        "Content-Type": "text/csv",
        "X-Demo-Token": token,
        // 对中文目标进行编码，防止 Header 传输乱码
        "X-Task-Goal": encodeURIComponent(goal),
      },
      body: file,
    });

    if (!response.ok) await showError(response);
    showTask(await response.json());
  } catch (error) {
    status.textContent = error.message;
  }
});

/**
 * 处理刷新按钮点击，获取最新任务状态。
 */
refresh.addEventListener("click", async () => {
  if (!taskId) return;
  try {
    const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, {
      headers: { "X-Demo-Token": document.querySelector("#token").value },
    });
    if (!response.ok) await showError(response);
    showTask(await response.json());
  } catch (error) {
    status.textContent = error.message;
  }
});
