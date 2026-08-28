const $ = (selector) => document.querySelector(selector);

const elements = {
  form: $("#run-form"),
  button: $("#run-button"),
  buttonLabel: $("#run-button-label"),
  formError: $("#form-error"),
  formNote: $("#form-note"),
  caseFile: $("#case-file"),
  targetTrials: $("#target-trials"),
  iterations: $("#iterations"),
  velocityMin: $("#velocity-min"),
  velocityMax: $("#velocity-max"),
  endpoint: $("#endpoint"),
  mcpDot: $("#mcp-dot"),
  mcpLabel: $("#mcp-label"),
  controlMode: $("#control-mode"),
  jobBadge: $("#job-badge"),
  completed: $("#completed-value"),
  target: $("#target-value"),
  progress: $("#progress-bar"),
  bestTemperature: $("#best-temperature"),
  bestVelocity: $("#best-velocity"),
  bestTrial: $("#best-trial"),
  updatedAt: $("#updated-at"),
  trialCount: $("#trial-count"),
  chart: $("#chart"),
  table: $("#trial-table"),
};

let initialized = false;
let submitting = false;

function setInputValues(defaults) {
  if (initialized) return;
  elements.caseFile.value = defaults.case_file || "";
  elements.targetTrials.value = defaults.target_trials;
  elements.iterations.value = defaults.iterations;
  elements.velocityMin.value = defaults.velocity_min;
  elements.velocityMax.value = defaults.velocity_max;
  elements.endpoint.value = defaults.endpoint;
  initialized = true;
}

function formatNumber(value, digits = 3) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : "—";
}

function renderStatus(state) {
  const online = state.mcp.status === "online";
  elements.mcpDot.className = `status-dot ${online ? "" : "offline"}`;
  elements.mcpLabel.textContent = online ? "Fluent MCP 已连接" : "Fluent MCP 未连接";
  elements.mcpLabel.title = state.mcp.detail;
  elements.controlMode.textContent = state.server.control_mode;

  const labels = { idle: "待命", running: "计算中", completed: "已完成", failed: "异常" };
  elements.jobBadge.textContent = labels[state.job.status] || state.job.status;
  elements.jobBadge.className = `job-badge ${state.job.status}`;

  const canRun = state.server.can_control && state.job.status !== "running";
  elements.button.disabled = !canRun || submitting;
  elements.buttonLabel.textContent = state.job.status === "running" ? "正在优化" : "开始 / 继续优化";
  if (!state.server.can_control) {
    elements.formNote.textContent = "当前是远程只读视图。请在运行服务的电脑上打开本机地址来启动计算。";
  } else if (state.job.status === "running") {
    elements.formNote.textContent = "Fluent 正在串行计算。页面可以关闭，服务会继续运行。";
  }
  if (state.job.error) elements.formError.textContent = state.job.error;
}

function renderMetrics(state) {
  const summary = state.summary || {};
  const completed = state.job.completed_trials || 0;
  const target = state.job.target_trials || state.defaults.target_trials || 0;
  elements.completed.textContent = completed;
  elements.target.textContent = ` / ${target}`;
  elements.progress.style.width = `${target ? Math.min(100, (completed / target) * 100) : 0}%`;

  elements.bestTemperature.textContent = formatNumber(summary.best_value, 3);
  const bestVelocity = summary.best_params?.cold_inlet_velocity;
  elements.bestVelocity.textContent = formatNumber(bestVelocity, 4);
  elements.bestTrial.textContent = summary.best_trial_number == null
    ? "尚无有效试验"
    : `来自试验 #${summary.best_trial_number}`;

  const updated = summary.updated_at ? new Date(summary.updated_at) : new Date();
  elements.updatedAt.textContent = `更新于 ${updated.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
}

function validTrials(trials) {
  return trials.filter((trial) => {
    const x = Number(trial.parameters?.cold_inlet_velocity);
    const y = Number(trial.objective_value);
    return trial.state === "COMPLETE" && Number.isFinite(x) && Number.isFinite(y);
  });
}

function svgElement(name, attrs = {}, text = "") {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  if (text) node.textContent = text;
  return node;
}

function renderChart(trials) {
  const points = validTrials(trials).sort((a, b) => Number(a.parameters.cold_inlet_velocity) - Number(b.parameters.cold_inlet_velocity));
  elements.trialCount.textContent = `${points.length} 个数据点`;
  elements.chart.replaceChildren();
  if (!points.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "完成一次试验后，这里会显示结果曲线";
    elements.chart.append(empty);
    return;
  }

  const width = 800;
  const height = 300;
  const margin = { top: 18, right: 18, bottom: 45, left: 62 };
  const xs = points.map((item) => Number(item.parameters.cold_inlet_velocity));
  const ys = points.map((item) => Number(item.objective_value));
  const xPadding = Math.max((Math.max(...xs) - Math.min(...xs)) * 0.08, 0.02);
  const yPadding = Math.max((Math.max(...ys) - Math.min(...ys)) * 0.15, 0.2);
  const xMin = Math.min(...xs) - xPadding;
  const xMax = Math.max(...xs) + xPadding;
  const yMin = Math.min(...ys) - yPadding;
  const yMax = Math.max(...ys) + yPadding;
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const sx = (value) => margin.left + ((value - xMin) / (xMax - xMin)) * plotWidth;
  const sy = (value) => margin.top + (1 - (value - yMin) / (yMax - yMin)) * plotHeight;

  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  for (let index = 0; index <= 4; index += 1) {
    const y = margin.top + (plotHeight / 4) * index;
    const value = yMax - ((yMax - yMin) / 4) * index;
    svg.append(svgElement("line", { x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "grid-line" }));
    svg.append(svgElement("text", { x: margin.left - 10, y: y + 3, "text-anchor": "end", class: "tick-label" }, value.toFixed(2)));
  }
  for (let index = 0; index <= 4; index += 1) {
    const x = margin.left + (plotWidth / 4) * index;
    const value = xMin + ((xMax - xMin) / 4) * index;
    svg.append(svgElement("text", { x, y: height - 22, "text-anchor": "middle", class: "tick-label" }, value.toFixed(2)));
  }
  svg.append(svgElement("text", { x: margin.left + plotWidth / 2, y: height - 3, "text-anchor": "middle", class: "axis-label" }, "冷入口速度 (m/s)"));
  svg.append(svgElement("text", { x: 13, y: margin.top + plotHeight / 2, transform: `rotate(-90 13 ${margin.top + plotHeight / 2})`, "text-anchor": "middle", class: "axis-label" }, "出口平均温度 (K)"));

  const pathData = points.map((item, index) => `${index ? "L" : "M"}${sx(Number(item.parameters.cold_inlet_velocity))},${sy(Number(item.objective_value))}`).join(" ");
  svg.append(svgElement("path", { d: pathData, class: "plot-line" }));
  const best = Math.min(...ys);
  points.forEach((item) => {
    const xValue = Number(item.parameters.cold_inlet_velocity);
    const yValue = Number(item.objective_value);
    const circle = svgElement("circle", { cx: sx(xValue), cy: sy(yValue), r: 5, class: `plot-point ${yValue === best ? "best" : ""}` });
    circle.append(svgElement("title", {}, `试验 #${item.trial_number} · ${xValue.toFixed(4)} m/s · ${yValue.toFixed(3)} K`));
    svg.append(circle);
  });
  elements.chart.append(svg);
}

function renderTable(trials) {
  elements.table.replaceChildren();
  if (!trials.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "table-empty";
    cell.textContent = "暂无试验记录";
    row.append(cell);
    elements.table.append(row);
    return;
  }
  [...trials].reverse().slice(0, 12).forEach((trial) => {
    const row = document.createElement("tr");
    const values = [
      `#${trial.trial_number}`,
      `${formatNumber(trial.parameters?.cold_inlet_velocity, 4)} m/s`,
      `${formatNumber(trial.objective_value, 3)} K`,
      trial.duration_seconds == null ? "—" : `${formatNumber(trial.duration_seconds, 1)} s`,
    ];
    values.forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    });
    const stateCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `state ${trial.state === "COMPLETE" ? "complete" : "other"}`;
    badge.textContent = trial.state === "COMPLETE" ? "有效" : (trial.state || "未知");
    stateCell.append(badge);
    row.append(stateCell);
    elements.table.append(row);
  });
}

async function refresh() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`状态接口返回 ${response.status}`);
    const state = await response.json();
    setInputValues(state.defaults);
    renderStatus(state);
    renderMetrics(state);
    renderChart(state.trials || []);
    renderTable(state.trials || []);
  } catch (error) {
    elements.mcpDot.className = "status-dot offline";
    elements.mcpLabel.textContent = "控制服务未连接";
    elements.formError.textContent = error.message;
  }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.formError.textContent = "";
  if (!elements.form.reportValidity()) return;
  const velocityMin = Number(elements.velocityMin.value);
  const velocityMax = Number(elements.velocityMax.value);
  if (velocityMin >= velocityMax) {
    elements.formError.textContent = "速度上限必须大于下限";
    elements.velocityMax.focus();
    return;
  }
  const payload = {
    case_file: elements.caseFile.value.trim(),
    target_trials: Number(elements.targetTrials.value),
    iterations: Number(elements.iterations.value),
    velocity_min: velocityMin,
    velocity_max: velocityMax,
    endpoint: elements.endpoint.value.trim(),
  };
  submitting = true;
  elements.button.disabled = true;
  elements.buttonLabel.textContent = "正在提交";
  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "启动失败");
    await refresh();
  } catch (error) {
    elements.formError.textContent = error.message;
  } finally {
    submitting = false;
    await refresh();
  }
});

refresh();
setInterval(refresh, 2000);
