"""Model discovery and task-local plan service used by the existing Web runtime."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from .model_introspection import SCANNER_VERSION, scan_model
from .model_profile import MAX_PARAMETERS, PROFILE_SCHEMA
from .parameter_ranker import rank_parameters
from .parameter_rules import RULE_VERSION, resolve_parameters
from .profile_store import ProfileStore, case_sha256, profile_key


class AdaptiveModels:
    def __init__(self, root: Path, template: dict):
        self.store = ProfileStore(root / "model_library")
        self.template = template
        self.profile: dict | None = None
        self.busy = False

    def catalog(self) -> dict:
        if not self.profile:
            return {}
        candidates = resolve_parameters(self.profile["signature"])
        overrides = self.profile.get("user_overrides", {})
        return {item.key: replace(item, recommended_min=overrides[item.key][0],
                                  recommended_max=overrides[item.key][1])
                if item.key in overrides else item for item in candidates}

    def state(self) -> dict:
        profile = self.profile
        return {
            "status": "scanning" if self.busy else "parsed" if profile else "unparsed",
            "max_parameters": MAX_PARAMETERS,
            "profile": profile,
            "execution_ready": bool(profile and profile.get("contract_confirmed")),
            "notice": "扫描只做参数发现；建议范围需人工确认。新模型的 Objective / Gate 尚待适配。",
        }

    def persist_selection(self, output: Path) -> None:
        output.mkdir(parents=True, exist_ok=True)
        (output / "model_selection.json").write_text(
            json.dumps(self.profile, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    def restore_selection(self, output: Path) -> None:
        path = output / "model_selection.json"
        self.profile = None
        if not path.is_file():
            return
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
            if (profile and profile.get("rule_version") == RULE_VERSION
                    and profile.get("scanner_version") == SCANNER_VERSION
                    and case_sha256(profile["case_file"]) == profile["case_sha256"]):
                self.profile = profile
        except (OSError, ValueError, KeyError, RuntimeError):
            pass

    async def analyze(self, case_file: str, endpoint: str, dimension: int,
                      product_version: str, question: str, force: bool = False) -> tuple[dict, bool]:
        if self.busy:
            raise RuntimeError("模型扫描正在进行")
        self.busy = True
        try:
            digest = await asyncio.to_thread(case_sha256, case_file)
            identity = json.dumps([endpoint, product_version, dimension], sort_keys=True)
            model_id = profile_key(digest, identity)
            previous = self.store.get(model_id)
            # No version pin -> no reuse: an upgraded default Fluent must be rescanned.
            profile = self.store.get(model_id) if product_version and not force else None
            cached = profile is not None
            if not profile:
                kwargs = dict(self.template["connection"]["connect_kwargs"])
                kwargs.update(case_file_name=case_file, dimension=dimension,
                              cleanup_on_exit=True, ui_mode="no_gui")
                kwargs.pop("case_data_file_name", None)
                if product_version:
                    kwargs["product_version"] = product_version
                else:
                    kwargs.pop("product_version", None)
                audit = self.store.root / "scans" / (model_id + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
                signature = await scan_model(endpoint, kwargs, audit)
                if signature.get("dimension") != dimension:
                    raise ValueError("实际模型维度与所选维度不一致，请检查 Case")
                if await asyncio.to_thread(case_sha256, case_file) != digest:
                    raise ValueError("扫描期间 Case 发生变化，请重新分析")
                baseline = self.template["connection"].get("baseline_sha256")
                # Only the audited template baseline may retain its existing objective/Gates.
                profile = {
                    "schema_version": PROFILE_SCHEMA, "model_id": model_id,
                    "case_sha256": digest, "rule_version": RULE_VERSION,
                    "scanner_version": SCANNER_VERSION,
                    "fluent_version": signature["fluent_version"], "product_version": product_version,
                    "dimension": dimension, "endpoint": endpoint, "signature": signature,
                    "signature_sha256": hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                    "user_overrides": {}, "history": [],
                    "contract_confirmed": digest == baseline,
                    "objective": self.template.get("objective") if digest == baseline else None,
                    "gates": self.template.get("optimization") if digest == baseline else None,
                    "audit_dir": str(audit),
                }
            profile["case_file"] = case_file
            profile["contract_confirmed"] = digest == self.template["connection"].get("baseline_sha256")
            profile["name"] = case_file.replace("\\", "/").rsplit("/", 1)[-1]
            candidates = resolve_parameters(profile["signature"])
            if previous and not cached:
                # Preserve only still-valid human overrides after an explicit refresh.
                for item in candidates:
                    bounds = previous.get("user_overrides", {}).get(item.key)
                    if bounds:
                        try:
                            low = item.validate_display_value(bounds[0], "下限")
                            high = item.validate_display_value(bounds[1], "上限")
                            if low < high:
                                profile["user_overrides"][item.key] = [low, high]
                        except (ValueError, IndexError, TypeError):
                            pass
                profile["selected_parameters"] = previous.get("selected_parameters", [])
                profile["history"] = previous.get("history", [])
            profile["parameter_candidates"] = [asdict(item) for item in candidates]
            profile["ranking"] = rank_parameters(candidates, question)
            profile["recommended_parameters"] = [item["key"] for item in profile["ranking"][:2]]
            profile["research_question"] = question
            profile["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.store.save(profile)
            self.profile = profile
            return profile, cached
        finally:
            self.busy = False

    def apply_defaults(self, defaults: dict) -> None:
        if not self.profile:
            defaults["parameters"] = []
            defaults["direct_parameters"] = []
            return
        catalog = self.catalog()
        selected = self.profile.get("selected_parameters") or self.profile["recommended_parameters"]
        selected = [key for key in selected if key in catalog][:MAX_PARAMETERS]
        defaults["case_file"] = self.profile["case_file"]
        defaults["endpoint"] = self.profile["endpoint"]
        defaults["parameters"] = [{"parameter_key": key, "range_min": catalog[key].recommended_min,
                                    "range_max": catalog[key].recommended_max} for key in selected]
        defaults["direct_parameters"] = [{"parameter_key": key, "value": catalog[key].default_value} for key in selected]

    async def validate_run(self, form) -> dict:
        if self.busy:
            raise RuntimeError("请等待模型扫描完成")
        if not self.profile or form.case_file != self.profile["case_file"] or form.endpoint != self.profile["endpoint"]:
            raise ValueError("模型或 MCP 地址已改变，请先分析模型")
        if await asyncio.to_thread(case_sha256, form.case_file) != self.profile["case_sha256"]:
            self.profile = None
            raise ValueError("模型已变化，需要重新分析参数")
        if not self.profile["contract_confirmed"]:
            raise ValueError("参数已解析，但此 Case 的 Objective / Gate 未确认；禁止沿用旧模型的报告和质量门")
        catalog = self.catalog()
        for item in form.parameters:
            if item.parameter_key not in catalog:
                raise ValueError("参数不属于当前模型的可编辑目录")
            parameter = catalog[item.parameter_key]
            if hasattr(item, "value"):
                parameter.validate_display_value(item.value, "参数值")
            else:
                parameter.validate_display_value(item.range_min, "参数下限")
                parameter.validate_display_value(item.range_max, "参数上限")
        return catalog

    def save_ranges(self, parameters: list, output: Path) -> None:
        if not self.profile:
            raise ValueError("请先分析模型")
        catalog = self.catalog()
        overrides = dict(self.profile.get("user_overrides", {}))
        for item in parameters:
            if item.parameter_key not in catalog:
                raise ValueError("参数不属于当前模型")
            parameter = catalog[item.parameter_key]
            overrides[item.parameter_key] = [parameter.validate_display_value(item.range_min, "下限"),
                                              parameter.validate_display_value(item.range_max, "上限")]
        self.profile["user_overrides"] = overrides
        self.profile["selected_parameters"] = [item.parameter_key for item in parameters]
        self.store.save(self.profile)
        self.persist_selection(output)

    def record_run(self, output: Path) -> None:
        if self.profile:
            value = str(output)
            if value not in self.profile["history"]:
                self.profile["history"].append(value)
            self.store.save(self.profile)
            self.persist_selection(output)

    def bind_connection(self, spec):
        """Pin the compute session to the exact scan identity, not template defaults."""
        from .spec import ExperimentSpec
        raw = spec.to_dict()
        raw["connection"]["baseline_sha256"] = self.profile["case_sha256"]
        kwargs = raw["connection"]["connect_kwargs"]
        kwargs["dimension"] = self.profile["dimension"]
        version = self.profile.get("product_version")
        if not version:
            version = self.profile["signature"].get("product_version")
        if not version:
            match = re.search(r"20(\d{2}) R(\d)", self.profile["fluent_version"])
            version = f"{match[1]}.{match[2]}.0" if match else None
        if not version:
            raise ValueError("无法锁定扫描时的 Fluent 版本，请指定版本重新分析")
        if version:
            kwargs["product_version"] = version
        raw["connection"]["reuse_existing_session"] = False
        raw["connection"]["disconnect_on_exit"] = True
        return ExperimentSpec.from_dict(raw)
