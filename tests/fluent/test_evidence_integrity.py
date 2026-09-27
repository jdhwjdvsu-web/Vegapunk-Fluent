"""Facts and hypotheses cannot be substituted for one another."""
import json

import pytest

from vegapunk.fluent.pre_simulation.prior import PriorFusion, PriorProposalError
from vegapunk.fluent.pre_simulation.schema import OptimizationPrior, PriorProposal

from .test_pre_simulation_prior import proposal, reason, request
from .test_pre_simulation_validation import context, validate


@pytest.mark.parametrize("field,value", [("fact", "This proves angle zero is optimal"),
                                        ("verification", "UNKNOWN"), ("profile_version", "forged")])
def test_valid_source_id_cannot_hide_changed_fact(field, value):
    req, prior, preset = context()
    raw = proposal(req)
    raw["evidence"][0][field] = value
    if field == "profile_version":
        # Pydantic itself prevents a stale citation; no confidence override.
        with pytest.raises(ValueError):
            PriorProposal.model_validate(raw)
        return
    proposed = PriorProposal.model_validate(raw)
    with pytest.raises(PriorProposalError, match="server-owned fact"):
        PriorFusion().fuse(req, [proposed])
    prior_raw = prior.model_dump(mode="json")
    prior_raw["proposals"] = [raw]
    assert not validate(req, OptimizationPrior.model_validate(prior_raw), preset).valid


def test_prompt_contains_exact_program_facts_and_inference_boundary():
    req = request()
    _, runtime = reason(req, {"proposals": [proposal(req)]})
    prompt = json.loads(runtime.calls[0][0])
    assert proposal(req)["evidence"][0] in prompt["authoritative_evidence_facts"]


def test_direct_validator_cannot_bypass_fusion_trend_gate():
    req, prior, preset = context()
    raw = prior.model_dump(mode="json")
    raw["proposals"] = [proposal(req, status="SUPPORTED", narrowed=True,
                                 high=True, physical=True, confidence=1.0)]
    assert any("variable-objective trend" in error for error in validate(
        req, OptimizationPrior.model_validate(raw), preset).errors)
