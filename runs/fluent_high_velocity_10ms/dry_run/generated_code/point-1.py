__vp_point_name = 'anomaly-cold-inlet-10ms'
__vp_applied = {}
solver.settings.setup.boundary_conditions.velocity_inlet['cold-inlet'].momentum.velocity_magnitude.value = 10.0
__vp_applied['cold_inlet_velocity'] = 10.0
solver.settings.solution.initialization.hybrid_initialize()
solver.settings.solution.run_calculation.iterate(iter_count=200)
__vp_computed = solver.settings.solution.report_definitions.compute(report_defs=['outlet-temp-avg', 'mass-flow-in', 'mass-flow-out'])
__vp_payload = {'name': __vp_point_name, 'parameters': __vp_applied, 'iterations_requested': 200, 'computed_reports': __vp_computed, 'monitor_history_raw': []}
print('VEGAPUNK_FLUENT_RESULT=' + repr(__vp_payload))
