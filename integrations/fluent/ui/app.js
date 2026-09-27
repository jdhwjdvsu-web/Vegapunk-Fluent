const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const elements = {
  form: $("#run-form"),
  runButton: $("#run-button"),
  runButtonLabel: $("#run-button-label"),
  formError: $("#form-error"),
  formNote: $("#form-note"),
  objectiveMetric: $("#objective-metric"),
  objectiveDirection: $("#objective-direction"),
  objectiveDescription: $("#objective-description"),
  massBalanceCheck: $("#mass-balance-check"),
  approvalBadge: $("#approval-badge"),
  agentMode: $("#agent-mode"),
  resultObjectiveLabel: $("#result-objective-label"),
  resultObjectiveUnit: $("#result-objective-unit"),
  verificationTitle: $("#verification-title"),
  verificationDetail: $("#verification-detail"),
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
  monitorObjectiveSummary: $("#monitor-objective-summary"),
  objectiveChartSubtitle: $("#objective-chart-subtitle"),
  bestObjectiveUnit: $("#best-objective-unit"),
  bestParameterUnit: $("#best-parameter-unit"),
  parameterChartSubtitle: $("#parameter-chart-subtitle"),
  trialParameterHeader: $("#trial-parameter-header"),
  footerTrial: $("#footer-trial"),
  footerFluentDot: $("#footer-fluent-dot"),
  footerFluent: $("#footer-fluent"),
  footerRunner: $("#footer-runner"),
  footerGate: $("#footer-gate"),
  updatedAt: $("#updated-at"),
  agentV3Status: $("#agent-v3-status"),
  agentV3Message: $("#agent-v3-message"),
  agentV3Questions: $("#agent-v3-questions"),
  agentV3Errors: $("#agent-v3-errors"),
  agentV3Task: $("#agent-v3-task"),
  agentV3Bindings: $("#agent-v3-bindings"),
  agentV3Mappings: $("#agent-v3-mappings"),
  agentV3Resolved: $("#agent-v3-resolved"),
  geometryRecommendation: $("#geometry-recommendation"),
  taskOverview: $("#task-overview"),
  mappingOverview: $("#mapping-overview"),
  geometryOverview: $("#geometry-overview"),
  agentProgressLabel: $("#agent-progress-label"),
  agentAttemptCancel: $("#agent-attempt-cancel"),
  agentAttemptRetry: $("#agent-attempt-retry"),
  planModeNote: $("#plan-mode-note"),
  legacyPlanEditor: $("#legacy-plan-editor"),
  advancedSettings: $("#advanced-settings"),
  v3PlanSnapshot: $("#v3-plan-snapshot"),
  v3PlanValues: $("#v3-plan-values"),
  v3EditPlan: $("#v3-edit-plan"),
  v3RevisionPanel: $("#v3-revision-panel"),
  v3RevisionVariables: $("#v3-revision-variables"),
  v3RevisionTrials: $("#v3-revision-trials"),
  v3RevisionIterations: $("#v3-revision-iterations"),
  v3RevisionNotes: $("#v3-revision-notes"),
  v3RevisionError: $("#v3-revision-error"),
  v3RevisionSubmit: $("#v3-revision-submit"),
  v3RevisionCancel: $("#v3-revision-cancel"),
  goalError: $("#goal-error"),
  generatePlan: $("#generate-plan"),
  agentPanel: $("#agent-panel"),
};

let initialized = false;
let submitting = false;
let directSubmitting = false;
let lastState = null;
let parameterCatalog = [];
let metricCatalog = [];
let selectedParameterKeys = [];
let lastConversationRevision = null;
let renderedModelId = null;


let maxParameters = 5;
let scanSubmitting = false;
let libraryEntries = [];
let renderedPlanSignature = null;
let renderedResolvedSignature = null;
let v3RevisionOpen = false;
let activeAttemptId = null;
let lastSuccessfulRefreshAt = null;
let v3RevisionPending = false;

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
  $("#contract-status").textContent = state.plan
    ? "目标已绑定到当前模型的可执行指标；修改后需要重新批准。"
    : "当前模型没有完整的参数与目标指标，请检查模型扫描结果。";
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
    addAgentMessage(`${result.cached ? "复用模型 Profile" : "模型扫描完成"}。参数和指标目录已生成；Simulation Agent 可在目录内提出方案，最终仍需你确认。`);
    switchView("plan");
    await loadModelLibrary();
    return true;
  } catch (error) {
    $("#model-scan-status").textContent = error.message;
    addAgentMessage(error.message);
    return false;
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
  const native = parameterCatalog.find((item) => item.key === key);
  const semantic = lastState?.agent?.state?.resolved_task?.task?.variables?.find((item) => item.id === key);
  return native || (semantic ? {key: semantic.id, label: semantic.name, unit: semantic.unit, native_to_display: 1} : null);
}

function activeObjective(state) {
  return state?.agent?.state?.resolved_task?.task?.objective || state?.plan?.objective;
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
  $("#model-dimension").value = String(defaults.dimension || 3);
  elements.budgetTrials.textContent = defaults.target_trials;
  const path = defaults.case_file || "尚未配置 case 路径";
  elements.modelPath.textContent = path;
  elements.modelPath.title = path;
  elements.modelName.textContent = path.split(/[\\/]/).pop() || "Fluent case";
  initialized = true;
}

function renderPlanState(state) {
  metricCatalog = state.metrics || [];
  const plan = state.plan;
  const signature = JSON.stringify({ model: state.model?.profile?.model_id, plan });
  if (plan && signature !== renderedPlanSignature) {
    const direct = rowValues(true);
    rebuildRows(plan.parameters || [], direct.length ? direct : (state.defaults.direct_parameters || []));
    elements.targetTrials.value = plan.budget.target_trials;
    elements.iterations.value = plan.budget.iterations;
    elements.budgetTrials.textContent = plan.budget.target_trials;
    elements.massBalanceCheck.checked = Boolean(plan.checks?.mass_balance);
    renderedPlanSignature = signature;
  }
  const objective = activeObjective(state);
  const selectedMetric = objective?.metric_key || "";
  elements.objectiveMetric.replaceChildren();
  metricCatalog.filter((item) => (item.roles || []).includes("objective")).forEach((item) => {
    const option = document.createElement("option");
    option.value = item.key;
    option.textContent = `${item.label} · ${item.unit}`;
    option.selected = item.key === selectedMetric;
    elements.objectiveMetric.append(option);
  });
  if (objective) elements.objectiveDirection.value = objective.direction;
  const metric = metricCatalog.find((item) => item.key === elements.objectiveMetric.value);
  elements.objectiveDescription.textContent = metric
    ? `${metric.kind} · ${metric.report_type || "mass-flow"} · ${metric.locations.join(", ")}`
    : "当前模型没有可执行目标指标";
  elements.monitorObjectiveSummary.textContent = metric && objective
    ? `${objective.direction === "maximize" ? "最大化" : "最小化"}${metric.label}；所有结果必须通过有限值与已启用的质量检查。`
    : "请先分析模型并确认目标与质量检查。";
  elements.objectiveChartSubtitle.textContent = metric ? `${metric.label} / trial` : "Objective / trial";
  const approval = state.approval?.status || "required";
  elements.approvalBadge.textContent = approval === "approved" ? "已批准" : approval === "stale" ? "方案已变更" : "等待批准";
  elements.approvalBadge.classList.toggle("approved", approval === "approved");
  elements.agentMode.textContent = state.agent?.configured
    ? `${state.agent.model_id} · 受控方案模式`
    : `未连接大模型 · ${state.agent?.reason || "需要配置模型"}`;
}

function formatNumber(value, digits = 3) {
  if (value == null || value === "") return "—";
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
    return ["COMPLETE", "PASS"].includes(trial.state) && Number.isFinite(objective)
      && trial.objective_value !== null && typeof trial.objective_value !== "boolean"
      && trial.gates?.status === "PASS" && trial.gates?.passed === true
      && trial.solve_confirmed === true && trial.gate_executed === true && trial.baseline_reloaded === true;
  });
}

function parameterKeysForResult(values = {}) {
  const keys = Object.keys(values).filter((key) => parameterByKey(key));
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
  const jobIndicator = document.createElement("i");
  elements.jobBadge.replaceChildren(jobIndicator, document.createTextNode(labels[visibleStatus] || visibleStatus));
  elements.jobBadge.className = `job-badge ${visibleStatus}`;
  elements.runningPulse.classList.toggle("running", busy);
  elements.footerRunner.textContent = busy ? "Running" : failed ? "Failed" : "Idle";

  const planningMode = state.task?.planning_mode;
  const graphState = state.agent?.state;
  const planningActive = ["queued", "running", "cancelling"].includes(state.agent?.attempt?.status);
  const legacyModeLocked = !planningMode && Boolean(graphState?.task_object || graphState?.resolved_task);
  const hasApprovableV3 = graphState?.status === "awaiting_approval"
    && Boolean(graphState?.resolved_task?.executable)
    && !(graphState?.validation_errors || []).length;
  const hasApprovablePlan = (planningMode === "manual" || (!planningMode && !legacyModeLocked)) && Boolean(state.plan);
  const canRun = state.server.can_control && !busy && !planningActive
    && (planningMode === "agent_v3" ? hasApprovableV3 : hasApprovablePlan)
    && state.model?.status !== "scanning";
  elements.runButton.disabled = !canRun || submitting;
  elements.runButtonLabel.textContent = busy ? "Fluent 正在运行" : "批准并开始实验";
  elements.directButton.disabled = !state.server.can_control || busy || planningActive || !state.model?.execution_ready || directSubmitting;
  elements.newTaskButton.disabled = !state.server.can_control || busy || planningActive;
  elements.clearConversation.disabled = !state.server.can_control || planningActive;
  $("#agent-submit").disabled = !state.server.can_control || planningActive;
  elements.generatePlan.disabled = !state.server.can_control || planningActive || scanSubmitting;
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
  const objectiveMetric = metricCatalog.find((item) => item.key === activeObjective(state)?.metric_key);
  const objectiveUnit = objectiveMetric?.unit || "";
  const direction = activeObjective(state)?.direction || "minimize";
  const trials = validTrials(state.trials || []);
  const completed = Math.max(state.trials?.length || 0, state.job.completed_trials || 0);
  const plannedTarget = state.agent?.state?.resolved_task?.task?.termination?.max_trials;
  const target = state.job.target_trials || plannedTarget || state.defaults.target_trials || 0;
  const percent = target ? Math.min(100, (completed / target) * 100) : 0;
  elements.completed.textContent = completed;
  elements.target.textContent = ` / ${target} trials`;
  elements.progress.style.width = `${percent}%`;
  elements.progressCaption.textContent = state.runtime_reconciliation?.status === "BLOCKED"
    ? "运行证据冲突或状态未知，已阻止续跑；请人工诊断"
    : state.job.status === "running"
    ? `Trial ${Math.min(completed + 1, target)} 正在由 Fluent 求解`
    : state.job.status === "completed" ? `实验已结束：${completed} 个 Trial 已记录，${summary.feasible_trials || 0} 个可行；未通过 Gate 的结果不参与最优排名。`
    : completed >= target && target ? "目标试验已完成，可查看结果与审计产物" : "等待批准或继续实验";
  elements.footerTrial.textContent = `${completed} / ${target}`;
  elements.resultCount.textContent = completed;

  const bestValue = summary.best_value;
  const bestCombination = summary.best_params
    ? formatParameterCombination(summary.best_params)
    : "—";
  elements.bestTemperature.textContent = formatNumber(bestValue, 3);
  elements.bestObjectiveUnit.textContent = objectiveUnit ? ` ${objectiveUnit}` : "";
  elements.bestVelocity.textContent = bestCombination;
  elements.bestParameterUnit.textContent = "";
  elements.bestTrial.textContent = summary.best_trial_number == null ? "尚无结果" : `Trial #${summary.best_trial_number}`;
  elements.resultBestTemp.textContent = formatNumber(bestValue, 3);
  elements.resultObjectiveLabel.textContent = objectiveMetric ? `本轮样本最佳 · ${objectiveMetric.label}` : "本轮样本最佳值";
  elements.resultObjectiveUnit.textContent = objectiveUnit ? ` ${objectiveUnit}` : "";
  elements.resultBestVelocity.textContent = bestCombination;
  elements.resultParameterLabel.textContent = "参数组合";
  elements.resultParameterUnit.textContent = "";

  if (trials.length > 1) {
    const first = Number(trials[0].objective_value);
    const best = (direction === "maximize" ? Math.max : Math.min)(...trials.map((item) => Number(item.objective_value)));
    const sampleDifference = direction === "maximize" ? best - first : first - best;
    elements.objectiveTrend.textContent = `${direction === "maximize" ? "↑" : "↓"} ${sampleDifference.toFixed(3)} ${objectiveUnit}`;
    elements.objectiveTrend.title = "相对首个有效样本的目标值变化，不代表基线改进已验证";
  } else {
    elements.objectiveTrend.textContent = "—";
  }
  elements.trialCount.textContent = `${trials.length} points`;

  const allPassed = (state.trials || []).length > 0 && (state.trials || []).every((trial) =>
    trial.gates?.status === "PASS" && trial.gates?.passed === true);
  elements.footerGate.textContent = allPassed ? "All passed" : trials.length ? "Review" : "—";
  if (summary.best_params) {
    const proxyResult = (state.agent?.state?.resolved_task?.variables || []).some((item) => item.binding?.classification === "MAPPED_PROXY");
    elements.resultInsight.textContent = `本轮已采样点中的最佳组合为 ${bestCombination}。${proxyResult ? "这是固定几何代理结果，尚未完成真实几何重建与网格验证。" : "如需判断全局最优或数值不确定度，应增加采样和独立性研究。"}`;
  }
  const verification = summary.verification;
  elements.verificationTitle.textContent = summary.best_status === "BEST_VERIFIED" ? "样本最佳点已独立复算"
    : verification?.status === "orphaned" ? "复算状态未知，已阻止自动重试"
    : verification ? "复算未获验证，需人工核对" : summary.best_params ? "样本最佳点待复算" : "等待有效结果";
  elements.verificationDetail.textContent = verification
    ? `${verification.status} · 复算 ${formatNumber(verification.verification_objective, 4)} ${verification.objective_unit || objectiveUnit} · 允许差 ${formatNumber(verification.allowed_difference, 4)}`
    : "最佳候选需从受保护基线独立复算后才标记为已复核";

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
  const best = (options.direction === "maximize" ? Math.max : Math.min)(...points.map(options.y));
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
  const objectiveMetric = metricCatalog.find((item) => item.key === activeObjective(lastState)?.metric_key);
  const objectiveUnit = objectiveMetric?.unit || "";
  const primaryKey = lastState?.agent?.state?.resolved_task?.task?.variables?.[0]?.id || selectedParameterKeys[0];
  const parameter = parameterByKey(primaryKey);
  const displayValue = (trial) => Number(trial.parameters?.[primaryKey]) * Number(parameter?.native_to_display || 1);
  const parameterPoints = points.filter((trial) => Number.isFinite(displayValue(trial)));
  renderChart(elements.objectiveChart, points, {
    x: (trial) => Number(trial.trial_number), y: (trial) => Number(trial.objective_value),
    xFormat: (value) => `#${Math.max(0, Math.round(value))}`, xLabel: "Trial", line: true, area: true, direction: activeObjective(lastState)?.direction,
    empty: "完成试验后显示目标函数历史",
    tooltip: (trial) => `Trial #${trial.trial_number} · ${formatNumber(trial.objective_value, 3)} ${objectiveUnit}`,
  });
  renderChart(elements.parameterChart, parameterPoints, {
    x: displayValue, y: (trial) => Number(trial.objective_value),
    xFormat: (value) => value.toFixed(2), xLabel: `${parameter?.label || "Parameter"} (${parameter?.unit || "—"})`, line: false, area: false, direction: activeObjective(lastState)?.direction,
    empty: "等待有效参数点",
    tooltip: (trial) => `${formatParameterCombination(trial.parameters)} · ${formatNumber(trial.objective_value, 3)} ${objectiveUnit}`,
  });
  elements.parameterChartSubtitle.textContent = `${parameter?.label || "主变量"} / ${objectiveMetric?.label || "目标值"}（完整组合见 Trial）`;
}

function renderTable(trials) {
  const objectiveMetric = metricCatalog.find((item) => item.key === activeObjective(lastState)?.metric_key);
  const objectiveUnit = objectiveMetric?.unit || "";
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
      `${formatNumber(trial.objective_value, 3)} ${objectiveUnit}`,
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
  const objectiveMetric = metricCatalog.find((item) => item.key === activeObjective(state)?.metric_key);
  const objectiveLabel = objectiveMetric?.label || "目标值";
  const objectiveUnit = objectiveMetric?.unit || "";
  const direct = state.direct_run || {};
  const paragraph = elements.agentResult.querySelector("p");
  const time = elements.agentResult.querySelector("time");
  if (direct.status === "running") {
    const parameter = parameterByKey(elements.directParameterRows[0]?.select.value);
    paragraph.textContent = `单点工况已提交。MCP 正在修改${parameter?.label || "所选参数"}、运行 Fluent 并生成温度云图。`;
    time.textContent = "Fluent 单点计算中";
  } else if (direct.status === "completed" && direct.result) {
    paragraph.textContent = `单点计算完成：${objectiveLabel} ${formatNumber(direct.result.objective_value, 3)} ${objectiveUnit}，原生 Fluent 产物已记录。`;
    time.textContent = "单点结果";
  } else if (state.job.status === "running") {
    paragraph.textContent = `Trial ${(state.trials?.length || 0) + 1} 正在运行。若发生传输不确定性，控制器会停止，不会盲目重试。`;
    time.textContent = "实验运行中";
  } else if (state.job.status === "completed" && !Number.isFinite(best)) {
    paragraph.textContent = `实验已结束，${state.trials?.length || 0} 个 Trial 已记录；没有通过验收的最优候选。终止原因：${state.summary?.termination_reason || "请查看审计记录"}。`;
    time.textContent = "无可行候选，未生成最优建议";
  } else if (trials.length) {
    paragraph.textContent = `${trials.length} 个 Trial 已记录，当前最佳${objectiveLabel}为 ${formatNumber(best, 3)} ${objectiveUnit}。最佳候选仍以独立复算状态为准。`;
    time.textContent = "结果摘要";
  } else {
    paragraph.textContent = "实验尚未开始。请先审查参数范围、目标和 Gate，再批准执行。";
    time.textContent = "等待批准";
  }
}

function prettyStructured(value, fallback) {
  return value == null ? fallback : JSON.stringify(value, null, 2);
}

function replaceSummary(container, lines, emptyText) {
  container.replaceChildren();
  if (!lines.length) {
    const empty = document.createElement("p");
    empty.textContent = emptyText;
    container.append(empty);
    return;
  }
  lines.forEach(({ label, value }) => {
    const row = document.createElement("p");
    const strong = document.createElement("strong");
    strong.textContent = label;
    row.append(strong, document.createTextNode(value ? ` ${value}` : ""));
    container.append(row);
  });
}

function addV3SnapshotItem(label, value, wide = false) {
  const item = document.createElement("div");
  item.className = `v3-snapshot-item${wide ? " wide" : ""}`;
  const caption = document.createElement("span");
  caption.textContent = label;
  const content = document.createElement("strong");
  content.textContent = value || "—";
  item.append(caption, content);
  elements.v3PlanValues.append(item);
}

function renderV3PlanSnapshot(task) {
  elements.v3PlanValues.replaceChildren();
  (task.variables || []).forEach((variable) => {
    addV3SnapshotItem(
      `变量 · ${variable.name || variable.id}`,
      `${variable.minimum} ≤ ${variable.id} ≤ ${variable.maximum} ${variable.unit || ""} · 初始值 ${variable.initial_value}`,
      true,
    );
  });
  const objective = task.objective || {};
  addV3SnapshotItem(
    "目标",
    `${objective.direction === "maximize" ? "最大化" : "最小化"} ${objective.semantic_metric || objective.metric_key || "—"}`,
  );
  addV3SnapshotItem(
    "计算预算",
    `${task.termination?.max_trials || "—"} trials · ${task.solver_requirements?.iterations || "—"} iterations/trial`,
  );
  const residuals = task.solver_requirements?.residual_thresholds || {};
  const residualText = Object.entries(residuals).map(([key, value]) => `${key}≤${value}`).join("；");
  addV3SnapshotItem(
    "质量与收敛门禁",
    `质量相对误差≤${task.solver_requirements?.mass_balance_relative_tolerance ?? "—"}${residualText ? `；${residualText}` : ""}`,
    true,
  );
}

function populateV3RevisionEditor(task) {
  elements.v3RevisionVariables.replaceChildren();
  (task.variables || []).forEach((variable) => {
    const card = document.createElement("section");
    card.className = "v3-revision-variable";
    card.dataset.variableId = variable.id;
    card.dataset.variableName = variable.name || variable.id;
    card.dataset.unit = variable.unit || "";
    card.dataset.originalMinimum = String(variable.minimum);
    card.dataset.originalMaximum = String(variable.maximum);
    card.dataset.originalInitial = String(variable.initial_value);
    const meta = document.createElement("span");
    meta.className = "variable-meta";
    meta.textContent = `语义变量 · ${variable.id}`;
    const title = document.createElement("h3");
    title.textContent = variable.name || variable.id;
    const fields = document.createElement("div");
    fields.className = "v3-revision-fields";
    [
      ["最小值", "minimum", variable.minimum],
      ["最大值", "maximum", variable.maximum],
      ["初始值", "initial", variable.initial_value],
    ].forEach(([labelText, role, value]) => {
      const label = document.createElement("label");
      label.className = "field";
      const span = document.createElement("span");
      span.textContent = `${labelText}${variable.unit ? ` / ${variable.unit}` : ""}`;
      const input = document.createElement("input");
      input.type = "number";
      input.step = "any";
      input.value = value;
      input.dataset.role = role;
      label.append(span, input);
      fields.append(label);
    });
    card.append(meta, title, fields);
    elements.v3RevisionVariables.append(card);
  });
  elements.v3RevisionTrials.value = task.termination?.max_trials ?? "";
  elements.v3RevisionIterations.value = task.solver_requirements?.iterations ?? "";
  elements.v3RevisionTrials.dataset.originalValue = elements.v3RevisionTrials.value;
  elements.v3RevisionIterations.dataset.originalValue = elements.v3RevisionIterations.value;
  elements.v3RevisionNotes.value = "";
  elements.v3RevisionError.textContent = "";
}

function v3RevisionRequest(task) {
  const variables = [...elements.v3RevisionVariables.querySelectorAll(".v3-revision-variable")].map((card) => ({
    id: card.dataset.variableId,
    name: card.dataset.variableName,
    unit: card.dataset.unit,
    minimum: Number(card.querySelector('[data-role="minimum"]').value),
    maximum: Number(card.querySelector('[data-role="maximum"]').value),
    initial_value: Number(card.querySelector('[data-role="initial"]').value),
    originals: {
      minimum: Number(card.dataset.originalMinimum),
      maximum: Number(card.dataset.originalMaximum),
      initial_value: Number(card.dataset.originalInitial),
    },
  }));
  for (const variable of variables) {
    if (![variable.minimum, variable.maximum, variable.initial_value].every(Number.isFinite)) {
      throw new Error(`${variable.name} 的最小值、最大值和初始值必须是有限数字。`);
    }
    if (variable.minimum >= variable.maximum) {
      throw new Error(`${variable.name} 的最大值必须大于最小值。`);
    }
    if (variable.initial_value < variable.minimum || variable.initial_value > variable.maximum) {
      throw new Error(`${variable.name} 的初始值必须位于修改后的范围内。`);
    }
  }
  const maxTrials = Number(elements.v3RevisionTrials.value);
  const iterations = Number(elements.v3RevisionIterations.value);
  if (!Number.isInteger(maxTrials) || maxTrials < 1 || maxTrials > 100) {
    throw new Error("最大试验数必须是 1–100 之间的整数。");
  }
  if (!Number.isInteger(iterations) || iterations < 1 || iterations > 1000000) {
    throw new Error("每次求解迭代必须是 1–1,000,000 之间的整数。");
  }
  const notes = elements.v3RevisionNotes.value.trim();
  const changed = variables.some((variable) =>
    variable.minimum !== variable.originals.minimum ||
    variable.maximum !== variable.originals.maximum ||
    variable.initial_value !== variable.originals.initial_value
  ) || maxTrials !== Number(elements.v3RevisionTrials.dataset.originalValue)
    || iterations !== Number(elements.v3RevisionIterations.dataset.originalValue)
    || Boolean(notes);
  if (!changed) throw new Error("尚未修改任何参数。请调整数值或填写其他修改要求。");
  const payload = {
    action: "revise_agent_v3_plan",
    preserve_unspecified_settings: true,
    variables: variables.map(({ originals, ...variable }) => variable),
    termination: { max_trials: maxTrials },
    solver_requirements: { iterations },
    user_notes: notes || null,
  };
  return {
    summary: `修改 V3 方案：${variables.map((variable) => `${variable.id}=${variable.minimum}…${variable.maximum} ${variable.unit}`).join("；")}；${maxTrials} trials；${iterations} iterations`,
    message: [
      "请修改当前已冻结的 Fluent Agent V3 实验方案。",
      "根据下面的结构化修订请求生成新的 ResolvedTaskObject，重新执行变量分类、MappingSpec 校验与 Reviewer；未提及的设置保持不变。",
      "不要执行实验，完成后停在等待人工审批状态；旧审批不得复用。",
      JSON.stringify(payload, null, 2),
    ].join("\n"),
  };
}

function setV3RevisionOpen(open, task = null) {
  v3RevisionOpen = open;
  elements.v3RevisionPanel.hidden = !open;
  if (open && task) {
    populateV3RevisionEditor(task);
    elements.v3RevisionVariables.querySelector("input")?.focus();
  }
  elements.runButton.disabled = elements.runButton.disabled || open || v3RevisionPending;
  elements.v3EditPlan.setAttribute("aria-expanded", String(open));
}

function renderAgentV3(state) {
  const graph = state.agent?.state || null;
  const attempt = state.agent?.attempt || null;
  const attemptActive = ["queued", "running", "cancelling"].includes(attempt?.status);
  if (attemptActive) activeAttemptId = attempt.attempt_id;
  const status = attemptActive ? (attempt.graph_status || attempt.status) : (graph?.status || attempt?.status || "not_started");
  const statusLabels = {
    not_started: "等待任务", idle: "等待任务", parsing: "理解目标",
    needs_information: "需要补充", classifying: "识别变量", designing_mapping: "设计映射",
    reviewing_mapping: "修订映射",
    human_review_required: "需要人工检查", awaiting_approval: "等待批准",
    ready_to_run: "已批准", geometry_unsupported: "几何能力不足",
    running: "执行中", completed: "已完成", failed: "规划失败", timed_out: "规划超时",
    cancelled: "已取消", interrupted: "已中断", stale_response_discarded: "任务已变化",
  };
  const nodeLabels = {
    task_parser_agent: "解析任务", task_schema_validator: "校验任务", variable_classifier_agent: "匹配能力",
    classification_validator: "校验分类", mapping_designer_agent: "设计映射", mapping_spec_validator: "验证映射",
    mapping_reviewer_agent: "自动修复", resolved_task_compiler: "冻结方案", human_approval_gate: "等待审批",
  };
  elements.agentV3Status.textContent = statusLabels[status] || status;
  elements.agentV3Status.classList.toggle("approved", ["ready_to_run", "completed"].includes(status));
  elements.agentV3Status.classList.toggle("failed", ["geometry_unsupported", "human_review_required", "failed", "timed_out", "cancelled", "interrupted"].includes(status));
  const elapsed = attempt?.started_at ? Math.max(0, Math.round((Date.now() - Date.parse(attempt.started_at)) / 1000)) : 0;
  const repairedCount = (graph?.issue_history || []).filter((issue) => issue.status === "resolved").length;
  elements.agentProgressLabel.textContent = attemptActive
    ? `${nodeLabels[attempt.current_node] || statusLabels[status] || "规划中"} · ${elapsed}s`
    : graph ? `当前阶段 · ${statusLabels[status] || status}${repairedCount ? ` · 已修复 ${repairedCount} 项` : ""}` : "尚未开始";
  elements.agentV3Message.textContent = attemptActive
    ? "规划正在后台执行。离开页面不会中止；可以在此取消。"
    : graph?.agent_message || attempt?.error || "在“定义任务”中提交研究目标后，这里会显示结构化任务、变量分类和映射审查结果。";
  elements.agentAttemptCancel.hidden = !attemptActive;
  elements.agentAttemptRetry.hidden = !["failed", "timed_out", "cancelled", "interrupted"].includes(attempt?.status);

  const questions = graph?.clarification_questions || [];
  elements.agentV3Questions.hidden = !questions.length;
  elements.agentV3Questions.textContent = questions.length ? `需要补充：${questions.join("；")}` : "";
  const validationErrors = graph?.validation_errors || [];
  const reviewIssues = graph?.mapping_review_issues || [];
  const structuredIssues = (graph?.active_issues || []).map((issue) => issue.expected || issue.code).filter(Boolean);
  const errors = structuredIssues.length ? structuredIssues : validationErrors.length ? validationErrors : reviewIssues;
  elements.agentV3Errors.hidden = !errors.length;
  elements.agentV3Errors.textContent = errors.length ? `Mapping Reviewer 修订问题：${errors.join("；")}` : "";
  elements.agentV3Task.textContent = prettyStructured(graph?.task_object, "等待自然语言任务");
  elements.agentV3Mappings.textContent = prettyStructured(graph?.mapping_specs, "尚无代理映射");
  elements.agentV3Resolved.textContent = prettyStructured(graph?.resolved_task, "审批前将在此显示最终冻结对象");

  const task = graph?.task_object;
  const objective = task?.objective;
  if (task?.variables?.length) {
    const semanticLabels = task.variables.map((variable) => variable.name || variable.id);
    elements.projectParameterLabel.textContent = semanticLabels.join(" × ");
    elements.monitorTitle.textContent = `${semanticLabels.join(" + ")}优化`;
  }
  replaceSummary(elements.taskOverview, task ? [
    { label: "问题", value: task.research_question },
    { label: "目标", value: `${objective?.direction === "maximize" ? "最大化" : "最小化"} ${objective?.semantic_metric || "—"} · ${objective?.location || "—"}` },
    { label: "固定条件", value: (task.fixed_conditions || []).length
      ? task.fixed_conditions.map((item) => `${item.name} ${formatNumber(item.value, 4)} ${item.unit}`).join("；")
      : "未声明" },
    { label: "预算", value: `${task.termination?.max_trials || "—"} trials · ${task.solver_requirements?.iterations || "—"} iterations` },
  ] : [], "等待 Agent 解析研究目标。");

  elements.agentV3Bindings.replaceChildren();
  const bindings = graph?.variable_bindings || [];
  if (!bindings.length) {
    const empty = document.createElement("p");
    empty.textContent = "尚无分类结果";
    elements.agentV3Bindings.append(empty);
  }
  bindings.forEach((binding) => {
    const item = document.createElement("div");
    item.className = "agent-v3-binding";
    const title = document.createElement("strong");
    title.textContent = binding.variable_id;
    const badge = document.createElement("span");
    badge.className = `classification-badge ${binding.classification === "GEOMETRY_UNSUPPORTED" ? "geometry" : ""}`;
    badge.textContent = binding.classification;
    title.append(badge);
    const detail = document.createElement("small");
    const parameters = (binding.candidate_parameter_ids || []).join(", ") || "无原生参数";
    detail.textContent = `Capability: ${binding.selected_capability_id || "无"} · Fluent IDs: ${parameters} · confidence: ${binding.confidence}`;
    const reason = document.createElement("small");
    reason.textContent = binding.classification_reason;
    item.append(title, detail, reason);
    elements.agentV3Bindings.append(item);
  });

  const mappings = graph?.mapping_specs || [];
  const hasMappedProxy = bindings.some((binding) => binding.classification === "MAPPED_PROXY");
  replaceSummary(elements.mappingOverview, mappings.map((mapping) => ({
    label: mapping.source_variables?.join(" + ") || mapping.mapping_id,
    value: `→ ${(mapping.selected_fluent_parameter_ids || []).join(", ")} · 置信度 ${Math.round(Number(mapping.confidence || 0) * 100)}%`,
  })), bindings.length && !mappings.length
    ? (hasMappedProxy ? "代理变量的映射尚未通过校验。" : "变量可直接落到 Fluent，无需代理映射。")
    : "尚无映射验证。");

  const recommendation = graph?.geometry_recommendations?.[0] || null;
  elements.geometryRecommendation.textContent = prettyStructured(
    recommendation,
    "存在通过独立复算的代理最优解后，将显示 GeometryRecommendation。",
  );
  replaceSummary(elements.geometryOverview, recommendation ? [
    { label: recommendation.recommended_geometry_parameter, value: `${formatNumber(recommendation.recommended_geometry_value, 4)} ${recommendation.unit || ""}` },
    { label: "置信度", value: `${Math.round(Number(recommendation.confidence || 0) * 100)}% · 需要几何复核` },
  ] : [], graph?.geometry_unsupported?.length ? `当前 Case 不支持：${graph.geometry_unsupported.join("、")}` : "等待几何类变量或优化结果。");

  const frozenTask = graph?.resolved_task?.task || null;
  if (frozenTask?.termination?.max_trials) {
    elements.targetTrials.value = frozenTask.termination.max_trials;
    elements.budgetTrials.textContent = frozenTask.termination.max_trials;
  }
  if (frozenTask?.solver_requirements?.iterations) elements.iterations.value = frozenTask.solver_requirements.iterations;
  const v3Mode = state.task?.planning_mode === "agent_v3";
  const legacyModeLocked = !state.task?.planning_mode && Boolean(graph?.task_object || graph?.resolved_task);
  const hasFrozenTask = Boolean(frozenTask);
  const resolvedSignature = graph?.resolved_task ? JSON.stringify(graph.resolved_task) : null;
  if (hasFrozenTask && resolvedSignature !== renderedResolvedSignature) {
    renderedResolvedSignature = resolvedSignature;
    renderV3PlanSnapshot(frozenTask);
  }
  elements.legacyPlanEditor.hidden = v3Mode || legacyModeLocked;
  elements.legacyPlanEditor.querySelectorAll("input, select, button").forEach((control) => { control.disabled = v3Mode || legacyModeLocked; });
  elements.v3PlanSnapshot.hidden = !hasFrozenTask;
  elements.advancedSettings.classList.toggle("is-frozen", hasFrozenTask);
  [elements.caseFile, elements.endpoint, elements.targetTrials, elements.iterations].forEach((control) => {
    control.readOnly = v3Mode || legacyModeLocked;
    control.setAttribute("aria-readonly", String(v3Mode || legacyModeLocked));
  });
  const directStatus = state.direct_run?.status || "idle";
  const busy = state.job.status === "running" || directStatus === "running";
  elements.v3EditPlan.disabled = !v3Mode || !hasFrozenTask || !state.server.can_control || busy || v3RevisionPending;
  elements.v3RevisionSubmit.disabled = v3RevisionPending;
  elements.v3RevisionCancel.disabled = v3RevisionPending;
  elements.runButton.disabled = elements.runButton.disabled || v3RevisionOpen || v3RevisionPending;
  if (v3RevisionPending) {
    elements.approvalBadge.textContent = "正在重新规划";
    elements.approvalBadge.classList.remove("approved");
    elements.runButtonLabel.textContent = "等待新方案";
  } else if (hasFrozenTask && !busy) {
    elements.runButtonLabel.textContent = "批准冻结方案并开始实验";
  }
  if (v3Mode && state.server.can_control && !busy) {
    elements.formNote.textContent = v3RevisionOpen
      ? "正在编辑修订请求。提交后会重新规划；取消后才能批准当前冻结方案。"
      : hasFrozenTask
        ? "运行将严格采用上方冻结对象。需要改参数时请先点击“修改方案”，重新规划并审批。"
        : "Agent V3 正在形成结构化方案；补充信息并等待校验完成后才能审批。";
  }
  if (!v3Mode) {
    renderedResolvedSignature = null;
    v3RevisionOpen = false;
    v3RevisionPending = false;
    elements.v3RevisionPanel.hidden = true;
  }
  elements.planModeNote.textContent = legacyModeLocked
    ? "这是升级前创建的只读历史任务。为避免猜测审批模式，请新建任务后再规划或执行。"
    : v3Mode
    ? (hasFrozenTask
      ? "Agent V3 方案已冻结。下方参数只读；如需调整，请使用“修改方案”生成新版本，旧审批不会沿用。"
      : "当前任务由 Agent V3 规划。手动编辑器已隔离，校验完成前不会启用审批和运行。")
    : "当前使用目录驱动的手动方案；也可以返回“定义任务”让 Agent 生成 V3 方案。";
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
    lastSuccessfulRefreshAt = new Date();
    lastState = state;
    const modelId = state.model?.profile?.model_id || null;
    if (initialized && modelId !== renderedModelId) initialized = false;
    setInputValues(state.defaults, state.parameters);
    renderPlanState(state);
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
    renderAgentV3(state);
  } catch (error) {
    elements.mcpDot.className = "status-dot offline";
    elements.mcpLabel.textContent = "控制服务未连接";
    elements.footerFluentDot.className = "status-dot offline";
    elements.footerFluent.textContent = "Offline";
    elements.updatedAt.textContent = `数据未更新${lastSuccessfulRefreshAt ? ` · 上次成功 ${lastSuccessfulRefreshAt.toLocaleTimeString("zh-CN")}` : ""}`;
    [elements.runButton, elements.directButton, elements.generatePlan, $("#agent-submit")].forEach((button) => { button.disabled = true; });
    elements.formError.textContent = error.message;
  }
}

elements.targetTrials.addEventListener("input", () => { elements.budgetTrials.textContent = elements.targetTrials.value || "—"; });
elements.objectiveMetric.addEventListener("change", () => {
  const metric = metricCatalog.find((item) => item.key === elements.objectiveMetric.value);
  if (metric) {
    elements.objectiveDescription.textContent = `${metric.kind} · ${metric.report_type || "mass-flow"} · ${metric.locations.join(", ")}`;
    elements.objectiveDirection.value = metric.recommended_direction;
  }
});
elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.formError.textContent = "";
  if (!elements.form.reportValidity()) return;
  const v3Mode = lastState?.task?.planning_mode === "agent_v3";
  if (v3Mode && (v3RevisionOpen || v3RevisionPending)) {
    elements.formError.textContent = v3RevisionPending
      ? "正在重新生成方案，请等待 Mapping Reviewer 完成。"
      : "请先提交或取消当前方案修改，再批准执行。";
    elements.v3RevisionPanel.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  const parameters = elements.parameterRows.map((row) => ({
    parameter_key: row.select.value,
    range_min: Number(row.rangeMin.value),
    range_max: Number(row.rangeMax.value),
  }));
  if (!v3Mode && new Set(parameters.map((item) => item.parameter_key)).size !== parameters.length) {
    elements.formError.textContent = "优化变量不能相同";
    elements.parameterRows[1].select.focus();
    return;
  }
  for (const [index, parameter] of (v3Mode ? [] : parameters).entries()) {
    if (parameter.range_min >= parameter.range_max) {
      elements.formError.textContent = `变量 ${index + 1} 的上限必须大于下限`;
      elements.parameterRows[index].rangeMax.focus();
      return;
    }
  }
  const payload = {
    case_file: elements.caseFile.value.trim(), target_trials: Number(elements.targetTrials.value),
    iterations: Number(elements.iterations.value),
    ...(v3Mode ? {} : { parameters }),
    endpoint: elements.endpoint.value.trim(),
  };
  const planPayload = {
    research_question: $("#goal-input").value.trim(),
    parameters,
    objective: {
      metric_key: elements.objectiveMetric.value,
      direction: elements.objectiveDirection.value,
    },
    constraints: [],
    checks: {
      finite_outputs: true,
      mass_balance: elements.massBalanceCheck.checked,
      mass_balance_relative_tolerance: 0.001,
      residual_thresholds: {},
    },
    budget: { target_trials: payload.target_trials, iterations: payload.iterations },
  };
  submitting = true;
  elements.runButton.disabled = true;
  elements.runButtonLabel.textContent = "正在提交";
  try {
    if (!v3Mode) {
      const saved = await fetch("/api/plans", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(planPayload) });
      const savedResult = await saved.json();
      if (!saved.ok) throw new Error(apiErrorMessage(savedResult, "方案保存失败"));
    }
    const graphState = lastState?.agent?.state || {};
    const approvalRequest = v3Mode
      ? { approved_by: "local-user", plan_revision: graphState.plan_revision, planning_attempt_id: graphState.planning_attempt_id }
      : { approved_by: "local-user" };
    const approved = await fetch("/api/plans/approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(approvalRequest) });
    const approvedResult = await approved.json();
    if (!approved.ok) throw new Error(apiErrorMessage(approvedResult, "方案审批失败"));
    if (v3Mode) payload.start_request_id = approvedResult.fingerprint;
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

const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

async function pollPlanningAttempt(attemptId) {
  while (true) {
    await wait(700);
    const response = await fetch(`/api/agent/attempts/${encodeURIComponent(attemptId)}`);
    const attempt = await response.json();
    if (!response.ok) throw new Error(apiErrorMessage(attempt, "无法读取规划进度"));
    activeAttemptId = ["queued", "running", "cancelling"].includes(attempt.status) ? attemptId : null;
    await refresh();
    if (activeAttemptId) continue;
    const graph = lastState?.agent?.state || {};
    const followups = (graph.clarification_questions || []).length
      ? `\n待确认：${graph.clarification_questions.join("；")}` : "";
    if (graph.agent_message || attempt.error) addAgentMessage(`${graph.agent_message || attempt.error}${followups}`);
    if (graph.task_object || graph.resolved_task) {
      renderedPlanSignature = null;
      switchView("plan");
    }
    if (["failed", "timed_out", "interrupted"].includes(attempt.status)) {
      throw new Error(attempt.error || `规划结束于 ${attempt.status}`);
    }
    return attempt.status !== "cancelled";
  }
}

async function requestAgent(question, showUserMessage = true, inputCategory = "user_message") {
  if (!question) return false;
  if (showUserMessage) addAgentMessage(question, true);
  try {
    const response = await fetch("/api/agent/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question, input_category: inputCategory }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(apiErrorMessage(result, "助手请求失败"));
    activeAttemptId = result.attempt_id;
    addAgentMessage(`规划已提交到后台（${result.effective_reasoning || "medium"} reasoning）。`);
    switchView("plan");
    await refresh();
    return await pollPlanningAttempt(result.attempt_id);
  } catch (error) {
    addAgentMessage(error.message);
    throw error;
  }
}

elements.agentAttemptCancel.addEventListener("click", async () => {
  const attemptId = activeAttemptId || lastState?.agent?.attempt?.attempt_id;
  if (!attemptId) return;
  elements.agentAttemptCancel.disabled = true;
  try {
    const response = await fetch(`/api/agent/attempts/${encodeURIComponent(attemptId)}/cancel`, { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(apiErrorMessage(result, "取消失败"));
    activeAttemptId = null;
    addAgentMessage("本次规划已取消，迟到响应不会写入当前任务。");
    await refresh();
  } catch (error) {
    addAgentMessage(error.message);
  } finally {
    elements.agentAttemptCancel.disabled = false;
  }
});

elements.agentAttemptRetry.addEventListener("click", async () => {
  const attemptId = lastState?.agent?.attempt?.attempt_id;
  if (!attemptId) return;
  elements.agentAttemptRetry.disabled = true;
  try {
    const response = await fetch(`/api/agent/attempts/${encodeURIComponent(attemptId)}/retry`, { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(apiErrorMessage(result, "重试失败"));
    activeAttemptId = result.attempt_id;
    addAgentMessage("已创建新的规划 attempt，历史失败记录会保留。", true);
    await pollPlanningAttempt(result.attempt_id);
  } catch (error) {
    addAgentMessage(error.message);
  } finally {
    elements.agentAttemptRetry.disabled = false;
  }
});

elements.v3EditPlan.addEventListener("click", () => {
  const task = lastState?.agent?.state?.resolved_task?.task;
  if (!task || v3RevisionPending) return;
  setV3RevisionOpen(true, task);
  elements.formError.textContent = "";
  elements.v3RevisionPanel.scrollIntoView({ behavior: "smooth", block: "start" });
});

elements.v3RevisionCancel.addEventListener("click", () => {
  if (v3RevisionPending) return;
  setV3RevisionOpen(false);
  elements.v3RevisionError.textContent = "";
  refresh();
});

elements.v3RevisionSubmit.addEventListener("click", async () => {
  const task = lastState?.agent?.state?.resolved_task?.task;
  if (!task || v3RevisionPending) return;
  elements.v3RevisionError.textContent = "";
  let revision;
  try {
    revision = v3RevisionRequest(task);
  } catch (error) {
    elements.v3RevisionError.textContent = error.message;
    return;
  }
  v3RevisionPending = true;
  elements.v3RevisionSubmit.disabled = true;
  elements.v3RevisionCancel.disabled = true;
  elements.v3RevisionSubmit.textContent = "正在重新规划…";
  elements.runButton.disabled = true;
  elements.runButtonLabel.textContent = "等待新方案";
  elements.approvalBadge.textContent = "正在重新规划";
  elements.approvalBadge.classList.remove("approved");
  addAgentMessage(revision.summary, true);
  try {
    await requestAgent(revision.message, false, "plan_revision");
    v3RevisionOpen = false;
    elements.v3RevisionPanel.hidden = true;
    renderedResolvedSignature = null;
  } catch (error) {
    elements.v3RevisionError.textContent = error.message;
  } finally {
    v3RevisionPending = false;
    elements.v3RevisionSubmit.disabled = false;
    elements.v3RevisionCancel.disabled = false;
    elements.v3RevisionSubmit.textContent = "重新生成并校验方案";
    await refresh();
  }
});

elements.agentForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = elements.agentQuestion.value.trim();
  if (!question) return;
  elements.agentQuestion.value = "";
  $("#agent-submit").disabled = true;
  try {
    const category = lastState?.agent?.state?.status === "needs_information"
      ? "physical_clarification" : "user_message";
    await requestAgent(question, true, category);
  } catch (_) {
    // The error is already rendered in the conversation.
  } finally {
    $("#agent-submit").disabled = false;
  }
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

async function generatePlan() {
  const goal = $("#goal-input").value.trim();
  elements.goalError.textContent = "";
  if (!goal) {
    elements.goalError.textContent = "请先描述要研究的变量、目标或约束。";
    $("#goal-input").focus();
    return;
  }
  elements.generatePlan.disabled = true;
  elements.generatePlan.textContent = "正在生成方案…";
  try {
    if (!lastState?.model?.profile) {
      const analyzed = await analyzeModel(false);
      if (!analyzed) throw new Error("模型分析未完成，暂时无法生成实验方案。");
    }
    await requestAgent(goal, true, "initial_request");
  } catch (error) {
    elements.goalError.textContent = error.message;
    switchView(lastState?.model?.profile ? "plan" : "model");
  } finally {
    elements.generatePlan.disabled = false;
    elements.generatePlan.textContent = "生成实验方案 →";
  }
}

elements.generatePlan.addEventListener("click", generatePlan);
$$('[data-prompt]').forEach((button) => {
  button.addEventListener("click", () => {
    $("#goal-input").value = button.dataset.prompt;
    $("#goal-input").focus();
  });
});

function setAgentOpen(open) {
  elements.agentPanel.classList.toggle("open", open);
  document.body.classList.toggle("agent-collapsed", !open);
  $("#agent-toggle").setAttribute("aria-expanded", String(open));
}
$("#agent-toggle").addEventListener("click", () => setAgentOpen(!elements.agentPanel.classList.contains("open")));
$("#agent-close").addEventListener("click", () => setAgentOpen(false));
if (window.innerWidth <= 1180) setAgentOpen(false);

if (new URLSearchParams(window.location.search).get("preview") === "1") {
  $("#preview-notice").hidden = false;
  elements.controlMode.textContent = "样例预览";
}

refresh();
window.setInterval(refresh, 2000);
