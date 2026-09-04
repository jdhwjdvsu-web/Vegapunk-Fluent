solver.settings.file.read_case(file_name='D:\\Vegapunk-Fluent\\deliverables\\mixing_elbow_fluent_model_20260831\\mixing_elbow.cas.h5')
__vp_inlet = solver.settings.setup.boundary_conditions.velocity_inlet['cold-inlet']
__vp_inlet.momentum.velocity_magnitude.value = 10.0
solver.settings.solution.initialization.hybrid_initialize()
solver.settings.solution.run_calculation.iterate(iter_count=200)
__vp_contours = solver.settings.results.graphics.contour
__vp_contours['temperature-contour-cold-inlet-10ms'] = {}
__vp_contour = __vp_contours['temperature-contour-cold-inlet-10ms']
__vp_contour.field = 'temperature'
__vp_contour.surfaces_list = ['symmetry-xyplane']
__vp_contour.filled = True
__vp_contour.node_values = True
__vp_contour.display()
solver.settings.results.graphics.views.auto_scale()
__vp_picture = solver.settings.results.graphics.picture
__vp_picture.use_window_resolution = False
__vp_picture.x_resolution = 1600
__vp_picture.y_resolution = 1000
__vp_picture.save_picture(file_name='D:\\Vegapunk-Fluent\\runs\\fluent_high_velocity_10ms\\temperature_contour_cold_inlet_10ms.png')
print('VEGAPUNK_CONTOUR=ok')
