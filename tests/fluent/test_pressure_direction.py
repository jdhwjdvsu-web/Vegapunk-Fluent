import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from vegapunk.fluent.capability_registry import CapabilityRegistry
from vegapunk.fluent.mapping_runtime import validate_mapping_spec
from vegapunk.fluent.mapping_schema import MappingSpec
from vegapunk.fluent.parameter_rules import resolve_parameters
from vegapunk.fluent.pressure_direction import probe_code, update_lines, COLLECTION
from vegapunk.fluent.spec import ParameterSpec
from vegapunk.fluent.task_schema import SimulationTaskObject
from .test_agent_v3 import fan_mapping, fan_task, fan_registry


def parameters():
    return [ParameterSpec(name=f'{zone}_{axis}', collection_path=COLLECTION,
                          object_name=zone, property_path=f'momentum.flow_direction_{axis}',
                          unit='dimensionless', minimum=-1, maximum=1)
            for zone in ('left', 'right') for axis in 'xyz']


def values():
    return {f'{zone}_{axis}': value for zone in ('left', 'right')
            for axis, value in zip('xyz', (math.cos(math.pi/6), .5, 0))}


def test_codegen_writes_complete_groups_and_reads_back():
    lines, handled = update_lines(parameters(), values())
    code = '\n'.join(lines)
    compile(code, '<direction>', 'exec')
    assert len(handled) == 6
    assert code.count("direction_specification_method = 'Direction Vector'") == 2
    assert code.count('flow_direction.get_state()') == 4
    assert 'momentum.flow_direction_x =' not in code


def test_missing_or_nonunit_components_fail_before_writes():
    with pytest.raises(ValueError, match='complete XYZ'):
        update_lines(parameters()[:-1], values())
    bad = values()
    bad['right_y'] = .1
    with pytest.raises(ValueError, match='normalized'):
        update_lines(parameters(), bad)
    with pytest.raises(ValueError, match='Duplicate'):
        update_lines(parameters() + parameters()[:1], values())


def test_discovery_and_registry_keep_both_inlets():
    signature = {'observations': [dict(rule_id=f'flow_direction_{axis}', object_name=zone,
                 value=value, editable=True) for zone in ('left', 'right')
                 for axis, value in zip('xyz', (1, 0, 0))]}
    found = {p.key: p for p in resolve_parameters(signature)}
    registry = CapabilityRegistry({}, found, {})
    capability = registry.require('mapped.fan_incidence_2d.v1')
    assert len(found) == len(capability.available_parameter_ids) == 6
    assert capability.executable
    assert registry.get('direct.flow_direction_x.v1') is None
    only_x = {k: p for k, p in found.items() if p.rule_id == 'flow_direction_x'}
    assert not CapabilityRegistry({}, only_x, {}).require(capability.capability_id).executable


def test_two_inlet_mapping_normalizes_each_group_not_combined_vector():
    registry = fan_registry()
    second = {k+'-second': replace(p, key=k+'-second', object_name='second', zone='second')
              for k,p in registry.parameters.items()}
    combined = CapabilityRegistry(registry.profile, {**registry.parameters, **second}, registry.metrics)
    raw = fan_mapping()
    for key in registry.parameters:
        raw['selected_fluent_parameter_ids'].append(key+'-second')
        raw['forward_expression'][key+'-second'] = raw['forward_expression'][key]
        raw['output_units'][key+'-second'] = 'dimensionless'
    task = SimulationTaskObject.model_validate(fan_task())
    assert validate_mapping_spec(MappingSpec.model_validate(raw), task, combined) == []
    raw['forward_expression']['flow-y-id-second'] = {'value': 0}
    assert any('归一化' in e for e in validate_mapping_spec(MappingSpec.model_validate(raw), task, combined))


class Method:
    def __init__(self):
        self.value = 'Normal to Boundary'
    def get_state(self):
        return self.value
    def allowed_values(self):
        return ['Normal to Boundary', 'Direction Vector']


class Vector:
    def get_state(self):
        return [dict(option='value', value=v) for v in (1, 0, 0)]
    def is_active(self):
        return True
    def is_read_only(self):
        return False


class Momentum:
    def __init__(self):
        object.__setattr__(self, 'direction_specification_method', Method())
        object.__setattr__(self, 'flow_direction', Vector())
    def __setattr__(self, key, value):
        if key == 'direction_specification_method':
            self.direction_specification_method.value = value
        elif key == 'flow_direction':
            assert value == [1, 0, 0]
    def get_state(self):
        return {'mode': self.direction_specification_method.get_state()}


def test_probe_restores_mode_and_only_publishes_validated_components():
    momentum = Momentum()
    class Inlets(dict):
        def get_object_names(self):
            return list(self)
    solver = SimpleNamespace(settings=SimpleNamespace(setup=SimpleNamespace(
        boundary_conditions=SimpleNamespace(pressure_inlet=Inlets(left=SimpleNamespace(momentum=momentum))))))
    model = {'warnings': [], 'observations': []}
    exec(probe_code(), {'solver': solver, '__vp_model': model})
    assert momentum.get_state()['mode'] == 'Normal to Boundary'
    assert len(model['observations']) == 3
    assert not model['warnings']


def test_codegen_fails_on_readback_mismatch_before_solver_iteration():
    class BadMomentum:
        def __init__(self):
            object.__setattr__(self, 'flow_direction', Vector())
        def __setattr__(self, name, value):
            pass  # simulate a backend accepting a write but not applying it
    inlets = {zone: SimpleNamespace(momentum=BadMomentum()) for zone in ('left', 'right')}
    solver = SimpleNamespace(settings=SimpleNamespace(setup=SimpleNamespace(
        boundary_conditions=SimpleNamespace(pressure_inlet=inlets))))
    lines, _ = update_lines(parameters(), values())
    with pytest.raises(ValueError, match='readback mismatch'):
        exec('\n'.join(lines), {'solver': solver, '__vp_applied': {}})


def test_failed_probe_never_publishes_components_and_restores_mode():
    class Unwritable(Momentum):
        def __setattr__(self, key, value):
            if key == 'flow_direction':
                raise RuntimeError('write rejected')
            super().__setattr__(key, value)
    class Inlets(dict):
        def get_object_names(self):
            return list(self)
    momentum = Unwritable()
    solver = SimpleNamespace(settings=SimpleNamespace(setup=SimpleNamespace(
        boundary_conditions=SimpleNamespace(pressure_inlet=Inlets(left=SimpleNamespace(momentum=momentum))))))
    model = {'warnings': [], 'observations': []}
    exec(probe_code(), {'solver': solver, '__vp_model': model})
    assert model['observations'] == []
    assert 'write rejected' in model['warnings'][0]
    assert momentum.get_state()['mode'] == 'Normal to Boundary'
