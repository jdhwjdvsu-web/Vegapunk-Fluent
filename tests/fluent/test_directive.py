import pytest

from vegapunk.fluent.directive import OptimizationDirective


def directive_data():
    return {
        "parent_campaign_id": "campaign-a",
        "objective": {"report": "outlet-temp", "direction": "minimize"},
        "search_space": [{"name": "velocity", "minimum": 0.4, "maximum": 1.1}],
        "constraints": [],
        "budget": 20,
        "action": "increase_budget",
        "rationale": "Add evidence without changing the scientific contract",
    }


def test_directive_requires_explicit_human_approval():
    directive = OptimizationDirective.from_dict(directive_data())
    with pytest.raises(PermissionError, match="human approval"):
        directive.assert_approved()
    approved = directive.approve("local-user")
    approved.assert_approved()
    assert approved.approved_at


def test_directive_rejects_unknown_action():
    raw = directive_data()
    raw["action"] = "fully_autonomous"
    with pytest.raises(ValueError, match="action"):
        OptimizationDirective.from_dict(raw)
