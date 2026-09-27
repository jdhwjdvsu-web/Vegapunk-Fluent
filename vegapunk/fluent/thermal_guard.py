"""Opt-in, fail-closed thermal continuation contract for CHT campaigns."""
from __future__ import annotations
import hashlib
import math
import re
from pathlib import Path

FIELDS={'data_file','data_sha256','heat_source_W','heat_net_report','mass_in_report','mass_out_report','temperature_reports','window','temperature_span_K','heat_relative_tolerance','mass_relative_tolerance','pseudo_courant'}
def validate_thermal_guard(raw):
    if raw is None:return None
    if not isinstance(raw,dict) or set(raw)!=FIELDS:raise ValueError('thermal_guard requires exactly the documented fields')
    v=dict(raw)
    for k in ['data_file','heat_net_report','mass_in_report','mass_out_report']:
        if not isinstance(v[k],str) or not v[k].strip():raise ValueError(k+' must be nonempty')
    if not str(v['data_file']).endswith('.dat.h5'):raise ValueError('thermal data must be .dat.h5')
    if not isinstance(v['data_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',v['data_sha256']):raise ValueError('invalid data SHA256')
    if not isinstance(v['temperature_reports'],list) or len(v['temperature_reports'])!=2 or any(not isinstance(x,str) or not x for x in v['temperature_reports']) or len(set(v['temperature_reports']))!=2:raise ValueError('two distinct temperature reports required')
    if type(v['window']) is not int or not 2<=v['window']<=1000:raise ValueError('invalid thermal window')
    for k in ['heat_source_W','temperature_span_K','heat_relative_tolerance','mass_relative_tolerance','pseudo_courant']:
        if isinstance(v[k],bool) or not isinstance(v[k],(int,float)) or not math.isfinite(v[k]) or v[k]<=0:raise ValueError('invalid '+k)
    return v

def verify_thermal_data(g):
    if hashlib.sha256(Path(g['data_file']).read_bytes()).hexdigest()!=g['data_sha256']:raise ValueError('Thermal baseline data fingerprint mismatch')

def setup_lines(g):
    # Fixed code, never arbitrary LLM-authored solver code.
    return [f"solver.settings.file.read_data(file_name={g['data_file']!r})",
        "solver.settings.solution.methods.pseudo_time_method.formulation.segregated_solver = 'local-time-step'",
        f"solver.settings.solution.controls.pseudo_time_method_local_time_step.pseudo_time_courant_number = {g['pseudo_courant']!r}",
        "for __vp_eq in solver.settings.solution.monitor.residual.equations.get_object_names():",
        "    solver.settings.solution.monitor.residual.equations[__vp_eq].check_convergence = False",
        "for __vp_file in solver.settings.solution.monitor.report_files.get_object_names():",
        "    solver.settings.solution.monitor.report_files[__vp_file].active = False"]

def solve_lines(g,iterations):
    names=[g['heat_net_report'],g['mass_in_report'],g['mass_out_report']]+g['temperature_reports']
    return ["__vp_thermal_history_raw = []",
        f"solver.settings.solution.run_calculation.iterate(iter_count={iterations-g['window']})",
        f"for __vp_sample in range({g['window']}):",
        "    solver.settings.solution.run_calculation.iterate(iter_count=1)",
        "    __vp_thermal_history_raw.append(solver.settings.solution.report_definitions.compute("+f"report_defs={names!r}))"]

def evaluate_thermal_guard(g,history,stdout,thresholds):
    from .codegen import normalize_computed_reports
    names={g['heat_net_report'],g['mass_in_report'],g['mass_out_report'],*g['temperature_reports']}
    result={'required':True,'passed':False,'samples':0}
    try:
        rows=[{k:x['value'] for k,x in normalize_computed_reports(v,names).items()} for v in history]
        if len(rows)!=g['window'] or not all(math.isfinite(v) for r in rows for v in r.values()):return result
        # For flux-heattransfer compute(), Net includes the volumetric source.
        heat=max(abs(r[g['heat_net_report']])/g['heat_source_W'] for r in rows)
        mass=max(abs(r[g['mass_in_report']]+r[g['mass_out_report']])/max(abs(r[g['mass_in_report']]),abs(r[g['mass_out_report']]),1e-15) for r in rows)
        spans={k:max(r[k] for r in rows)-min(r[k] for r in rows) for k in g['temperature_reports']}
        residuals={}
        equations=['continuity','x-velocity','y-velocity','z-velocity','energy','k','omega']
        for line in stdout.splitlines():
            p=line.split()
            if len(p)>=9 and p[0].isdigit() and ':' in p[8]:
                try:residuals[int(p[0])]=dict(zip(equations,map(float,p[1:8])))
                except ValueError:pass
        tail=[residuals[i] for i in sorted(residuals)[-g['window']:]]
        residual_pass=len(tail)==g['window'] and bool(thresholds) and all(all(k in r and math.isfinite(r[k]) and r[k]<=lim for k,lim in thresholds.items()) for r in tail)
        result.update(samples=len(rows),heat_error_max=heat,mass_error_max=mass,temperature_spans_K=spans,residual_window_pass=residual_pass,
                      passed=residual_pass and heat<=g['heat_relative_tolerance'] and mass<=g['mass_relative_tolerance'] and max(spans.values())<=g['temperature_span_K'])
    except (KeyError,TypeError,ValueError,OverflowError) as e:result['error']=str(e)
    return result
