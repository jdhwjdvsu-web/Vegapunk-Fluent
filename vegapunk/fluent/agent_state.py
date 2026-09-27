"""Persistent LangGraph state contract for one Fluent Web task."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


AgentStatus = Literal[
    "idle",
    "parsing",
    "needs_information",
    "classifying",
    "designing_mapping",
    "reviewing_mapping",
    "human_review_required",
    "geometry_unsupported",
    "awaiting_approval",
    "ready_to_run",
    "running",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "interrupted",
    "stale_response_discarded",
]


class FluentAgentState(TypedDict, total=False):
    thread_id: str
    task_id: str
    conversation_revision: int
    model_id: str
    case_sha256: str
    model_signature_sha256: str
    research_question: str
    original_request: str
    current_intent: str
    plan_revision: int
    planning_attempt_id: str | None
    current_node: str | None
    latest_input_category: str
    effective_reasoning: str
    input_history: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    latest_user_message: str
    task_object: dict[str, Any] | None
    variable_bindings: list[dict[str, Any]]
    mapping_specs: list[dict[str, Any]]
    resolved_task: dict[str, Any] | None
    clarification_questions: list[str]
    validation_errors: list[str]
    mapping_review_issues: list[str]
    active_issues: list[dict[str, Any]]
    issue_history: list[dict[str, Any]]
    geometry_unsupported: list[str]
    geometry_recommendations: list[dict[str, Any]]
    approval_status: str
    approval_fingerprint: str | None
    campaign_id: str | None
    active_trial_id: str | None
    active_job_id: str | None
    solved_trials: int
    completed_trials: int
    feasible_trials: int
    failed_trials: int
    best_result: dict[str, Any] | None
    termination_reason: str | None
    status: AgentStatus
    classification_revision_count: int
    mapping_revision_count: int
    classification_revision_total: int
    mapping_revision_total: int
    agent_message: str
    pending_geometry_recommendation: dict[str, Any] | None
    geometry_handoff_result: dict[str, Any] | None
    result_interpretation_error: str | None


def initial_agent_state(
    *,
    task_id: str,
    conversation_revision: int,
    model_id: str,
    case_sha256: str,
    model_signature_sha256: str,
) -> FluentAgentState:
    return FluentAgentState(
        thread_id=task_id,
        task_id=task_id,
        conversation_revision=conversation_revision,
        model_id=model_id,
        case_sha256=case_sha256,
        model_signature_sha256=model_signature_sha256,
        research_question="",
        original_request="",
        current_intent="",
        plan_revision=0,
        planning_attempt_id=None,
        current_node=None,
        latest_input_category="initial_request",
        effective_reasoning="medium",
        input_history=[],
        messages=[],
        latest_user_message="",
        task_object=None,
        variable_bindings=[],
        mapping_specs=[],
        resolved_task=None,
        clarification_questions=[],
        validation_errors=[],
        mapping_review_issues=[],
        active_issues=[],
        issue_history=[],
        geometry_unsupported=[],
        geometry_recommendations=[],
        approval_status="required",
        approval_fingerprint=None,
        campaign_id=None,
        active_trial_id=None,
        active_job_id=None,
        solved_trials=0,
        completed_trials=0,
        feasible_trials=0,
        failed_trials=0,
        best_result=None,
        termination_reason=None,
        status="idle",
        classification_revision_count=0,
        mapping_revision_count=0,
        classification_revision_total=0,
        mapping_revision_total=0,
        agent_message="",
        pending_geometry_recommendation=None,
        geometry_handoff_result=None,
        result_interpretation_error=None,
    )
