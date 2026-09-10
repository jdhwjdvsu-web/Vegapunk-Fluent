# Vegapunk / PyFluent-MCP integration

## Adaptive parameters milestone (2026-09-07)

The Web workbench now discovers its parameter catalog from the selected Case via
read-only MCP introspection. It supports 1–5 parameters, rule-based Top-2 ranking,
saved ranges and a SHA256/version-aware SQLite model library. New Cases are not
allowed to inherit the Mixing Elbow objective or gates: automatic Objective/Gate
adaptation remains the next milestone. See
[implementation, usage and live evidence](../../docs/Fluent_Adaptive_实施与使用.md).

The previous fixed catalog is retained only for legacy Python-call compatibility;
an unscanned Web model exposes no parameters. The current assistant is a rule-based
planner, not an LLM agent.

This implementation uses the official
[ansys/pyfluent-mcp](https://github.com/ansys/pyfluent-mcp) server.

This integration keeps Vegapunk and Fluent in separate runtimes:

```text
Vegapunk (Ubuntu/WSL, Python 3.11)
        |
        | FastMCP over HTTP
        v
PyFluent-MCP (Windows, Python 3.12+)
        |
        | PyFluent
        v
Ansys Fluent (Windows)
```

The separation is intentional: the current Vegapunk environment targets Python 3.11,
while `ansys-fluent-mcp` 0.4.0 requires Python 3.12 or newer.

## 1. Install and start the Windows service

From PowerShell:

```powershell
cd D:\Vegapunk-Fluent\integrations\fluent\windows
.\install_pyfluent_mcp.ps1
.\start_pyfluent_mcp.ps1
```

The default address is `http://127.0.0.1:18000/mcp`. If Vegapunk runs in WSL and
cannot reach Windows loopback, start the service on a Windows-reachable interface:

```powershell
.\start_pyfluent_mcp.ps1 -BindAddress 0.0.0.0
```

Then set `connection.endpoint` to the Windows host IP in the task specification and
set `allow_remote_endpoint` to `true`. Limit TCP port 18000 to WSL/private networks
with Windows Firewall; PyFluent-MCP must not be exposed to the public internet.

The included WSL launcher discovers the current Windows gateway on every run, so it
continues to work when WSL's NAT address changes after a reboot:

```bash
cd /mnt/d/Vegapunk-Fluent
export FLUENT_CASE_FILE='C:\cases\mixing_elbow.cas.h5'
bash integrations/fluent/wsl/run_fluent_experiment.sh
```

The installed Ubuntu runtime uses Python 3.11 at
`/home/vegapunk/.venvs/vegapunk311`. Override it with `VEGAPUNK_PYTHON` if the
environment is moved. The Windows MCP service remains a separate Python 3.13
environment under `%LOCALAPPDATA%\Vegapunk\fluent-mcp`.

## 2. Validate without running Fluent

```powershell
$env:FLUENT_CASE_FILE = 'D:\cases\mixing_elbow.cas.h5'
python -m vegapunk.fluent --spec config\fluent\mixing_elbow.example.json `
  --output-dir $env:TEMP\vegapunk-fluent-dry-run --dry-run
```

## 3. Run an experiment

```powershell
python -m vegapunk.fluent --spec config\fluent\mixing_elbow.example.json `
  --output-dir $env:TEMP\vegapunk-fluent-run
```

The runner validates the declarative specification, checks parameter bounds,
generates fixed-shape PyFluent code, calls PyFluent-MCP `validate_code`, executes one
design point at a time, and writes:

- `generated_code/`: exact audited code sent to Fluent
- `solver_stdout/`: one solver transcript per design point
- `fluent_result.json`: parameters, reports, residuals, constraints and validity
- `final_info.json`: Vegapunk comparison metrics

## Safety contract

The agent-facing input has no arbitrary code field. Parameters require an allowlisted
Fluent settings path, object name, numeric bounds and complete design-point values.
Endpoints are loopback-only unless remote access is explicitly enabled. Production
tasks should keep residual thresholds and engineering report constraints enabled.

The example changes a boundary-condition value. Geometry changes require a separate
Fluent Meshing or PyWorkbench workflow and are not implied by this runner.

## 4. Run the Optuna walking skeleton

The v0.1 demo keeps one serial Fluent session per controller process. Every trial
reloads the same baseline case, recreates report definitions, applies only validated
numeric parameters, initializes, solves, reads the objective and inlet/outlet mass
flows, runs validity gates, and then calls Optuna `tell`.

For a Windows-only two-point baseline check, use the PyFluent environment directly:

```powershell
$env:FLUENT_CASE_FILE = 'C:\path\to\mixing_elbow.cas.h5'
& "$env:LOCALAPPDATA\Vegapunk\fluent-mcp\.venv\Scripts\python.exe" `
  integrations\fluent\evaluate.py `
  --spec config\fluent\mixing_elbow.optuna-demo.json `
  --output runs\fluent_demo_v01\direct_validation.json `
  --values 0.55 0.85
```

Start the Windows MCP service on the WSL-reachable interface:

```powershell
cd D:\Vegapunk-Fluent\integrations\fluent\windows
.\start_pyfluent_mcp.ps1 -BindAddress 0.0.0.0
```

Run from WSL. The launcher discovers the current Windows gateway automatically:

```bash
cd /mnt/d/Vegapunk-Fluent
export FLUENT_CASE_FILE='C:\path\to\mixing_elbow.cas.h5'
bash integrations/fluent/wsl/run_optuna_demo.sh \
  config/fluent/mixing_elbow.optuna-demo.json runs/fluent_demo_v01 6
```

To demonstrate controlled recovery, first stop normally after three completed
trials, then launch the same output directory with a target of six:

```bash
bash integrations/fluent/wsl/run_optuna_demo.sh \
  config/fluent/mixing_elbow.optuna-demo.json runs/fluent_demo_v01 3
bash integrations/fluent/wsl/run_optuna_demo.sh \
  config/fluent/mixing_elbow.optuna-demo.json runs/fluent_demo_v01 6
```

The first command creates the SQLite study and closes Fluent. The second loads the
same study and continues at the next trial; completed trials are not recomputed.

The audited output directory contains:

- `study.sqlite3`: persistent Optuna study
- `run_log.jsonl`: append-only controller events
- `preflight_rejection.json`: deliberately illegal parameter rejected before MCP
- `trial_results/trial-*.json`: parameters, objective, gates, duration and reports
- `generated_code/trial-*.py`: exact fixed-shape PyFluent code sent through MCP
- `solver_stdout/trial-*.log`: complete Fluent transcript per trial
- `demo_summary.json` and `best_result.json`: study counts and best result

The committed validation run completed six trials: two random startup trials and
four TPE trials. All passed the finite-number and 0.1% mass-conservation gates. It
also demonstrated a normal restart after trial 2. See
`runs/fluent_demo_v01/demo_summary.json` for the exact values.

## 5. Open the AI Scientist Simulation Workbench

The responsive browser UI is organized as a scientific task workbench instead of a
traditional Fluent parameter panel. The left rail follows the research workflow
(model and data, experiment plan, experiment monitor, results and artifacts), the
center pane shows the selected stage, the right pane provides a bounded planning
assistant, and the bottom strip keeps Fluent, Runner, Gate and Trial state visible.

The default monitor view highlights progress, the best objective, objective history,
parameter space and auditable Trial records. Advanced settings keep the case path,
MCP endpoint, trial budget, solver iterations and parameter bounds available without
making them the primary interface. Agent-generated plans always stop at a human
approval gate before a local run is submitted.

Start it from WSL. The launcher resolves the current Windows gateway for MCP and
binds the UI to all local interfaces:

```bash
cd /mnt/d/Vegapunk-Fluent
export FLUENT_CASE_FILE='C:\path\to\mixing_elbow.cas.h5'
bash integrations/fluent/wsl/run_fluent_ui.sh
```

Open `http://127.0.0.1:8780` on the same computer. A Windows-only launch also works:

```powershell
$env:FLUENT_CASE_FILE = 'C:\path\to\mixing_elbow.cas.h5'
python -m vegapunk.fluent.web --host 0.0.0.0 --port 8780
```

The loopback page may submit runs. LAN and Tailscale views are read-only by default,
so opening a dashboard link on another device cannot start Fluent accidentally.
Only use `--allow-remote-control` behind a trusted authenticated network boundary.
There is intentionally no cancel button: interrupting a submitted solver call can
leave Fluent state uncertain, and the controller requires an explicit restart in
that situation.

## 6. Run the persistent V1 Job path

Job mode adds a second Windows MCP endpoint. Keep the official PyFluent-MCP on port
18000, then start the Vegapunk Job facade on port 18001 in another PowerShell:

```powershell
cd D:\Vegapunk-Fluent\integrations\fluent\windows
.\start_pyfluent_mcp.ps1 -BindAddress 0.0.0.0 -Port 18000
.\start_vegapunk_job_mcp.ps1 -BindAddress 0.0.0.0 -Port 18001
```

The Job endpoint persists `job.json`, `events.jsonl`, `result.json`, and
`transcript.log` under `%LOCALAPPDATA%\Vegapunk\fluent-jobs\jobs\job-*`. It returns a
stable `job_id` immediately, deduplicates the same Campaign/Trial/Attempt, and marks
nonterminal work `ORPHANED` after a service restart.

Enable the Job-backed Controller from WSL:

```bash
cd /mnt/d/Vegapunk-Fluent
export FLUENT_CASE_FILE='C:\path\to\mixing_elbow.cas.h5'
export FLUENT_JOB_MODE=1
bash integrations/fluent/wsl/run_optuna_demo.sh \
  config/fluent/mixing_elbow.optuna-demo.json \
  runs/fluent_job_v1 \
  20
```

The Controller polls heartbeats, applies the per-Trial timeout, retries failed or
explicitly `retry_safe` attempts from the immutable baseline, and persists the V1
state chain through `CREATED → SUBMITTED → RUNNING → RESULT_READY → GATED → TOLD`.
An orphan with unconfirmed worker termination stops for manual recovery instead of
risking a duplicate Fluent solve.

## 7. Stage-0 and Stage-4 admission tools

Repeatability thresholds are data-driven rather than hard-coded to 0.1%:

```bash
python -m vegapunk.fluent.baseline \
  --input baseline_records.json \
  --output baseline_assessment.json
```

After the isolated fault-injection Campaign, validate its 20 evidence records:

```bash
python -m vegapunk.fluent.acceptance \
  --input acceptance_records.json \
  --output acceptance_summary.json
```

The fixed plan is `config/fluent/acceptance_v1.json`. Manual Fluent termination,
MCP restart, and WSL Controller restart are intentionally explicit test actions; the
validator never pretends those failures happened when they were not actually run.
The real Mixing Elbow run from 2026-08-31 is summarized in
`docs/fluent_v1_acceptance_20260831_summary.json`; full local evidence is kept under
`runs/fluent_v1_acceptance_20260831/` and the external Windows Job Store.

## V1 limitations and deferred work

- Serial execution only; there is no Fluent worker pool or parallel study.
- Job mode provides heartbeat, timeout, retry, idempotency and persisted state. A
  Job-service crash is persisted as `ORPHANED`; because the old Fluent process cannot
  yet be terminated or reattached with certainty, the Controller refuses an automatic
  retry until an operator confirms cleanup.
- Running-Job cancellation deliberately returns `cancelled=false` until the
  downstream PyFluent service can confirm that Fluent actually stopped. Queued Jobs
  can be cancelled safely. This prevents false cancellation and duplicate solves.
- Parameters are existing Fluent settings only. Geometry edits and remeshing are out
  of scope.
- One scalar objective, one to five numeric parameters, safe linear parameter
  combinations, engineering constraints, mass balance, configurable salt/component
  balances, residuals and last-N report stationarity are supported. Multi-objective,
  surrogate and multi-fidelity optimization remain deferred.
- Campaign fingerprints include the model identity, search space, objective,
  constraints, solver, Gate version and Optuna version. Put the Stage-0 case digest in
  `connection.baseline_sha256`; without it, the canonical case path is the baseline
  identity.
- Vegapunk exposes this as an explicit integration command; autonomous outer-loop
  research orchestration is not part of v0.1.

The detailed implementation/acceptance matrix is in
`docs/Fluent_V1_实施核对.md`.
