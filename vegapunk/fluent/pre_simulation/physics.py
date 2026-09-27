"""Deterministic physical features from verified ModelProfile readbacks."""

from __future__ import annotations

import math

from .schema import (
    EvidenceRef,
    EvidenceStatus,
    FeatureObservation,
    ModelProfile,
    PhysicsFeatures,
    VerificationStatus,
)


_RULE_UNITS = {
    "velocity": "m/s",
    "hydraulic_diameter": "m",
    "density": "kg/m³",
    "viscosity": "Pa·s",
}


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _read(profile: ModelProfile, key: str, rule_id: str | None, unit: str) -> tuple[float | None, EvidenceRef | None]:
    observed = profile.readbacks.get(key)
    if observed is not None and observed.verification == VerificationStatus.VERIFIED and observed.unit == unit:
        number = _finite_positive(observed.value)
        if number is not None:
            return number, EvidenceRef(
                source_type="model_readback", source_id=key, fact=f"{key}={number} {unit}",
                profile_version=profile.profile_version, verification=VerificationStatus.VERIFIED,
            )
    if rule_id is not None:
        matches = [item for item in profile.parameters if item.get("rule_id") == rule_id]
        # Multiple zones are not collapsed into an unjustified single scale.
        if len(matches) == 1 and _RULE_UNITS.get(rule_id) == unit:
            number = _finite_positive(matches[0].get("value"))
            if number is not None:
                return number, EvidenceRef(
                    source_type="model_readback",
                    source_id=str(matches[0].get("object_name") or rule_id),
                    fact=f"{rule_id}={number} {unit}",
                    profile_version=profile.profile_version,
                    verification=VerificationStatus.VERIFIED,
                )
    return None, None


def _observation(
    value: float | None, unit: str, refs: list[EvidenceRef | None], missing: list[str]
) -> FeatureObservation:
    evidence = [item for item in refs if item is not None]
    return FeatureObservation(
        value=value,
        unit=unit,
        evidence=evidence,
        evidence_status=EvidenceStatus.SUPPORTED if value is not None else EvidenceStatus.INSUFFICIENT,
        missing_information=[] if value is not None else missing,
        verification=VerificationStatus.VERIFIED if value is not None else VerificationStatus.UNKNOWN,
    )


class PhysicsFeatureExtractor:
    """Compute only quantities whose inputs and units are directly evidenced."""

    def extract(self, profile: ModelProfile) -> PhysicsFeatures:
        density, density_ref = _read(profile, "density_kg_m3", "density", "kg/m³")
        viscosity, viscosity_ref = _read(profile, "dynamic_viscosity_Pa_s", "viscosity", "Pa·s")
        velocity, velocity_ref = _read(profile, "velocity_m_s", "velocity", "m/s")
        length, length_ref = _read(profile, "hydraulic_diameter_m", "hydraulic_diameter", "m")
        inlet_area, inlet_area_ref = _read(profile, "inlet_area_m2", None, "m²")
        transfer_area, transfer_area_ref = _read(profile, "heat_transfer_area_m2", None, "m²")
        thermal_load, thermal_ref = _read(profile, "thermal_load_W", None, "W")
        if thermal_load is None and len(profile.heat_sources) == 1:
            candidate = profile.heat_sources[0]
            if candidate.get("unit") == "W" and candidate.get("verification") == "VERIFIED":
                thermal_load = _finite_positive(candidate.get("value"))
                if thermal_load is not None:
                    thermal_ref = EvidenceRef(
                        source_type="model_readback", source_id=str(candidate.get("name") or "heat_source"),
                        fact=f"heat source={thermal_load} W", profile_version=profile.profile_version,
                        verification=VerificationStatus.VERIFIED,
                    )
        features = {
            "velocity_scale": _observation(velocity, "m/s", [velocity_ref], ["verified inlet velocity"]),
            "characteristic_length": _observation(length, "m", [length_ref], ["verified hydraulic diameter or characteristic length"]),
            "thermal_load": _observation(thermal_load, "W", [thermal_ref], ["verified heat-source power"]),
            "reynolds_number": _observation(
                density * velocity * length / viscosity
                if None not in (density, velocity, length, viscosity) else None,
                "dimensionless", [density_ref, velocity_ref, length_ref, viscosity_ref],
                [name for name, value in (
                    ("density", density), ("velocity", velocity),
                    ("characteristic length", length), ("dynamic viscosity", viscosity),
                ) if value is None],
            ),
            "mass_flow": _observation(
                density * velocity * inlet_area if None not in (density, velocity, inlet_area) else None,
                "kg/s", [density_ref, velocity_ref, inlet_area_ref],
                [name for name, value in (
                    ("density", density), ("velocity", velocity), ("inlet area", inlet_area),
                ) if value is None],
            ),
            "heat_flux": _observation(
                thermal_load / transfer_area if None not in (thermal_load, transfer_area) else None,
                "W/m²", [thermal_ref, transfer_area_ref],
                [name for name, value in (
                    ("thermal load", thermal_load), ("heat-transfer area", transfer_area),
                ) if value is None],
            ),
        }
        for name, unit in (
            ("prandtl_number", "dimensionless"), ("nusselt_number", "dimensionless"),
            ("peclet_number", "dimensionless"), ("grashof_number", "dimensionless"),
            ("rayleigh_number", "dimensionless"), ("pressure_scale", "Pa"),
            ("thermal_resistance", "K/W"),
        ):
            features[name] = _observation(None, unit, [], [f"{name} inputs and validated correlation unavailable"])
        return PhysicsFeatures(profile_version=profile.profile_version, features=features)
