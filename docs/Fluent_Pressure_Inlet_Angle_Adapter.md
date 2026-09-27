# Pressure-inlet XY angle adapter (2026-09-15)

The V3 angle proxy now supports complete XYZ groups for pressure inlets, including
one semantic angle driving multiple inlets. It does not rotate a fan or remesh CAD.

## Discovery and execution contract

- Parameter rules 1.1 and scanner 1.2 invalidate older catalogs.
- The scanner uses an owned, newly loaded Fluent session; it never takes over an
  active session and never saves the case. For each pressure inlet it temporarily
  selects `Direction Vector`, verifies a constant unit XYZ vector is writable and
  reads back the same vector, restores the original mode, and compares the active
  momentum state with the original. Failure to restore aborts discovery.
- Virtual scalar IDs `flow_direction_x/y/z` refer to a fixed adapter, not arbitrary
  Fluent scalar paths. They are not exposed as independent DIRECT capabilities.
- Registry 3.1 retains every complete inlet group rather than overwriting one
  inlet's components with another's. The mapping validator checks normalization
  per inlet, requires complete XYZ, and requires Z=0 for this XY capability.
- Code generation validates all vector groups before generating writes, switches
  only the selected pressure inlet modes, writes each vector atomically and verifies
  numerical readback before initialization/iteration. Every trial reloads baseline.
- The LLM still chooses the mapping from the real scanned catalog. Approval binds
  the entire mapping, case identity, budgets and solver acceptance conditions.

## Persistent Job integration

When `connection.job_endpoint` is configured, the native Job controller shares the
semantic campaign identity and verifies that baseline and execution contract agree.
Its native-vector trial ledger is stored under `native_execution/trial_ledger` so
semantic angle values and native component values cannot collide. Job IDs and the
RESULT_READY → GATED → TOLD transitions remain durable.

The interface is validated for three-component pressure inlet vectors in Fluent
2026 R1. This is not a claim of validated 2024 execution or real geometry rotation.

Focused regressions are in `tests/fluent/test_pressure_direction.py` and
`tests/fluent/test_experiment_orchestrator_v3.py`. Real run evidence is retained
separately under `runs/cht_closed_loop_20260915`; Fake tests alone are not acceptance.
