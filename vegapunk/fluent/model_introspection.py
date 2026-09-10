"""Read-only, fixed-shape model scanner executed through validated MCP code."""
from __future__ import annotations

import json
from pathlib import Path

from .direct_run import _call
from .parameter_rules import RULES, collection_path
from .runner import FluentExperimentError

SCANNER_VERSION = "1.0"
MARKER = "VEGAPUNK_MODEL="
BOUNDARIES = ("velocity_inlet", "pressure_inlet", "mass_flow_inlet", "pressure_outlet", "outflow", "wall", "symmetry", "interior", "interface", "periodic")


def build_introspection_code() -> str:
    lines = [
        "import json",
        "__vp_model = {'boundaries': [], 'materials': [], 'physics': {}, 'reports': [], 'observations': [], 'warnings': []}",
    ]

    def probe(statement: str, label: str) -> None:
        lines.extend(["try:", *["    " + row for row in statement.splitlines()],
                      "except Exception as __vp_error:",
                      f"    __vp_model['warnings'].append({label!r} + ': ' + str(__vp_error)[:240])"])

    probe("__vp_model['fluent_version'] = str(solver.get_fluent_version())", "version")
    probe("__vp_model['product_version'] = solver.get_fluent_version().value", "product version")
    probe("__vp_model['solver_type'] = solver.settings.setup.general.solver.type.get_state()", "solver")
    probe("__vp_model['dimension'] = 2 if solver.settings.setup.general.solver.two_dim_space.is_active() else 3", "dimension")
    for key, expression in {
        "energy": "solver.settings.setup.models.energy.enabled.get_state()",
        "turbulence": "solver.settings.setup.models.viscous.model.get_state() not in ('laminar', 'inviscid')",
        "viscous_model": "solver.settings.setup.models.viscous.model.get_state()",
        "species": "solver.settings.setup.models.species.model.get_state()",
        "multiphase": "solver.settings.setup.models.multiphase.model.get_state()",
    }.items():
        probe(f"__vp_model['physics'][{key!r}] = {expression}", key)
    for kind in BOUNDARIES:
        probe(f"for __vp_name in solver.settings.setup.boundary_conditions.{kind}.get_object_names():\n    __vp_model['boundaries'].append({{'name': __vp_name, 'type': {kind!r}}})", kind)
    for kind in ("fluid", "solid"):
        probe(f"for __vp_name in solver.settings.setup.materials.{kind}.get_object_names():\n    __vp_model['materials'].append({{'name': __vp_name, 'type': {kind!r}}})", kind)
    probe("__vp_model['reports'] = solver.settings.solution.report_definitions.get_state()", "reports")
    probe("__vp_model['cell_zones'] = solver.settings.setup.cell_zone_conditions.fluid.get_object_names()", "cell zones")
    probe("__vp_model['physics']['porous_zones'] = [name for name in solver.settings.setup.cell_zone_conditions.fluid.get_object_names() if solver.settings.setup.cell_zone_conditions.fluid[name].porous_zone.porous.get_state() is True]", "porous media")
    for rule in RULES:
        base = f"solver.settings.{collection_path(rule)}[__vp_name]"
        node = f"{base}.{rule.path}"
        checks = f"{node}.is_active() and not {node}.is_read_only()"
        if rule.option_path:
            checks += f" and {base}.{rule.option_path}.get_state() in ('constant', 'value')"
        statement = f"for __vp_name in solver.settings.{collection_path(rule)}.get_object_names():\n"
        statement += "    try:\n"
        statement += f"        if {checks}:\n"
        statement += f"            __vp_model['observations'].append({{'rule_id': {rule.id!r}, 'object_name': __vp_name, 'value': {node}.get_state(), 'native_min': {node}.min(), 'native_max': {node}.max(), 'editable': True}})\n"
        statement += "    except Exception as __vp_error:\n"
        statement += f"        __vp_model['warnings'].append({rule.id!r} + ':' + __vp_name + ': ' + str(__vp_error)[:160])"
        probe(statement, rule.id)
    lines.append(f"print({MARKER!r} + json.dumps(__vp_model, allow_nan=False))")
    return "\n".join(lines) + "\n"


async def scan_model(endpoint: str, connect_kwargs: dict, audit_dir: Path) -> dict:
    from fastmcp import Client

    audit_dir.mkdir(parents=True, exist_ok=True)
    code = build_introspection_code()
    (audit_dir / "introspection.py").write_text(code, encoding="utf-8")
    owned = False
    async with Client(endpoint, timeout=600) as client:
        status = await _call(client, "session_status", {})
        if status.get("connected"):
            raise FluentExperimentError("MCP 已有活动会话；扫描不会接管或关闭它")
        try:
            await _call(client, "connect", {"connect_kwargs": connect_kwargs})
            owned = True
            validation = await _call(client, "validate_code", {"code": code})
            (audit_dir / "validation.json").write_text(json.dumps(validation, ensure_ascii=False), encoding="utf-8")
            if validation.get("status") != "ok":
                raise FluentExperimentError("模型扫描代码未通过 MCP 验证")
            result = await _call(client, "run_code", {"code": code})
            stdout = str(result.get("stdout", ""))
            (audit_dir / "stdout.log").write_text(stdout, encoding="utf-8")
            for line in reversed(stdout.splitlines()):
                if line.startswith(MARKER):
                    signature = json.loads(line[len(MARKER):])
                    if not signature.get("fluent_version") or not signature.get("boundaries"):
                        raise FluentExperimentError("模型扫描缺少版本或边界信息，拒绝生成可执行目录")
                    return signature
            raise FluentExperimentError("MCP 未返回结构化模型扫描结果")
        finally:
            if owned:
                await _call(client, "disconnect", {})
