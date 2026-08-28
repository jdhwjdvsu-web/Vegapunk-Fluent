__vp_baseline_case = 'C:\\path\\to\\mixing_elbow.cas.h5'
solver.settings.file.read_case(file_name=__vp_baseline_case)
__vp_report_defs = solver.settings.solution.report_definitions
__vp_collection = __vp_report_defs.surface
__vp_collection['outlet-temp-avg'] = {}
__vp_report = __vp_collection['outlet-temp-avg']
__vp_report.report_type = 'surface-areaavg'
__vp_report.field = 'temperature'
__vp_report.surface_names = ['outlet']
__vp_collection = __vp_report_defs.flux
__vp_collection['mass-flow-in'] = {}
__vp_report = __vp_collection['mass-flow-in']
__vp_report.boundaries = ['cold-inlet', 'hot-inlet']
__vp_collection = __vp_report_defs.flux
__vp_collection['mass-flow-out'] = {}
__vp_report = __vp_collection['mass-flow-out']
__vp_report.boundaries = ['outlet']
__vp_point_name = 'trial-0005'
__vp_applied = {}
solver.settings.setup.boundary_conditions.velocity_inlet['cold-inlet'].momentum.velocity_magnitude.value = 0.7976370241110938
__vp_applied['cold_inlet_velocity'] = 0.7976370241110938
solver.settings.solution.initialization.hybrid_initialize()
solver.settings.solution.run_calculation.iterate(iter_count=100)
__vp_computed = solver.settings.solution.report_definitions.compute(report_defs=['outlet-temp-avg', 'mass-flow-in', 'mass-flow-out'])
__vp_payload = {'name': __vp_point_name, 'parameters': __vp_applied, 'iterations_requested': 100, 'computed_reports': __vp_computed}
print('VEGAPUNK_FLUENT_RESULT=' + repr(__vp_payload))
