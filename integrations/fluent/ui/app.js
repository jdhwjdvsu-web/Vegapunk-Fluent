const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const elements = {
  form: $("#run-form"),
  runButton: $("#run-button"),
  runButtonLabel: $("#run-button-label"),
  formError: $("#form-error"),
  formNote: $("#form-note"),
  caseFile: $("#case-file"),
  targetTrials: $("#target-trials"),
  iterations: $("#iterations"),
  parameterSelect: $("#parameter-select"),
  rangeMin: $("#range-min"),
  rangeMax: $("#range-max"),
  rangeUnitMin: $("#range-unit-min"),
  rangeUnitMax: $("#range-unit-max"),
  parameterDescription: $("#parameter-description"),
  parameterSafetyHint: $("#parameter-safety-hint"),
  endpoint: $("#endpoint"),
  budgetTrials: $("#budget-trials"),
  mcpDot: $("#mcp-dot"),
  mcpLabel: $("#mcp-label"),
  controlMode: $("#control-mode"),
  jobBadge: $("#job-badge"),
  runningPulse: $("#running-pulse"),
  completed: $("#completed-value"),
  target: $("#target-value"),
  progress: $("#progress-bar"),
  progressCaption: $("#progress-caption"),
  bestTemperature: $("#best-temperature"),
  bestVelocity: $("#best-velocity"),
  bestTrial: $("#best-trial"),
  objectiveTrend: $("#objective-trend"),
  trialCount: $("#trial-count"),
  objectiveChart: $("#objective-chart"),
  parameterChart: $("#parameter-chart"),
  table: $("#trial-table"),
  modelName: $("#model-name"),
  modelPath: $("#model-path"),
  resultCount: $("#result-count"),
  resultBestTemp: $("#result-best-temp"),
  resultBestVelocity: $("#result-best-velocity"),
  resultParameterLabel: $("#result-parameter-label"),
  resultParameterUnit: $("#result-parameter-unit"),
  resultInsight: $("#result-insight"),
  directForm: $("#direct-run-form"),
  directParameterSelect: $("#direct-parameter-select"),
  directValue: $("#direct-value"),
  directUnit: $("#direct-unit"),
  directRangeHint: $("#direct-range-hint"),
  directButton: $("#direct-run-button"),
  directButtonLabel: $("#direct-run-button-label"),
  directError: $("#direct-run-error"),
  directResult: $("#direct-run-result"),
  directTemperature: $("#direct-temperature"),
  directMassError: $("#direct-mass-error"),
  directGate: $("#direct-gate"),
  directContour: $("#direct-contour"),
  directCaption: $("#direct-caption"),
  agentResult: $("#agent-result-message"),
  agentThread: $("#agent-thread"),
  agentForm: $("#agent-form"),
  agentQuestion: $("#agent-question"),
  clearConversation: $("#clear-conversation"),
  newTaskButton: $("#new-task-button"),
  currentTaskLabel: $("#current-task-label"),
  projectParameterLabel: $("#project-parameter-label"),
  monitorTitle: $("#monitor-title"),
  bestParameterUnit: $("#best-parameter-unit"),
  parameterChartSubtitle: $("#parameter-chart-subtitle"),
  trialParameterHeader: $("#trial-parameter-header"),
  footerTrial: $("#footer-trial"),
  footerFluentDot: $("#footer-fluent-dot"),
  footerFluent: $("#footer-fluent"),
  footerRunner: $("#footer-runner"),
  footerGate: $("#footer-gate"),
  updatedAt: $("#updated-at"),
};

let initialized = false;
let submitting = false;
let directSubmitting = false;
let lastState = null;
let parameterCatalog = [];
let selectedParameterKey = "cold_inlet_velocity";
let lastConversationRevision = null;

function switchView(name, openAdvanced = false) {
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  if (openAdvanced) $("#advanced-settings").open = true;
  $(".workbench").scrollTo({ top: 0, behavior: "smooth" });
}

$$('[data-view]').forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view, button.dataset.openAdvanced === "true"));
});

function parameterByKey(key) {
  return parameterCatalog.find((item) => item.key === key) || parameterCatalog[0] || null;
}

function populateParameterSelect(select, selectedKey) {
  select.replaceChildren();
  const categories = [...new Set(parameterCatalog.map((item) => item.category))];
  categories.forEach((category) => {
    const group = document.createElement("optgroup");
    group.label = category;
    parameterCatalog.filter((item) => item.category === category).forEach((item) => {
      const option = document.createElement("option");
      option.value = item.key;
      option.textContent = `${item.label} · ${item.zone}`;
      option.selected = item.key === selectedKey;
      group.append(option);
    });
    select.append(group);
  });
}

function applyParameterInputs(key, useRecommended = true) {
  const parameter = parameterByKey(key);
  if (!parameter) return;
  selectedParameterKey = parameter.key;
  elements.parameterSelect.value = parameter.key;
  elements.rangeMin.min = parameter.hard_min;
  elements.rangeMin.max = parameter.hard_max;
  elements.rangeMin.step = parameter.step;
  elements.rangeMax.min = parameter.hard_min;
  elements.rangeMax.max = parameter.hard_max;
  elements.rangeMax.step = parameter.step;
  if (useRecommended) {
    elements.rangeMin.value = parameter.recommended_min;
    elements.rangeMax.value = parameter.recommended_max;
  }
  elements.rangeUnitMin.textContent = parameter.unit;
  elements.rangeUnitMax.textContent = parameter.unit;
  elements.parameterDescription.textContent = parameter.description;
  elements.parameterSafetyHint.textContent = `允许 ${parameter.hard_min}–${parameter.hard_max} ${parameter.unit}；建议 ${parameter.recommended_min}–${parameter.recommended_max} ${parameter.unit}。`;
  elements.projectParameterLabel.textContent = parameter.label;
  elements.monitorTitle.textContent = `${parameter.label}优化`;
}

function applyDirectParameter(key, useDefault = true) {
  const parameter = parameterByKey(key);
  if (!parameter) return;
  elements.directParameterSelect.value = parameter.key;
  elements.directValue.min = parameter.hard_min;
  elements.directValue.max = parameter.hard_max;
  elements.directValue.step = parameter.step;
  if (useDefault) elements.directValue.value = parameter.default_value;
  elements.directUnit.textContent = parameter.unit;
  elements.directRangeHint.textContent = `允许 ${parameter.hard_min}–${parameter.hard_max} ${parameter.unit}；建议范围 ${parameter.recommended_min}–${parameter.recommended_max} ${parameter.unit}。`;
}

function setInputValues(defaults, parameters) {
  if (initialized) return;
  parameterCatalog = parameters || [];
  populateParameterSelect(elements.parameterSelect, defaults.parameter_key);
  populateParameterSelect(elements.directParameterSelect, defaults.direct_parameter_key);
  elements.caseFile.value = defaults.case_file || "";
  elements.targetTrials.value = defaults.target_trials;
  elements.iterations.value = defaults.iterations;
  elements.endpoint.value = defaults.endpoint;
  applyParameterInputs(defaults.parameter_key || parameterCatalog[0]?.key);
  elements.rangeMin.value = defaults.range_min;
  elements.rangeMax.value = defaults.range_max;
  applyDirectParameter(defaults.direct_parameter_key || defaults.parameter_key || parameterCatalog[0]?.key);
  elements.directValue.value = defaults.direct_value;
  elements.budgetTrials.textContent = defaults.target_trials;
  const path = defaults.case_file || "尚未配置 case 路径";
  elements.modelPath.textContent = path;
  elements.modelPath.title = path;
  elements.modelName.textContent = path.split(/[\\/]/).pop() || "Fluent case";
  initialized = true;
}

function formatNumber(value, digits = 3) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : "—";
}

function validTrials(trials) {
  return trials.filter((trial) => {
    const parameter = Number(trial.parameters?.[selectedParameterKey]);
    const objective = Number(trial.objective_value);
    return ["COMPLETE", "PASS"].includes(trial.state) && Number.isFinite(parameter) && Number.isFinite(objective);
  });
}

function renderStatus(state) {
  const online = state.mcp.status === "online";
  elements.mcpDot.className = `status-dot ${online ? "" : "offline"}`;
  elements.footerFluentDot.className = `status-dot ${online ? "" : "offline"}`;
  elements.mcpLabel.textContent = online ? "Fluent · 已连接" : "Fluent · 未连接";
  elements.mcpLabel.title = state.mcp.detail;
  elements.controlMode.textContent = state.server.control_mode;
  elements.footerFluent.textContent = online ? "Connected" : "Offline";

  const labels = { idle: "待命", running: "运行中", completed: "已完成", failed: "异常" };
  const directStatus = state.direct_run?.status || "idle";
  const busy = state.job.status === "running" || directStatus === "running";
  const failed = state.job.status === "failed" || directStatus === "failed";
  const visibleStatus = directStatus === "running" ? directStatus : state.job.status;
  elements.jobBadge.innerHTML = `<i></i>${labels[visibleStatus] || visibleStatus}`;
  elements.jobBadge.className = `job-badge ${visibleStatus}`;
  elements.runningPulse.classList.toggle("running", busy);
  elements.footerRunner.textContent = busy ? "Running" : failed ? "Failed" : "Idle";

  const canRun = state.server.can_control && !busy;
  elements.runButton.disabled = !canRun || submitting;
  elements.runButtonLabel.textContent = busy ? "Fluent 正在运行" : "批准并开始实验";
  elements.directButton.disabled = !canRun || directSubmitting;
  elements.newTaskButton.disabled = !state.server.can_control || busy;
  elements.clearConversation.disabled = !state.server.can_control;
  elements.directButtonLabel.textContent = directStatus === "running" ? "Fluent 计算中" : "计算并生成云图";
  if (!state.server.can_control) {
    elements.formNote.textContent = "当前是远程只读视图。请在运行服务的电脑上打开本机地址批准实验。";
  } else if (busy) {
    elements.formNote.textContent = "Fluent 正在串行求解。关闭页面不会中止当前实验。";
  }
  elements.formError.textContent = state.job.error || "";
}

function renderDirectRun(state) {
  const direct = state.direct_run || {};
  const result = direct.result;
  elements.directError.textContent = direct.error || "";
  if (!result) {
    elements.directResult.hidden = true;
    return;
  }
  const relativeError = Number(result.mass_balance_relative_error);
  const parameter = result.parameter || {};
  elements.directResult.hidden = false;
  elements.directTemperature.textContent = formatNumber(result.objective_value, 3);
  elements.directMassError.textContent = Number.isFinite(relativeError)
    ? `${(relativeError * 100).toExponential(3)}%`
    : "—";
  elements.directGate.textContent = result.mass_balance_gate_passed ? "PASS" : "REVIEW";
  elements.directGate.style.color = result.mass_balance_gate_passed ? "var(--green)" : "var(--red)";
  const cacheKey = encodeURIComponent(result.run_id || Date.now());
  elements.directContour.src = `${result.image_url}?v=${cacheKey}`;
  const classification = result.classification === "out_of_baseline_range" ? "扩展异常工况" : "基准范围工况";
  elements.directCaption.textContent = `Fluent 原生 Static Temperature 云图 · ${parameter.label || parameter.key || "参数"} ${formatNumber(parameter.display_value, 4)} ${parameter.unit || ""} · ${classification}`;
}

function renderMetrics(state) {
  const summary = state.summary || {};
  const trials = validTrials(state.trials || []);
  const parameter = parameterByKey(selectedParameterKey);
  const completed = state.job.completed_trials || trials.length;
  const target = state.job.target_trials || state.defaults.target_trials || 0;
  const percent = target ? Math.min(100, (completed / target) * 100) : 0;
  elements.completed.textContent = completed;
  elements.target.textContent = ` / ${target} trials`;
  elements.progress.style.width = `${percent}%`;
  elements.progressCaption.textContent = state.job.status === "running"
    ? `Trial ${Math.min(completed + 1, target)} 正在由 Fluent 求解`
    : completed >= target && target ? "目标试验已完成，可查看结果与审计产物" : "等待批准或继续实验";
  elements.footerTrial.textContent = `${completed} / ${target}`;
  elements.resultCount.textContent = completed;

  const bestValue = summary.best_value;
  const bestNativeValue = summary.best_params?.[selectedParameterKey];
  const bestParameterValue = Number(bestNativeValue) * Number(parameter?.native_to_display || 1);
  elements.bestTemperature.textContent = formatNumber(bestValue, 3);
  elements.bestVelocity.textContent = formatNumber(bestParameterValue, 4);
  elements.bestParameterUnit.textContent = parameter?.unit || "—";
  elements.bestTrial.textContent = summary.best_trial_number == null ? "尚无结果" : `Trial #${summary.best_trial_number}`;
  elements.resultBestTemp.textContent = formatNumber(bestValue, 3);
  elements.resultBestVelocity.textContent = formatNumber(bestParameterValue, 4);
  elements.resultParameterLabel.textContent = parameter?.label || "参数";
  elements.resultParameterUnit.textContent = parameter?.unit || "—";

  if (trials.length > 1) {
    const first = Number(trials[0].objective_value);
    const best = Math.min(...trials.map((item) => Number(item.objective_value)));
    const improvement = ((first - best) / first) * 100;
    elements.objectiveTrend.textContent = `↓ ${improvement.toFixed(2)}%`;
  } else {
    elements.objectiveTrend.textContent = "—";
  }
  elements.trialCount.textContent = `${trials.length} points`;

  const allPassed = trials.length > 0 && trials.every((trial) => trial.gates?.passed !== false);
  elements.footerGate.textContent = allPassed ? "All passed" : trials.length ? "Review" : "—";
  if (Number.isFinite(bestParameterValue)) {
    elements.resultInsight.textContent = `当前最优点位于 ${formatNumber(bestParameterValue, 4)} ${parameter?.unit || ""}。若需要更高精度，可围绕该点缩小参数范围并增加目标 Trial 数。`;
  }

  const updated = summary.updated_at ? new Date(summary.updated_at) : new Date();
  elements.updatedAt.textContent = `Last update ${updated.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
}

function svgElement(name, attrs = {}, text = "") {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  if (text) node.textContent = text;
  return node;
}

function chartScale(points, xValue, yValue) {
  const xs = points.map(xValue);
  const ys = points.map(yValue);
  const xSpan = Math.max(Math.max(...xs) - Math.min(...xs), 0.1);
  const ySpan = Math.max(Math.max(...ys) - Math.min(...ys), 0.2);
  return {
    xMin: Math.min(...xs) - xSpan * 0.08,
    xMax: Math.max(...xs) + xSpan * 0.08,
    yMin: Math.min(...ys) - ySpan * 0.12,
    yMax: Math.max(...ys) + ySpan * 0.12,
  };
}

function renderChart(container, points, options) {
  container.replaceChildren();
  if (!points.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = options.empty;
    container.append(empty);
    return;
  }
  const width = 520;
  const height = 210;
  const margin = { top: 14, right: 12, bottom: 31, left: 45 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const scale = chartScale(points, options.x, options.y);
  const sx = (value) => margin.left + ((value - scale.xMin) / (scale.xMax - scale.xMin)) * plotWidth;
  const sy = (value) => margin.top + (1 - (value - scale.yMin) / (scale.yMax - scale.yMin)) * plotHeight;
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });

  if (options.area) {
    const defs = svgElement("defs");
    const gradient = svgElement("linearGradient", { id: "objectiveFill", x1: 0, y1: 0, x2: 0, y2: 1 });
    gradient.append(svgElement("stop", { offset: "0%", "stop-color": "#2864dc", "stop-opacity": ".18" }));
    gradient.append(svgElement("stop", { offset: "100%", "stop-color": "#2864dc", "stop-opacity": "0" }));
    defs.append(gradient);
    svg.append(defs);
  }

  for (let index = 0; index <= 3; index += 1) {
    const y = margin.top + (plotHeight / 3) * index;
    const value = scale.yMax - ((scale.yMax - scale.yMin) / 3) * index;
    svg.append(svgElement("line", { x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "grid-line" }));
    svg.append(svgElement("text", { x: margin.left - 8, y: y + 3, "text-anchor": "end", class: "tick-label" }, value.toFixed(2)));
  }
  for (let index = 0; index <= 3; index += 1) {
    const x = margin.left + (plotWidth / 3) * index;
    const value = scale.xMin + ((scale.xMax - scale.xMin) / 3) * index;
    svg.append(svgElement("text", { x, y: height - 16, "text-anchor": "middle", class: "tick-label" }, options.xFormat(value)));
  }
  svg.append(svgElement("text", { x: margin.left + plotWidth / 2, y: height - 2, "text-anchor": "middle", class: "axis-label" }, options.xLabel));

  const ordered = [...points].sort((a, b) => options.x(a) - options.x(b));
  if (options.line) {
    const line = ordered.map((point, index) => `${index ? "L" : "M"}${sx(options.x(point))},${sy(options.y(point))}`).join(" ");
    if (options.area) {
      const area = `${line} L${sx(options.x(ordered.at(-1)))},${margin.top + plotHeight} L${sx(options.x(ordered[0]))},${margin.top + plotHeight} Z`;
      svg.append(svgElement("path", { d: area, class: "plot-area" }));
    }
    svg.append(svgElement("path", { d: line, class: "plot-line" }));
  }
  const best = Math.min(...points.map(options.y));
  ordered.forEach((point) => {
    const y = options.y(point);
    const circle = svgElement("circle", { cx: sx(options.x(point)), cy: sy(y), r: y === best ? 5.5 : 4.5, class: `plot-point ${y === best ? "best" : ""}` });
    circle.append(svgElement("title", {}, options.tooltip(point)));
    svg.append(circle);
  });
  container.append(svg);
}

function renderCharts(trials) {
  const points = validTrials(trials);
  const parameter = parameterByKey(selectedParameterKey);
  const displayValue = (trial) => Number(trial.parameters?.[selectedParameterKey]) * Number(parameter?.native_to_display || 1);
  renderChart(elements.objectiveChart, points, {
    x: (trial) => Number(trial.trial_number), y: (trial) => Number(trial.objective_value),
    xFormat: (value) => `#${Math.max(0, Math.round(value))}`, xLabel: "Trial", line: true, area: true,
    empty: "完成试验后显示目标函数历史",
    tooltip: (trial) => `Trial #${trial.trial_number} · ${formatNumber(trial.objective_value, 3)} K`,
  });
  renderChart(elements.parameterChart, points, {
    x: displayValue, y: (trial) => Number(trial.objective_value),
    xFormat: (value) => value.toFixed(2), xLabel: `${parameter?.label || "Parameter"} (${parameter?.unit || "—"})`, line: false, area: false,
    empty: "等待有效参数点",
    tooltip: (trial) => `${formatNumber(displayValue(trial), 4)} ${parameter?.unit || ""} · ${formatNumber(trial.objective_value, 3)} K`,
  });
  elements.parameterChartSubtitle.textContent = `${parameter?.label || "Parameter"} / outlet temperature`;
}

function renderTable(trials) {
  elements.table.replaceChildren();
  const parameter = parameterByKey(selectedParameterKey);
  elements.trialParameterHeader.textContent = parameter?.label || "Parameter";
  if (!trials.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.className = "table-empty";
    cell.textContent = "暂无试验记录";
    row.append(cell);
    elements.table.append(row);
    return;
  }
  [...trials].reverse().slice(0, 12).forEach((trial) => {
    const row = document.createElement("tr");
    const relativeError = Number(trial.gates?.metrics?.mass_balance_relative_error);
    const status = trial.gates?.passed ? "PASS" : trial.state === "PRUNED" ? "CONSTRAINT" : "DIVERGED";
    const statusClass = status === "PASS" ? "pass" : status === "CONSTRAINT" ? "warn" : "fail";
    const values = [
      `#${trial.trial_number}`,
      trial.sampling_phase === "startup_random" ? "Random" : "TPE",
      `${formatNumber(Number(trial.parameters?.[selectedParameterKey]) * Number(parameter?.native_to_display || 1), 4)} ${parameter?.unit || ""}`,
      `${formatNumber(trial.objective_value, 3)} K`,
      Number.isFinite(relativeError) ? `${(relativeError * 100).toExponential(2)}%` : "—",
      trial.duration_seconds == null ? "—" : `${formatNumber(trial.duration_seconds, 1)} s`,
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 1) cell.className = "sampling";
      row.append(cell);
    });
    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `gate-status ${statusClass}`;
    badge.textContent = status;
    statusCell.append(badge);
    row.append(statusCell);
    elements.table.append(row);
  });
}

function renderAgentState(state) {
  const trials = validTrials(state.trials || []);
  const best = state.summary?.best_value;
  const direct = state.direct_run || {};
  const paragraph = elements.agentResult.querySelector("p");
  const time = elements.agentResult.querySelector("time");
  if (direct.status === "running") {
    const parameter = parameterByKey(elements.directParameterSelect.value);
    paragraph.textContent = `单点工况已提交。MCP 正在修改${parameter?.label || "所选参数"}、运行 Fluent 并生成温度云图。`;
    time.textContent = "Fluent 单点计算中";
  } else if (direct.status === "completed" && direct.result) {
    paragraph.textContent = `单点计算完成：出口平均温度 ${formatNumber(direct.result.objective_value, 3)} K，温度云图已生成。`;
    time.textContent = "单点结果";
  } else if (state.job.status === "running") {
    paragraph.textContent = `Trial ${trials.length + 1} 正在运行。若发生传输不确定性，控制器会停止，不会盲目重试。`;
    time.textContent = "实验运行中";
  } else if (trials.length) {
    paragraph.textContent = `${trials.length} 个 Trial 已记录，当前最佳出口温度为 ${formatNumber(best, 3)} K。已完成的 Trial 均经过 Gate。`;
    time.textContent = "结果摘要";
  } else {
    paragraph.textContent = "实验尚未开始。请先审查参数范围、目标和 Gate，再批准执行。";
    time.textContent = "等待批准";
  }
}

function clearConversationView() {
  let node = elements.agentResult.nextElementSibling;
  while (node) {
    const next = node.nextElementSibling;
    node.remove();
    node = next;
  }
  elements.agentQuestion.value = "";
}

async function refresh() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`状态接口返回 ${response.status}`);
    const state = await response.json();
    lastState = state;
    setInputValues(state.defaults, state.parameters);
    elements.currentTaskLabel.textContent = `Mixing Elbow · ${state.task?.label || "参数研究"}`;
    const revision = state.task?.conversation_revision ?? 0;
    if (lastConversationRevision != null && revision !== lastConversationRevision) {
      clearConversationView();
    }
    lastConversationRevision = revision;
    renderStatus(state);
    renderMetrics(state);
    renderCharts(state.trials || []);
    renderTable(state.trials || []);
    renderDirectRun(state);
    renderAgentState(state);
  } catch (error) {
    elements.mcpDot.className = "status-dot offline";
    elements.mcpLabel.textContent = "控制服务未连接";
    elements.formError.textContent = error.message;
  }
}

elements.targetTrials.addEventListener("input", () => { elements.budgetTrials.textContent = elements.targetTrials.value || "—"; });
elements.parameterSelect.addEventListener("change", () => {
  applyParameterInputs(elements.parameterSelect.value);
  renderCharts(lastState?.trials || []);
  renderTable(lastState?.trials || []);
  renderMetrics(lastState || { summary: {}, trials: [], job: {}, defaults: {} });
});
elements.directParameterSelect.addEventListener("change", () => {
  applyDirectParameter(elements.directParameterSelect.value);
});

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.formError.textContent = "";
  if (!elements.form.reportValidity()) return;
  const rangeMin = Number(elements.rangeMin.value);
  const rangeMax = Number(elements.rangeMax.value);
  if (rangeMin >= rangeMax) {
    elements.formError.textContent = "参数上限必须大于下限";
    elements.rangeMax.focus();
    return;
  }
  const payload = {
    case_file: elements.caseFile.value.trim(), target_trials: Number(elements.targetTrials.value),
    iterations: Number(elements.iterations.value), parameter_key: elements.parameterSelect.value,
    range_min: rangeMin, range_max: rangeMax,
    endpoint: elements.endpoint.value.trim(),
  };
  submitting = true;
  elements.runButton.disabled = true;
  elements.runButtonLabel.textContent = "正在提交";
  try {
    const response = await fetch("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "启动失败");
    switchView("monitor");
    await refresh();
  } catch (error) {
    elements.formError.textContent = error.message;
  } finally {
    submitting = false;
    await refresh();
  }
});

async function submitDirectRun(value, fromAgent = false) {
  const numericValue = Number(value);
  const parameter = parameterByKey(elements.directParameterSelect.value);
  const min = Number(parameter?.hard_min);
  const max = Number(parameter?.hard_max);
  if (!Number.isFinite(numericValue) || numericValue < min || numericValue > max) {
    const message = `${parameter?.label || "参数"}必须在 ${min}–${max} ${parameter?.unit || ""} 之间。`;
    elements.directError.textContent = message;
    if (fromAgent) addAgentMessage(message);
    return false;
  }
  const payload = {
    case_file: elements.caseFile.value.trim(),
    parameter_key: parameter.key,
    value: numericValue,
    iterations: Number(elements.iterations.value),
    endpoint: elements.endpoint.value.trim(),
  };
  directSubmitting = true;
  elements.directButton.disabled = true;
  elements.directButtonLabel.textContent = "正在提交";
  elements.directError.textContent = "";
  try {
    const response = await fetch("/api/direct-runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "单点计算启动失败");
    elements.directValue.value = numericValue;
    if (fromAgent) addAgentMessage(`已提交${parameter.label} = ${numericValue} ${parameter.unit}。Fluent 将自动计算并返回温度云图。`);
    switchView("results");
    await refresh();
    return true;
  } catch (error) {
    elements.directError.textContent = error.message;
    if (fromAgent) addAgentMessage(`提交失败：${error.message}`);
    return false;
  } finally {
    directSubmitting = false;
    await refresh();
  }
}

elements.directForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.directForm.reportValidity()) return;
  await submitDirectRun(elements.directValue.value);
});

function addAgentMessage(text, user = false) {
  const article = document.createElement("article");
  article.className = `agent-message ${user ? "user" : ""}`;
  const avatar = document.createElement("span");
  avatar.textContent = user ? "U" : "A";
  const body = document.createElement("div");
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  const time = document.createElement("time");
  time.textContent = user ? "研究目标" : "规划建议";
  body.append(paragraph, time);
  article.append(avatar, body);
  elements.agentThread.append(article);
  elements.agentThread.scrollTop = elements.agentThread.scrollHeight;
}

function plannerReply(question) {
  const trials = validTrials(lastState?.trials || []);
  const direct = lastState?.direct_run?.result;
  if (/gate|拒绝|失败|发散/i.test(question)) {
    return "Gate 会先检查求解状态与有限值，再核对质量守恒误差。未通过的 Trial 不会进入最优值比较；完整原因保存在 trial_results 与 run_log.jsonl。";
  }
  if (/结果|温度|云图/i.test(question) && direct) {
    return `最近一次单点计算的出口平均温度为 ${formatNumber(direct.objective_value, 3)} K，Fluent 温度云图已显示在“结果与产物”。`;
  }
  if (/最优|结果|温度|trial/i.test(question) && trials.length) {
    return `当前共有 ${trials.length} 个有效 Trial，最佳出口温度为 ${formatNumber(lastState.summary?.best_value, 3)} K。你可以在“结果与产物”查看可审计文件。`;
  }
  return "研究目标已记录。当前版本会把它映射到受控 ExperimentSpec，并要求你在“实验方案”页确认变量、目标和 Gate 后再执行。";
}

function directParameterFromQuestion(question) {
  if (!/(设为|改为|改成|调整|提交|计算|运行|云图)/i.test(question)) return null;
  const aliases = [
    ["cold_inlet_velocity", /(cold[\s-]*inlet.*速度|冷入口.*速度)/i],
    ["hot_inlet_velocity", /(hot[\s-]*inlet.*速度|热入口.*速度)/i],
    ["cold_inlet_temperature", /(cold[\s-]*inlet.*温度|冷入口.*温度)/i],
    ["hot_inlet_temperature", /(hot[\s-]*inlet.*温度|热入口.*温度)/i],
    ["outlet_gauge_pressure", /(出口.*表压|出口.*压力|outlet.*pressure)/i],
    ["cold_inlet_turbulence_intensity", /(冷入口.*湍流强度|cold.*turbulence)/i],
    ["hot_inlet_turbulence_intensity", /(热入口.*湍流强度|hot.*turbulence)/i],
    ["cold_inlet_hydraulic_diameter", /(冷入口.*水力直径|cold.*hydraulic)/i],
    ["hot_inlet_hydraulic_diameter", /(热入口.*水力直径|hot.*hydraulic)/i],
    ["air_density", /(空气.*密度|air.*density)/i],
    ["air_viscosity", /(空气.*黏度|空气.*粘度|air.*viscosity)/i],
    ["air_specific_heat", /(空气.*比热|air.*specific.*heat)/i],
    ["air_thermal_conductivity", /(空气.*导热|air.*conductivity)/i],
  ];
  const selected = aliases.find(([, pattern]) => pattern.test(question));
  const match = question.match(/(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)/i);
  return selected && match ? { key: selected[0], value: Number(match[1]) } : null;
}

elements.agentForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = elements.agentQuestion.value.trim();
  if (!question) return;
  addAgentMessage(question, true);
  elements.agentQuestion.value = "";
  const directParameter = directParameterFromQuestion(question);
  if (directParameter) {
    applyDirectParameter(directParameter.key, false);
    await submitDirectRun(directParameter.value, true);
    return;
  }
  window.setTimeout(() => addAgentMessage(plannerReply(question)), 220);
});
elements.agentQuestion.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); elements.agentForm.requestSubmit(); }
});

elements.clearConversation.addEventListener("click", async () => {
  try {
    const response = await fetch("/api/agent/reset", { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "清空对话失败");
    clearConversationView();
    lastConversationRevision = result.conversation_revision;
  } catch (error) {
    elements.formError.textContent = error.message;
  }
});

elements.newTaskButton.addEventListener("click", async () => {
  elements.formError.textContent = "";
  try {
    const response = await fetch("/api/tasks/new", { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "新建任务失败");
    initialized = false;
    parameterCatalog = [];
    selectedParameterKey = "cold_inlet_velocity";
    lastState = null;
    clearConversationView();
    switchView("model");
    await refresh();
  } catch (error) {
    elements.formError.textContent = error.message;
  }
});

$("#accept-suggestion").addEventListener("click", () => switchView("plan"));
$("#generate-plan").addEventListener("click", () => {
  addAgentMessage("已根据当前模型和研究问题生成单参数温度优化方案。请检查速度范围、质量守恒 Gate 与试验预算。" );
  switchView("plan");
});

refresh();
window.setInterval(refresh, 2000);
