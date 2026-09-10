"""Explainable deterministic recommendation; not a claim of LLM reasoning."""
from .model_profile import UIParameter


def rank_parameters(candidates: list[UIParameter], question: str = "") -> list[dict]:
    terms = {
        "velocity": ("流速", "速度", "velocity", "压降", "pressure drop"),
        "temperature": ("温度", "temperature", "传热", "heat"),
        "gauge_pressure": ("压力", "pressure", "压降"),
        "viscosity": ("黏度", "粘度", "viscosity"),
        "density": ("密度", "density"),
    }
    ranked = []
    for item in candidates:
        if not item.editable:
            continue
        score = 60 if item.category == "边界条件" else 35 if item.category == "湍流" else 15
        reasons = ["优先边界条件，谨慎调整材料物性"]
        if item.rule_id == "velocity":
            score += 15
        if any(word in question.lower() for word in terms.get(item.rule_id, ())):
            score += 30
            reasons.append("匹配研究问题关键词")
        if item.recommended_min is None:
            score -= 20
            reasons.append("范围需要人工填写")
        ranked.append({"key": item.key, "score": score, "reasons": reasons})
    return sorted(ranked, key=lambda row: (-row["score"], row["key"]))
