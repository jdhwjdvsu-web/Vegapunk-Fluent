"""Atomic inlet-direction adapter with optional aerodynamic report coupling.

The historical module name is retained for compatibility. All generated
Fluent paths are selected from this fixed server-side table.
"""
from __future__ import annotations

import math


COLLECTION = "setup.boundary_conditions.pressure_inlet"
VELOCITY_COLLECTION = "setup.boundary_conditions.velocity_inlet"
PATHS = {f"momentum.flow_direction_{axis}": index for index, axis in enumerate("xyz")}
_CONFIG = {
    COLLECTION: ("direction_specification_method", "Direction Vector"),
    VELOCITY_COLLECTION: ("velocity_specification_method", "Magnitude and Direction"),
}


def _probe_collection_code(collection_path: str, method_attr: str, method_value: str) -> str:
    collection_name = collection_path.rsplit(".", 1)[-1]
    return f'''try:
    __vp_direction_collection = solver.settings.{collection_path}
except Exception:
    __vp_direction_collection = None
if __vp_direction_collection is not None:
    for __vp_name in __vp_direction_collection.get_object_names():
        __vp_momentum = __vp_direction_collection[__vp_name].momentum
        __vp_before = __vp_momentum.get_state()
        __vp_pending = []
        try:
            __vp_method_node = __vp_momentum.{method_attr}
            __vp_method = __vp_method_node.get_state()
            if {method_value!r} not in __vp_method_node.allowed_values():
                raise ValueError("required direction mode is unavailable")
            __vp_momentum.{method_attr} = {method_value!r}
            if __vp_momentum.flow_direction.is_active() and not __vp_momentum.flow_direction.is_read_only():
                __vp_raw = __vp_momentum.flow_direction.get_state()
                __vp_vector = []
                for __vp_item in __vp_raw:
                    if isinstance(__vp_item, dict):
                        if __vp_item.get("option") != "value":
                            raise ValueError("nonconstant inlet direction")
                        __vp_item = __vp_item["value"]
                    if isinstance(__vp_item, bool) or not isinstance(__vp_item, (int, float)):
                        raise ValueError("nonnumeric inlet direction")
                    __vp_vector.append(float(__vp_item))
                __vp_norm = sum(v*v for v in __vp_vector) ** 0.5
                if len(__vp_vector) not in (2, 3) or __vp_norm <= 1e-12 or abs(__vp_norm - 1.0) > 0.001:
                    raise ValueError("inlet direction must be a unit 2D/3D vector")
                __vp_momentum.flow_direction = __vp_vector
                if __vp_momentum.flow_direction.get_state() != __vp_raw:
                    raise ValueError("inlet direction readback mismatch")
                __vp_xyz = ([v / __vp_norm for v in __vp_vector] + [0.0])[:3]
                for __vp_axis, __vp_value in zip("xyz", __vp_xyz):
                    __vp_pending.append({{"rule_id": "flow_direction_" + __vp_axis, "collection": {collection_name!r}, "object_name": __vp_name, "value": __vp_value, "native_min": -1, "native_max": 1, "editable": True}})
        except Exception as __vp_error:
            __vp_model["warnings"].append({collection_name!r} + "_direction:" + __vp_name + ": " + str(__vp_error)[:160])
        finally:
            try:
                __vp_momentum.{method_attr} = __vp_method
            except Exception:
                pass
        if __vp_momentum.get_state() != __vp_before:
            raise ValueError({collection_name!r} + " direction probe failed to restore original state")
        __vp_model["observations"].extend(__vp_pending)
'''


def probe_code() -> str:
    """Probe pressure and velocity inlet directions, restoring changed modes."""

    return "".join(
        _probe_collection_code(path, method_attr, method_value)
        for path, (method_attr, method_value) in _CONFIG.items()
    )


def read_only_probe_code() -> str:
    """Observe an already-active vector mode without switching or restoring it.

    This is intentionally weaker than the legacy disposable-session probe:
    an inactive mode is UNVERIFIED, never made active by writing to Fluent.
    """
    blocks = []
    for collection_path, (method_attr, method_value) in _CONFIG.items():
        collection_name = collection_path.rsplit(".", 1)[-1]
        blocks.append(f'''try:
    __vp_direction_collection = solver.settings.{collection_path}
    for __vp_name in __vp_direction_collection.get_object_names():
        try:
            __vp_momentum = __vp_direction_collection[__vp_name].momentum
            __vp_method = __vp_momentum.{method_attr}.get_state()
            if __vp_method != {method_value!r}:
                __vp_model["warnings"].append({collection_name!r} + ":" + __vp_name + ":UNVERIFIED:direction mode not active")
                continue
            if not __vp_momentum.flow_direction.is_active() or __vp_momentum.flow_direction.is_read_only():
                __vp_model["warnings"].append({collection_name!r} + ":" + __vp_name + ":UNVERIFIED:direction is not readable/editable")
                continue
            __vp_raw = __vp_momentum.flow_direction.get_state()
            __vp_vector = []
            for __vp_item in __vp_raw:
                if isinstance(__vp_item, dict):
                    if __vp_item.get("option") != "value":
                        raise ValueError("nonconstant inlet direction")
                    __vp_item = __vp_item["value"]
                if isinstance(__vp_item, bool) or not isinstance(__vp_item, (int, float)):
                    raise ValueError("nonnumeric inlet direction")
                __vp_vector.append(float(__vp_item))
            __vp_norm = sum(v*v for v in __vp_vector) ** 0.5
            if len(__vp_vector) not in (2, 3) or __vp_norm <= 1e-12 or abs(__vp_norm - 1.0) > 0.001:
                raise ValueError("inlet direction must be a unit 2D/3D vector")
            __vp_xyz = (__vp_vector + [0.0])[:3]
            for __vp_axis, __vp_value in zip("xyz", __vp_xyz):
                __vp_model["observations"].append({{"rule_id": "flow_direction_" + __vp_axis, "collection": {collection_name!r}, "object_name": __vp_name, "value": __vp_value, "native_min": -1, "native_max": 1, "editable": True}})
        except Exception as __vp_error:
            __vp_model["warnings"].append({collection_name!r} + ":" + __vp_name + ":UNVERIFIED:" + str(__vp_error)[:160])
except Exception as __vp_error:
    __vp_model["warnings"].append({collection_name!r} + ":UNKNOWN:" + str(__vp_error)[:160])
''')
    return "".join(blocks)


def _aerodynamic_reports(reports) -> tuple[list[str], list[str]]:
    lift: list[str] = []
    drag: list[str] = []
    for report in reports or ():
        if getattr(report, "kind", None) == "derived_ratio":
            if getattr(report, "numerator_report", None):
                lift.append(report.numerator_report)
            if getattr(report, "denominator_report", None):
                drag.append(report.denominator_report)
            continue
        if getattr(report, "kind", None) != "existing":
            continue
        report_type = str(getattr(report, "report_type", "") or "").lower()
        if report_type == "lift":
            lift.append(report.name)
        elif report_type == "drag":
            drag.append(report.name)
    return lift, drag


def update_lines(parameters, values, reports=()) -> tuple[list[str], set[str]]:
    """Validate complete groups before writes and emit write/readback code."""

    groups = {}
    handled = set()
    for parameter in parameters:
        if parameter.property_path not in PATHS:
            continue
        if parameter.collection_path not in _CONFIG:
            raise ValueError("Direction adapter requires pressure_inlet or velocity_inlet")
        key = (parameter.collection_path, parameter.object_name)
        group = groups.setdefault(key, {})
        axis = PATHS[parameter.property_path]
        if axis in group:
            raise ValueError("Duplicate inlet direction component")
        group[axis] = (
            parameter.name,
            parameter.validate_value(values[parameter.name], parameter.name),
        )
        handled.add(parameter.name)

    lift_reports, drag_reports = _aerodynamic_reports(reports)
    if (lift_reports or drag_reports) and len(groups) > 1:
        raise ValueError("Aerodynamic report coupling requires exactly one inlet direction group")

    lines = []
    for (collection_path, zone), group in groups.items():
        if set(group) != {0, 1, 2}:
            raise ValueError("Inlet direction requires complete XYZ components per inlet")
        vector = [group[index][1] for index in range(3)]
        if not math.isclose(sum(v * v for v in vector), 1, abs_tol=1e-6, rel_tol=0):
            raise ValueError("Inlet direction must be normalized per inlet")
        method_attr, method_value = _CONFIG[collection_path]
        lines.extend([
            f"__vp_direction_momentum = solver.settings.{collection_path}[{zone!r}].momentum",
            f"__vp_direction_momentum.{method_attr} = {method_value!r}",
            "__vp_direction_before = __vp_direction_momentum.flow_direction.get_state()",
            "__vp_direction_size = len(__vp_direction_before)",
            "if __vp_direction_size not in (2, 3):",
            "    raise ValueError('Unsupported Fluent direction vector size')",
            f"__vp_direction_target = {vector!r}[:__vp_direction_size]",
            f"if __vp_direction_size == 2 and abs({vector[2]!r}) > 1e-9:",
            "    raise ValueError('2D Fluent direction requires zero Z component')",
            "__vp_direction_momentum.flow_direction = __vp_direction_target",
            "__vp_direction_readback = __vp_direction_momentum.flow_direction.get_state()",
            "__vp_direction_actual = [float(v['value']) if isinstance(v, dict) and v.get('option') == 'value' else float(v) for v in __vp_direction_readback]",
            "if len(__vp_direction_actual) != __vp_direction_size or any(abs(a-b) > 1e-9 for a,b in zip(__vp_direction_actual, __vp_direction_target)):",
            "    raise ValueError('Inlet direction readback mismatch')",
            "if __vp_direction_size == 2:",
            "    __vp_direction_actual.append(0.0)",
        ])
        for index in range(3):
            lines.append(f"__vp_applied[{group[index][0]!r}] = __vp_direction_actual[{index}]")

        # 2D drag follows the inlet vector; lift is its positive 90° rotation.
        # Preserve any Fluent-specific unused tail component already present.
        for kind, names, xy in (
            ("drag", drag_reports, ("__vp_direction_actual[0]", "__vp_direction_actual[1]")),
            ("lift", lift_reports, ("-__vp_direction_actual[1]", "__vp_direction_actual[0]")),
        ):
            for name in names:
                lines.extend([
                    f"__vp_force_report = solver.settings.solution.report_definitions.{kind}[{name!r}]",
                    "__vp_force_before = list(__vp_force_report.force_vector.get_state())",
                    f"__vp_force_target = [{xy[0]}, {xy[1]}] + __vp_force_before[2:]",
                    "__vp_force_report.force_vector = __vp_force_target",
                    "__vp_force_actual = list(__vp_force_report.force_vector.get_state())",
                    "if len(__vp_force_actual) != len(__vp_force_target) or any(abs(float(a)-float(b)) > 1e-9 for a,b in zip(__vp_force_actual, __vp_force_target)):",
                    "    raise ValueError('Aerodynamic force-vector readback mismatch')",
                ])
    return lines, handled
