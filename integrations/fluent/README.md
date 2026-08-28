# Vegapunk / PyFluent-MCP integration

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

## v0.1 limitations and deferred work

- Serial execution only; there is no Fluent worker pool or parallel study.
- A transport failure after `run_code` submission terminates the controller because
  Fluent state is uncertain. The same solve is never retried blindly.
- Recovery is controlled process restart through SQLite, not heartbeat, watchdog,
  job queue, or automated crash recovery.
- Parameters are existing Fluent settings only. Geometry edits and remeshing are out
  of scope.
- One scalar objective and independent float parameters are supported. Multi-objective
  optimization, coupled constraints, surrogate models and multi-fidelity runs are
  deferred.
- The demo records residuals but does not reject on a residual threshold; the
  acceptance gate is finite outputs plus mass conservation.
- Baseline identity is the configured case path. Content hashing, campaign versions,
  solver/build fingerprints and immutable case archival are deferred.
- Vegapunk exposes this as an explicit integration command; autonomous outer-loop
  research orchestration is not part of v0.1.
