"""Frozen legacy fixture for compatibility tests, never used for Case discovery."""
from .model_profile import UIParameter


PARAMETER_CATALOG = (
    UIParameter(
        "cold_inlet_velocity", "冷入口速度", "边界条件", "cold-inlet",
        "setup.boundary_conditions.velocity_inlet", "cold-inlet",
        "momentum.velocity_magnitude.value", "m/s", 0.05, 10.0, 0.4, 1.1,
        1.0, 0.05, "控制冷流体进入弯管的速度。",
    ),
    UIParameter(
        "hot_inlet_velocity", "热入口速度", "边界条件", "hot-inlet",
        "setup.boundary_conditions.velocity_inlet", "hot-inlet",
        "momentum.velocity_magnitude.value", "m/s", 0.05, 10.0, 0.8, 1.6,
        1.2, 0.05, "控制热流体进入弯管的速度。",
    ),
    UIParameter(
        "cold_inlet_temperature", "冷入口温度", "边界条件", "cold-inlet",
        "setup.boundary_conditions.velocity_inlet", "cold-inlet",
        "thermal.temperature.value", "K", 250.0, 450.0, 283.15, 303.15,
        293.15, 0.5, "设置冷入口的静温。",
    ),
    UIParameter(
        "hot_inlet_temperature", "热入口温度", "边界条件", "hot-inlet",
        "setup.boundary_conditions.velocity_inlet", "hot-inlet",
        "thermal.temperature.value", "K", 250.0, 600.0, 303.15, 353.15,
        313.15, 0.5, "设置热入口的静温。",
    ),
    UIParameter(
        "outlet_gauge_pressure", "出口表压", "边界条件", "outlet",
        "setup.boundary_conditions.pressure_outlet", "outlet",
        "momentum.gauge_pressure.value", "Pa", -50000.0, 50000.0,
        -1000.0, 1000.0, 0.0, 100.0, "设置压力出口相对于操作压力的表压。",
    ),
    UIParameter(
        "cold_inlet_turbulence_intensity", "冷入口湍流强度", "湍流", "cold-inlet",
        "setup.boundary_conditions.velocity_inlet", "cold-inlet",
        "turbulence.turbulent_intensity", "%", 0.1, 30.0, 1.0, 10.0,
        5.0, 0.1, "网页使用百分数；提交 Fluent 时自动换算为比例。", 0.01,
    ),
    UIParameter(
        "hot_inlet_turbulence_intensity", "热入口湍流强度", "湍流", "hot-inlet",
        "setup.boundary_conditions.velocity_inlet", "hot-inlet",
        "turbulence.turbulent_intensity", "%", 0.1, 30.0, 1.0, 10.0,
        5.0, 0.1, "网页使用百分数；提交 Fluent 时自动换算为比例。", 0.01,
    ),
    UIParameter(
        "cold_inlet_hydraulic_diameter", "冷入口水力直径", "湍流", "cold-inlet",
        "setup.boundary_conditions.velocity_inlet", "cold-inlet",
        "turbulence.hydraulic_diameter", "m", 0.001, 2.0, 0.025, 0.15,
        0.1016, 0.001, "用于强度–水力直径湍流入口定义。",
    ),
    UIParameter(
        "hot_inlet_hydraulic_diameter", "热入口水力直径", "湍流", "hot-inlet",
        "setup.boundary_conditions.velocity_inlet", "hot-inlet",
        "turbulence.hydraulic_diameter", "m", 0.001, 2.0, 0.01, 0.08,
        0.0254, 0.001, "用于强度–水力直径湍流入口定义。",
    ),
    UIParameter(
        "air_density", "空气密度", "材料物性", "air",
        "setup.materials.fluid", "air", "density.value", "kg/m³",
        0.1, 5000.0, 0.8, 1.5, 1.225, 0.01, "修改当前 Case 中 air 材料的常密度。",
    ),
    UIParameter(
        "air_viscosity", "空气动力黏度", "材料物性", "air",
        "setup.materials.fluid", "air", "viscosity.value", "Pa·s",
        1e-7, 10.0, 1e-5, 3e-5, 1.7894e-5, 1e-6,
        "修改当前 Case 中 air 材料的常动力黏度。", log=True,
    ),
    UIParameter(
        "air_specific_heat", "空气定压比热", "材料物性", "air",
        "setup.materials.fluid", "air", "specific_heat.value", "J/(kg·K)",
        100.0, 10000.0, 900.0, 1200.0, 1006.43, 10.0,
        "修改当前 Case 中 air 材料的常定压比热。",
    ),
    UIParameter(
        "air_thermal_conductivity", "空气导热系数", "材料物性", "air",
        "setup.materials.fluid", "air", "thermal_conductivity.value", "W/(m·K)",
        0.001, 1000.0, 0.015, 0.05, 0.0242, 0.001,
        "修改当前 Case 中 air 材料的常导热系数。", log=True,
    ),
)
PARAMETERS_BY_KEY = {parameter.key: parameter for parameter in PARAMETER_CATALOG}
DEFAULT_PARAMETER_KEY = "cold_inlet_velocity"


def _parameter(key: str) -> UIParameter:
    try:
        return PARAMETERS_BY_KEY[key]
    except KeyError as exc:
        raise ValueError("所选参数不在当前 Case 的受控白名单中") from exc
