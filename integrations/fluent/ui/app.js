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
  parameterRows: [],
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
  directParameterRows: [],
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
let selectedParameterKeys = [];
let lastConversationRevision = null;
let renderedModelId = null;


let maxParameters = 5;
let scanSubmitting = false;
let libraryEntries = [];

function rebuildRows(parameters, directParameters) {
  $("#parameter-list").replaceChildren();
  $("#direct-parameter-list").replaceChildren();
  elements.parameterRows = [];
  elements.directParameterRows = [];
  selectedParameterKeys = [];
  parameters.forEach((item) => addParameterRow(false, item));
  directParameters.forEach((item) => addParameterRow(true, item));
  updateAddButtons();
}

function updateAddButtons() {
  $("#add-parameter").disabled = elements.parameterRows.length >= Math.min(maxParameters, parameterCatalog.length);
  $("#add-direct-parameter").disabled = elements.directParameterRows.length >= Math.min(maxParameters, parameterCatalog.length);
}

function rowValues(direct) {
  return (direct ? elements.directParameterRows : elements.parameterRows).map((row) => direct
    ? { parameter_key: row.select.value, value: row.value.value }
    : { parameter_key: row.select.value, range_min: row.rangeMin.value, range_max: row.rangeMax.value });
}

function addParameterRow(direct, selected = null) {
  const rows = direct ? elements.directParameterRows : elements.parameterRows;
  if (rows.length >= maxParameters) return;
  const used = new Set(rows.map((row) => row.select.value));
  const parameter = selected ? parameterByKey(selected.parameter_key) : parameterCatalog.find((item) => !used.has(item.key));
  if (!parameter) return;
  const card = document.createElement("div");
  card.className = direct ? "direct-parameter-row" : "parameter-row parameter-picker";
  card.innerHTML = direct
    ? '<b class="variable-chip"></b><label><span>修改参数</span><select required></select></label><label><span>参数值</span><div><input type="number" required /><b class="unit"></b></div></label><small class="hint"></small><button type="button" class="remove-parameter">× 删除</button>'
    : '<span class="variable-chip"></span><label class="parameter-select-wrap"><span>修改参数</span><select required></select><small class="description"></small></label><label><span>MIN · <b class="min-unit"></b></span><input class="range-min" type="number" required /></label><span class="range-arrow">→</span><label><span>MAX · <b class="max-unit"></b></span><input class="range-max" type="number" required /></label><p class="parameter-safety-hint"></p><button type="button" class="remove-parameter">× 删除</button>';
  card.querySelector(".variable-chip").textContent = `变量 ${rows.length + 1}`;
  const select = card.querySelector("select");
  select.setAttribute("aria-label", `${direct ? "审计" : "优化"}变量 ${rows.length + 1}`);
  const row = direct
    ? { card, select, value: card.querySelector("input"), unit: card.querySelector(".unit"), rangeHint: card.querySelector(".hint") }
    : { card, select, rangeMin: card.querySelector(".range-min"), rangeMax: card.querySelector(".range-max"),
        rangeUnitMin: card.querySelector(".min-unit"), rangeUnitMax: card.querySelector(".max-unit"),
        description: card.querySelector(".description"), safetyHint: card.querySelector(".parameter-safety-hint") };
  rows.push(row);
  $(direct ? "#direct-parameter-list" : "#parameter-list").append(card);
  populateParameterSelect(select, parameter.key);
  const index = rows.length - 1;
  if (direct) {
    applyDirectParameter(index, parameter.key);
    if (selected) row.value.value = selected.value ?? "";
  } else {
    applyParameterInputs(index, parameter.key);
    if (selected) {
      row.rangeMin.value = selected.range_min ?? "";
      row.rangeMax.value = selected.range_max ?? "";
    }
  }
  select.addEventListener("change", () => {
    if (direct) applyDirectParameter(rows.indexOf(row), select.value);
    else applyParameterInputs(rows.indexOf(row), select.value);
  });
  card.querySelector(".remove-parameter").addEventListener("click", () => {
    if (rows.length <= 1) return;
    const plan = rowValues(false), single = rowValues(true);
    (direct ? single : plan).splice(rows.indexOf(row), 1);
    rebuildRows(plan, single);
  });
  updateAddButtons();
}

$("#add-parameter").addEventListener("click", () => addParameterRow(false));
$("#add-direct-parameter").addEventListener("click", () => addParameterRow(true));

function renderModelState(state) {
  const model = state.model || {};
  maxParameters = model.max_parameters || 5;
  const profile = model.profile;
  const scanning = model.status === "scanning" || scanSubmitting;
  $("#model-scan-status").textContent = scanning ? "正在分析 Fluent 模型：边界 / 材料 / 物理场 / 报告……"
    : profile ? `✓ 已解析 · ${state.parameters.length} 个候选参数 · Fluent ${profile.fluent_version} · ${profile.signature.warnings.length} 条扫描提示`
    : "尚未解析，请选择 Case 并分析";
  $("#contract-status").textContent = model.execution_ready
    ? "沿用 SHA256 一致的已验收模板：出口平均温度 MIN + 质量守恒 Gate。"
    : "待配置：此模型的 Objective / Gate 未确认，当前禁止提交计算。";
  $("#model-check").textContent = profile ? "✓" : "○";
  $(".project-card strong").textContent = profile?.name || "Fluent 模型";
  $(".project-icon").textContent = "CFD";
  $("#analyze-model").disabled = scanning || !state.server.can_control || state.job.status === "running" || state.direct_run.status === "running";
  $("#force-analyze").disabled = $("#analyze-model").disabled;
  $("#save-ranges").disabled = !profile || scanning || !state.server.can_control;
  if (!$("#model-case-path").value && elements.caseFile.value) $("#model-case-path").value = elements.caseFile.value;
}

async function loadModelLibrary() {
  const response = await fetch("/api/models");
  if (!response.ok) return;
  libraryEntries = (await response.json()).models;
  const select = $("#model-library");
  select.replaceChildren(new Option("请选择历史模型", ""));
  libraryEntries.forEach((item, index) => select.add(new Option(`${item.name} · Fluent ${item.fluent_version}`, String(index))));
}
$("#model-library").addEventListener("change", () => {
  if ($("#model-library").value === "") return;
  const item = libraryEntries[Number($("#model-library").value)];
  $("#model-case-path").value = item.case_file;
  $("#model-version").value = item.product_version || "";
  $("#model-dimension").value = item.dimension;
});

async function analyzeModel(force) {
  if (scanSubmitting) return;
  scanSubmitting = true;
  const casePath = $("#model-case-path").value.trim() || elements.caseFile.value.trim();
  try {
    const response = await fetch("/api/models/analyze", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case_file: casePath, endpoint: elements.endpoint.value.trim(),
        dimension: Number($("#model-dimension").value), product_version: $("#model-version").value.trim(),
        question: $("#goal-input").value, force }) });
    const result = await response.json();
    if (!response.ok) throw new Error(apiErrorMessage(result, "模型分析失败"));
    initialized = false;
    await refresh();
    addAgentMessage(`${result.cached ? "复用模型 Profile" : "模型扫描完成"}，推荐采用规则评分，不调用大模型。请确认范围；未适配的目标和 Gate 会阻止计算。`);
    switchView("plan");
    await loadModelLibrary();
  } catch (error) {
    $("#model-scan-status").textContent = error.message;
    addAgentMessage(error.message);
  } finally {
    scanSubmitting = false;
  }
}
$("#analyze-model").addEventListener("click", () => analyzeModel(false));
$("#force-analyze").addEventListener("click", () => analyzeModel(true));
elements.caseFile.addEventListener("input", () => { $("#model-case-path").value = elements.caseFile.value; });
$("#model-case-path").addEventListener("input", () => { elements.caseFile.value = $("#model-case-path").value; });
$("#save-ranges").addEventListener("click", async () => {
  if (!elements.form.reportValidity()) return;
  try {
    const parameters = rowValues(false).map((item) => ({...item, range_min: Number(item.range_min), range_max: Number(item.range_max)}));
    const response = await fetch("/api/models/ranges", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({parameters})});
    const result = await response.json();
    if (!response.ok) throw new Error(apiErrorMessage(result, "保存失败"));
    addAgentMessage("当前选择和人工确认范围已保存到模型 Profile。");
  } catch (error) { addAgentMessage(error.message); }
});
loadModelLibrary().catch(() => {});

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
  return parameterCatalog.find((item) => item.key === key) || null;
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
      option.textContent = item.label;
      option.selected = item.key === selectedKey;
      group.append(option);
    });
    select.append(group);
  });
}

function applyParameterInputs(rowIndex, key, useRecommended = true) {
  const parameter = parameterByKey(key);
  if (!parameter) return;
  const row = elements.parameterRows[rowIndex];
  selectedParameterKeys[rowIndex] = parameter.key;
  row.select.value = parameter.key;
  row.rangeMin.min = parameter.hard_min;
  row.rangeMin.max = parameter.hard_max;
  row.rangeMin.step = parameter.step || "any";
  row.rangeMax.min = parameter.hard_min;
  row.rangeMax.max = parameter.hard_max;
  row.rangeMax.step = parameter.step || "any";
  if (useRecommended) {
    row.rangeMin.value = parameter.recommended_min ?? "";
    row.rangeMax.value = parameter.recommended_max ?? "";
  }
  row.rangeUnitMin.textContent = parameter.unit;
  row.rangeUnitMax.textContent = parameter.unit;
  row.description.textContent = parameter.description;
  row.safetyHint.textContent = parameter.requires_range_confirmation ? "⚠ 无可靠推荐范围，请人工填写 MIN / MAX 并确认。" : `允许 ${parameter.hard_min}–${parameter.hard_max} ${parameter.unit}；建议 ${parameter.recommended_min}–${parameter.recommended_max} ${parameter.unit}。`;
  const labels = selectedParameterKeys.map((selectedKey) => parameterByKey(selectedKey)?.label).filter(Boolean);
  elements.projectParameterLabel.textContent = labels.join(" × ");
  elements.monitorTitle.textContent = `${labels.join(" + ")}优化`;
}

function applyDirectParameter(rowIndex, key, useDefault = true) {
  const parameter = parameterByKey(key);
  if (!parameter) return;
  const row = elements.directParameterRows[rowIndex];
  row.select.value = parameter.key;
  row.value.min = parameter.hard_min;
  row.value.max = parameter.hard_max;
  row.value.step = parameter.step || "any";
  if (useDefault) row.value.value = parameter.default_value;
  row.unit.textContent = parameter.unit;
  row.rangeHint.textContent = `允许 ${parameter.hard_min}–${parameter.hard_max} ${parameter.unit}；建议范围 ${parameter.recommended_min}–${parameter.recommended_max} ${parameter.unit}。`;
}

function setInputValues(defaults, parameters) {
  if (initialized) return;
  parameterCatalog = parameters || [];
  const defaultParameters = defaults.parameters || [];
  rebuildRows(defaultParameters, defaults.direct_parameters || []);
  elements.caseFile.value = defaults.case_file || "";
  elements.targetTrials.value = defaults.target_trials;
  elements.iterations.value = defaults.iterations;
  elements.endpoint.value = defaults.endpoint;
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

function apiErrorMessage(result, fallback) {
  if (typeof result?.detail === "string") return result.detail;
  if (Array.isArray(result?.detail)) {
    return result.detail.map((item) => item?.msg || String(item)).join("；");
  }
  return fallback;
}

function validTrials(trials) {
  return trials.filter((trial) => {
    const objective = Number(trial.objective_value);
    return ["COMPLETE", "PASS"].includes(trial.state) && Number.isFinite(objective);
  });
}

function parameterKeysForResult(values = {}) {
  const keys = Object.keys(values).filter((key) => hasCatalogParameter(key));
  return keys.length ? keys : selectedParameterKeys;
}

function hasCatalogParameter(key) {
  return parameterCatalog.some((item) => item.key === key);
}

function formatParameterCombination(values = {}, keys = parameterKeysForResult(values)) {
  return keys.map((key) => {
    const parameter = parameterByKey(key);
    const nativeValue = Number(values?.[key]);
    const displayValue = nativeValue * Number(parameter?.native_to_display || 1);
    return `${parameter?.label || key} ${formatNumber(displayValue, 4)} ${parameter?.unit || ""}`;
  }).join(" · ");
}

function renderStatus(state) {
  const online = state.mcp.status === "online";
  elements.mcpDot.className = `status-dot ${online ? "" : "offline"}`;
  elements.footerFluentDot.className = `status-dot ${online ? "" : "offline"}`;
  elements.mcpLabel.textContent = online ? "MCP · 服务可达" : "MCP · 服务未连接";
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

  const canRun = state.server.can_control && !busy && state.model?.execution_ready && state.model?.status !== "scanning";
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
  const parameterDetails = result.parameter_details || (result.parameter ? [result.parameter] : []);
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
  const combination = parameterDetails.map((parameter) => `${parameter.label || parameter.key || "参数"} ${formatNumber(parameter.display_value, 4)} ${parameter.unit || ""}`).join(" · ");
  elements.directCaption.textContent = `Fluent 原生 Static Temperature 云图 · ${combination || formatParameterCombination(result.parameters)} · ${classification}`;
}

function renderMetrics(state) {
  const summary = state.summary || {};
  const trials = validTrials(state.trials || []);
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
  const bestCombination = summary.best_params
    ? formatParameterCombination(summary.best_params)
    : "—";
  elements.bestTemperature.textContent = formatNumber(bestValue, 3);
  elements.bestVelocity.textContent = bestCombination;
  elements.bestParameterUnit.textContent = "";
  elements.bestTrial.textContent = summary.best_trial_number == null ? "尚无结果" : `Trial #${summary.best_trial_number}`;
  elements.resultBestTemp.textContent = formatNumber(bestValue, 3);
  elements.resultBestVelocity.textContent = bestCombination;
  elements.resultParameterLabel.textContent = "参数组合";
  elements.resultParameterUnit.textContent = "";

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
  if (summary.best_params) {
    elements.resultInsight.textContent = `当前最优组合为 ${bestCombination}。若需要更高精度，可围绕该组合缩小两个参数范围并增加目标 Trial 数。`;
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
  const primaryKey = selectedParameterKeys[0];
  const parameter = parameterByKey(primaryKey);
  const displayValue = (trial) => Number(trial.parameters?.[primaryKey]) * Number(parameter?.native_to_display || 1);
  const parameterPoints = points.filter((trial) => Number.isFinite(displayValue(trial)));
  renderChart(elements.objectiveChart, points, {
    x: (trial) => Number(trial.trial_number), y: (trial) => Number(trial.objective_value),
    xFormat: (value) => `#${Math.max(0, Math.round(value))}`, xLabel: "Trial", line: true, area: true,
    empty: "完成试验后显示目标函数历史",
    tooltip: (trial) => `Trial #${trial.trial_number} · ${formatNumber(trial.objective_value, 3)} K`,
  });
  renderChart(elements.parameterChart, parameterPoints, {
    x: displayValue, y: (trial) => Number(trial.objective_value),
    xFormat: (value) => value.toFixed(2), xLabel: `${parameter?.label || "Parameter"} (${parameter?.unit || "—"})`, line: false, area: false,
    empty: "等待有效参数点",
    tooltip: (trial) => `${formatParameterCombination(trial.parameters)} · ${formatNumber(trial.objective_value, 3)} K`,
  });
  elements.parameterChartSubtitle.textContent = `${parameter?.label || "主变量"} / outlet temperature（完整组合见 Trial）`;
}

function renderTable(trials) {
  elements.table.replaceChildren();
  elements.trialParameterHeader.textContent = "参数组合";
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
      formatParameterCombination(trial.parameters),
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
    const parameter = parameterByKey(elements.directParameterRows[0]?.select.value);
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
    const modelId = state.model?.profile?.model_id || null;
    if (initialized && modelId !== renderedModelId) initialized = false;
    setInputValues(state.defaults, state.parameters);
    renderedModelId = modelId;
    elements.currentTaskLabel.textContent = `${state.model?.profile?.name || "未选择模型"} · ${state.task?.label || "参数研究"}`;
    renderModelState(state);
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
elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.formError.textContent = "";
  if (!elements.form.reportValidity()) return;
  const parameters = elements.parameterRows.map((row) => ({
    parameter_key: row.select.value,
    range_min: Number(row.rangeMin.value),
    range_max: Number(row.rangeMax.value),
  }));
  if (new Set(parameters.map((item) => item.parameter_key)).size !== parameters.length) {
    elements.formError.textContent = "优化变量不能相同";
    elements.parameterRows[1].select.focus();
    return;
  }
  for (const [index, parameter] of parameters.entries()) {
    if (parameter.range_min >= parameter.range_max) {
      elements.formError.textContent = `变量 ${index + 1} 的上限必须大于下限`;
      elements.parameterRows[index].rangeMax.focus();
      return;
    }
  }
  const payload = {
    case_file: elements.caseFile.value.trim(), target_trials: Number(elements.targetTrials.value),
    iterations: Number(elements.iterations.value), parameters,
    endpoint: elements.endpoint.value.trim(),
  };
  submitting = true;
  elements.runButton.disabled = true;
  elements.runButtonLabel.textContent = "正在提交";
  try {
    const response = await fetch("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const result = await response.json();
    if (!response.ok) throw new Error(apiErrorMessage(result, "启动失败"));
    switchView("monitor");
    await refresh();
  } catch (error) {
    elements.formError.textContent = error.message;
  } finally {
    submitting = false;
    await refresh();
  }
});

async function submitDirectRun(fromAgent = false) {
  const parameters = elements.directParameterRows.map((row) => ({
    parameter: parameterByKey(row.select.value),
    value: Number(row.value.value),
  }));
  if (new Set(parameters.map((item) => item.parameter?.key)).size !== parameters.length) {
    const message = "审计参数不能相同。";
    elements.directError.textContent = message;
    if (fromAgent) addAgentMessage(message);
    return false;
  }
  for (const item of parameters) {
    const min = Number(item.parameter?.hard_min);
    const max = Number(item.parameter?.hard_max);
    if (!Number.isFinite(item.value) || item.value < min || item.value > max) {
      const message = `${item.parameter?.label || "参数"}必须在 ${min}–${max} ${item.parameter?.unit || ""} 之间。`;
      elements.directError.textContent = message;
      if (fromAgent) addAgentMessage(message);
      return false;
    }
  }
  const payload = {
    case_file: elements.caseFile.value.trim(),
    parameters: parameters.map((item) => ({ parameter_key: item.parameter.key, value: item.value })),
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
    if (!response.ok) throw new Error(apiErrorMessage(result, "单点计算启动失败"));
    if (fromAgent) addAgentMessage(`已提交参数组合：${parameters.map((item) => `${item.parameter.label} = ${item.value} ${item.parameter.unit}`).join("，")}。Fluent 将自动计算并返回温度云图。`);
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
  await submitDirectRun();
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
  return "当前是规则助手，未调用大模型。请在模型页填写研究问题并生成推荐，核对参数、目标和 Gate 后再执行。";
}

function directParameterFromQuestion(question) {
  if (!/(设为|改为|改成|调整|提交|计算|运行|云图)/i.test(question)) return null;
  const selected = parameterCatalog.find((item) => question.includes(item.label) || question.includes(item.key));
  const match = question.match(/(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)/i);
  return selected && match ? { key: selected.key, value: Number(match[1]) } : null;
}

elements.agentForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = elements.agentQuestion.value.trim();
  if (!question) return;
  addAgentMessage(question, true);
  elements.agentQuestion.value = "";
  const directParameter = directParameterFromQuestion(question);
  if (directParameter && elements.directParameterRows.length) {
    const matchedIndex = elements.directParameterRows.findIndex((row) => row.select.value === directParameter.key);
    const rowIndex = matchedIndex >= 0 ? matchedIndex : 0;
    applyDirectParameter(rowIndex, directParameter.key, false);
    elements.directParameterRows[rowIndex].value.value = directParameter.value;
    addAgentMessage("已把修改填入单点表单，请核对所有参数并点击计算。");
    switchView("results");
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
    selectedParameterKeys = [];
    lastState = null;
    clearConversationView();
    switchView("model");
    await refresh();
  } catch (error) {
    elements.formError.textContent = error.message;
  }
});

$("#accept-suggestion").addEventListener("click", () => switchView("plan"));
$("#generate-plan").addEventListener("click", () => analyzeModel(false));

refresh();
window.setInterval(refresh, 2000);
