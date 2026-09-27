import copy
from dataclasses import replace
import pytest
from vegapunk.fluent.thermal_guard import validate_thermal_guard, evaluate_thermal_guard, verify_thermal_data
from vegapunk.fluent.spec import SolverSpec, SpecError
from vegapunk.fluent.validity import evaluate_result_gate
from .test_validity import optimization_spec

def guard():
    return dict(data_file='D:/fixed.dat.h5',data_sha256='0'*64,heat_source_W=100,
                heat_net_report='heat',mass_in_report='in',mass_out_report='out',temperature_reports=['max','avg'],
                window=3,temperature_span_K=.1,heat_relative_tolerance=.01,mass_relative_tolerance=.001,pseudo_courant=1)
def history():return [{'heat':[.05,'W'],'in':[.05,'kg/s'],'out':[-.05,'kg/s'],'max':[328.,'K'],'avg':[327.,'K']} for _ in range(3)]
def stdout():return '\n'.join(f'{i} 0.0001 0.0001 0.0001 0.0001 1e-7 0.0001 0.0001 0:00:00 0' for i in range(1,4))
def test_net_already_includes_heat_source():
    r=evaluate_thermal_guard(guard(),history(),stdout(),{'continuity':.001,'energy':1e-6})
    assert r['passed'] and r['heat_error_max']==.0005
@pytest.mark.parametrize('mutation',['missing','nan','temperature','heat','mass','residual'])
def test_fail_closed(mutation):
    h=history();s=stdout()
    if mutation=='missing':h=h[:-1]
    if mutation=='nan':h[0]['heat'][0]=float('nan')
    if mutation=='temperature':h[0]['max'][0]+=1
    if mutation=='heat':h[0]['heat'][0]=2
    if mutation=='mass':h[0]['out'][0]=-.04
    if mutation=='residual':s=s.replace('1 0.0001','1 0.01')
    assert not evaluate_thermal_guard(guard(),h,s,{'continuity':.001})['passed']
def test_contract_validation_and_source_pin(tmp_path):
    g=guard();g['window']=True
    with pytest.raises(ValueError):validate_thermal_guard(g)
    g=guard();g['data_file']=str(tmp_path/'a.dat.h5');(tmp_path/'a.dat.h5').write_bytes(b'changed')
    with pytest.raises(ValueError):verify_thermal_data(g)
    with pytest.raises(SpecError):SolverSpec.from_dict(dict(initialization='hybrid',iterations=10,residual_thresholds={'continuity':.001},thermal_guard=guard()))
def test_missing_guard_never_ranks_candidate():
    spec=optimization_spec()
    spec=replace(spec,solver=replace(spec.solver,thermal_guard=guard()))
    r=evaluate_result_gate(spec,dict(status='completed',objective_value=300,mass_flow_in=1,mass_flow_out=1))
    assert not r.passed and any(c['name']=='thermal_window' and not c['passed'] for c in r.checks)

def test_v3_resolves_only_paired_data_and_preserves_guard(tmp_path):
    import hashlib
    from vegapunk.fluent.task_schema import SolverRequirements
    from vegapunk.fluent.experiment_orchestrator import compile_resolved_experiment
    from .test_experiment_orchestrator_v3 import executable_fan_case
    from .test_planning import profile
    case_path=tmp_path/'model.cas.h5';case_path.write_bytes(b'case')
    data_path=tmp_path/'model.dat.h5';data_path.write_bytes(b'approved-data')
    g=guard();g['data_file']='paired-case-data';g['data_sha256']=hashlib.sha256(b'approved-data').hexdigest()
    registry,resolved,template=executable_fan_case()
    resolved.task.solver_requirements=SolverRequirements(initialization='none',iterations=10,residual_thresholds={'continuity':.001},mass_balance_required=False,thermal_guard=g)
    p=profile();p['case_file']=str(case_path)
    p['signature']['reports']={
        'flux': {name: {'name': name, 'report_type': 'flux'} for name in ('heat','in','out')},
        'volume': {name: {'name': name, 'report_type': 'volume-max'} for name in ('max','avg')},
    }
    exp=compile_resolved_experiment(template,p,resolved,registry,'http://127.0.0.1:18000/mcp')
    assert exp.native_spec.solver.thermal_guard['data_file']==str(data_path)
    assert exp.semantic_spec.solver.thermal_guard==exp.native_spec.solver.thermal_guard
    assert exp.native_spec.solver.thermal_guard['data_sha256']==g['data_sha256']
    with pytest.raises(ValueError):SolverRequirements(thermal_guard={**g,'data_file':'D:/arbitrary.dat.h5'})

    wrong = copy.deepcopy(g); wrong['data_sha256']='0'*64
    resolved.task.solver_requirements=SolverRequirements(initialization='none',iterations=10,residual_thresholds={'continuity':.001},mass_balance_required=False,thermal_guard=wrong)
    with pytest.raises(ValueError, match='SHA256'):
        compile_resolved_experiment(template,p,resolved,registry,'http://127.0.0.1:18000/mcp')

def test_absent_guard_keeps_legacy_serialization():
    from vegapunk.fluent.task_schema import SolverRequirements
    assert 'thermal_guard' not in SolverRequirements().model_dump()
    assert 'thermal_guard' not in optimization_spec().to_dict()['solver']

def test_v3_failed_candidates_consume_total_budget(tmp_path):
    import asyncio
    from vegapunk.fluent.optimizer import run_optimization
    class RejectedRunner:
        calls=0
        def __init__(self,spec,path):pass
        async def open_session(self):pass
        async def close_session(self):pass
        async def evaluate_point(self,parameters,**kwargs):
            type(self).calls+=1
            return dict(status='completed',objective_value=300,objective_unit='K',mass_flow_in=1,mass_flow_out=1,
                        convergence={'required':True,'passed':False},reports={},
                        parameters=dict(parameters),baseline_reloaded=True)
    spec=optimization_spec()
    spec=replace(spec,execution_contract={'task':{'termination':{'max_trials':2}}},
                 optimization=replace(spec.optimization,target_trials=2,maximum_failed_trials=10))
    summary=asyncio.run(run_optimization(spec,tmp_path,runner_factory=RejectedRunner))
    assert RejectedRunner.calls==2
    assert summary['completed_trials']==0 and summary['termination_reason']=='max_trials_reached'
