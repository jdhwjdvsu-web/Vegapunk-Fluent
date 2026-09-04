__vp_baseline_case = 'D:/Vegapunk-Fluent/deliverables/mixing_elbow_fluent_model_20260831/mixing_elbow.cas.h5'
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
__vp_point_name = 'direct-cold-inlet-10ms'
__vp_applied = {}
solver.settings.setup.boundary_conditions.velocity_inlet['cold-inlet'].momentum.velocity_magnitude.value = 10.0
__vp_applied['cold_inlet_velocity'] = 10.0
solver.settings.solution.initialization.hybrid_initialize()
solver.settings.solution.run_calculation.iterate(iter_count=200)
__vp_computed = solver.settings.solution.report_definitions.compute(report_defs=['outlet-temp-avg', 'mass-flow-in', 'mass-flow-out'])
__vp_payload = {'name': __vp_point_name, 'parameters': __vp_applied, 'iterations_requested': 200, 'computed_reports': __vp_computed, 'monitor_history_raw': []}
print('VEGAPUNK_FLUENT_RESULT=' + repr(__vp_payload))
__vp_contours = solver.settings.results.graphics.contour
__vp_contours['temperature-contour-direct-run'] = {}
__vp_contour = __vp_contours['temperature-contour-direct-run']
__vp_contour.field = 'temperature'
__vp_contour.surfaces_list = ['symmetry-xyplane']
__vp_contour.filled = True
__vp_contour.node_values = True
__vp_contour.display()
solver.settings.results.graphics.views.auto_scale()
print('VEGAPUNK_CONTOUR=ready')
