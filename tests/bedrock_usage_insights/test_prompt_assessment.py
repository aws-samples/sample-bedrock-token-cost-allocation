"""Example-based tests for prompt-quality assessment and privacy integration."""

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from bedrock_usage_insights.models import (
    AnalysisScope,
    CategoryDefinition,
    ContentAnalysisPolicy,
    ContentAnalysisState,
    InsightConfiguration,
    InvocationRecord,
    PromptAssessmentStatus,
    PromptDimension,
    PromptDimensionConfiguration,
    RedactionStatus,
    SensitiveContent,
)
from bedrock_usage_insights.privacy import (
    ContentAccessState,
    DefaultPrivacyGate,
    PrivacyBoundary,
)
from bedrock_usage_insights.prompt_assessment import DeterministicPromptAssessor
from bedrock_usage_insights.signals import extract_signals

UTC = timezone.utc
NOW = datetime(2026, 2, 3, 4, 5, tzinfo=UTC)
ACCOUNT = "123456789012"
REGION = "us-east-1"
POLICY = "prompt-quality-policy-v1"
DIMENSIONS = tuple(dimension.value for dimension in PromptDimension)


def make_configuration(**changes) -> InsightConfiguration:
    values = {
        "configuration_version": "prompt-quality-v1",
        "effective_at": datetime(2026, 1, 1, tzinfo=UTC),
        "author_or_service": "prompt-quality-tests",
        "change_reason": "prompt-quality examples",
        "analysis_scope": AnalysisScope(
            accounts=(ACCOUNT,),
            regions=(REGION,),
            models=("model",),
            operations=("InvokeModel",),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        ),
        "enabled": True,
        "content_analysis": ContentAnalysisPolicy(
            state=ContentAnalysisState.ENABLED,
            approved_accounts=(ACCOUNT,),
            approved_regions=(REGION,),
            access_policy_reference=POLICY,
            retention_days=30,
            max_preview_length=256,
        ),
        "prompt_dimensions": {
            dimension: PromptDimensionConfiguration(enabled=True, threshold=0.5)
            for dimension in DIMENSIONS
        },
    }
    values.update(changes)
    return InsightConfiguration(**values)


def make_record(**changes) -> InvocationRecord:
    values = {
        "source_request_id": "request-1",
        "source_timestamp_utc": NOW,
        "source_account": ACCOUNT,
        "source_region": REGION,
        "model_id": "model",
        "operation": "InvokeModel",
        "analysis_run_id": "run-1",
        "configuration_version": "prompt-quality-v1",
        "analysis_logic_version": "1.0",
        "input_token_count": 12,
        "output_token_count": 20,
        "request_metadata": {"team": "platform"},
        "sensitive_content": SensitiveContent(
            prompt_preview=(
                "Analyze the provided data using the background context. "
                "Return a JSON table with the requested fields."
            ),
            response_preview="response",
        ),
    }
    values.update(changes)
    return InvocationRecord(**values)


def make_gate(**changes) -> DefaultPrivacyGate:
    return DefaultPrivacyGate(
        boundary=PrivacyBoundary(
            now=NOW,
            approved_access_policies=frozenset({POLICY}),
        ),
        **changes,
    )


def authorized_assessment(configuration=None, record=None):
    configuration = configuration or make_configuration()
    record = record or make_record()
    decision = make_gate().authorize(record, configuration)
    assert decision.allowed
    assessment = DeterministicPromptAssessor().assess(decision, configuration)
    return configuration, decision, assessment


def assert_not_assessed(assessment, reason):
    assert set(assessment.dimensions) == set(DIMENSIONS)
    for result in assessment.dimensions.values():
        assert result.status is PromptAssessmentStatus.NOT_ASSESSED
        assert result.score is None
        assert result.confidence_band is None
        assert result.evidence_reference is None
        assert result.not_assessed_reason == reason


def test_valid_prompt_preview_assesses_every_enabled_dimension_with_content_free_evidence():
    configuration, decision, assessment = authorized_assessment()

    assert decision.content is not None
    assert assessment.content_state is ContentAnalysisState.ENABLED
    assert all(
        result.status is PromptAssessmentStatus.ASSESSED
        for result in assessment.dimensions.values()
    )
    for result in assessment.dimensions.values():
        assert result.score is not None
        assert 0.0 <= result.score <= 1.0
        assert result.confidence_band is not None
        assert result.evidence_reference is not None
        assert result.evidence_reference.signal_names
        assert result.evidence_reference.source_field_classes
        assert result.threshold_interpretation == (
            "Configured threshold 0.50; score meets the threshold."
            if result.score >= 0.5
            else "Configured threshold 0.50; score is below the threshold."
        )
    assert "person or team" in assessment.limitation
    assert "Analyze the provided data" not in repr(assessment)


@pytest.mark.parametrize(
    ("record", "configuration", "expected_state", "expected_reason"),
    (
        (
            make_record(sensitive_content=None),
            make_configuration(),
            ContentAccessState.ABSENT,
            "content_unavailable",
        ),
        (
            make_record(
                sensitive_content=SensitiveContent(
                    redaction_status=RedactionStatus.REDACTED
                )
            ),
            make_configuration(),
            ContentAccessState.REDACTED,
            "content_redacted",
        ),
        (
            make_record(
                sensitive_content=SensitiveContent(prompt_preview=object())  # type: ignore[arg-type]
            ),
            make_configuration(),
            ContentAccessState.MALFORMED,
            "content_malformed",
        ),
        (
            make_record(source_timestamp_utc=datetime(2025, 1, 1, tzinfo=UTC)),
            make_configuration(),
            ContentAccessState.EXPIRED,
            "content_expired",
        ),
        (
            make_record(),
            make_configuration(
                privacy=replace(
                    make_configuration().privacy,
                    exclusion_rules=("prompt_preview", "response_preview"),
                )
            ),
            ContentAccessState.EXCLUDED,
            "content_excluded",
        ),
    ),
)
def test_unusable_prompt_content_is_not_assessed(record, configuration, expected_state, expected_reason):
    decision = make_gate().authorize(record, configuration)

    assert not decision.allowed
    assert decision.content_state is expected_state
    assert decision.reason == expected_reason
    assessment = DeterministicPromptAssessor().assess(decision, configuration)

    assert_not_assessed(assessment, expected_reason)


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_dimension_thresholds_are_independent(dimension):
    baseline_configuration = make_configuration()
    changed_dimensions = {
        name: PromptDimensionConfiguration(enabled=True, threshold=0.5)
        for name in DIMENSIONS
    }
    changed_dimensions[dimension] = PromptDimensionConfiguration(enabled=True, threshold=1.0)
    changed_configuration = make_configuration(prompt_dimensions=changed_dimensions)

    _, _, baseline = authorized_assessment(baseline_configuration)
    _, _, changed = authorized_assessment(changed_configuration)

    for name in DIMENSIONS:
        assert changed.dimensions[name].score == baseline.dimensions[name].score
        if name == dimension:
            assert changed.dimensions[name].threshold_interpretation == (
                "Configured threshold 1.00; score meets the threshold."
                if changed.dimensions[name].score >= 1.0
                else "Configured threshold 1.00; score is below the threshold."
            )
        else:
            assert changed.dimensions[name] == baseline.dimensions[name]


def test_all_dimensions_disabled_is_valid_and_has_no_confidence_or_evidence():
    configuration = make_configuration(
        prompt_dimensions={
            dimension: PromptDimensionConfiguration(enabled=False, threshold=0.5)
            for dimension in DIMENSIONS
        }
    )

    _, decision, assessment = authorized_assessment(configuration)

    assert decision.allowed
    assert assessment.content_state is ContentAnalysisState.ENABLED
    assert_not_assessed(assessment, "dimension_disabled")


@pytest.mark.parametrize(
    "prompt",
    (
        "Build.",
        "Analyze the input and return JSON.",
        "Context: use the provided records. Calculate totals for 2026-01-01. Output a table.",
        "No punctuation " * 80,
    ),
)
def test_assessed_scores_stay_within_inclusive_bounds(prompt):
    record = make_record(
        sensitive_content=SensitiveContent(
            prompt_preview=prompt,
            response_preview=None,
        )
    )

    _, _, assessment = authorized_assessment(record=record)

    for result in assessment.dimensions.values():
        assert result.status is PromptAssessmentStatus.ASSESSED
        assert result.score is not None
        assert 0.0 <= result.score <= 1.0


def test_confidence_and_evidence_apply_only_to_numeric_dimensions():
    dimensions = {
        name: PromptDimensionConfiguration(enabled=True, threshold=0.5)
        for name in DIMENSIONS
    }
    dimensions[PromptDimension.OUTPUT_CONSTRAINTS.value] = PromptDimensionConfiguration(
        enabled=False
    )
    configuration, _, assessment = authorized_assessment(
        make_configuration(prompt_dimensions=dimensions)
    )

    for name, result in assessment.dimensions.items():
        if name == PromptDimension.OUTPUT_CONSTRAINTS.value:
            assert result.status is PromptAssessmentStatus.NOT_ASSESSED
            assert result.score is None
            assert result.confidence_band is None
            assert result.evidence_reference is None
            assert result.not_assessed_reason == "dimension_disabled"
        else:
            assert result.status is PromptAssessmentStatus.ASSESSED
            assert result.score is not None
            assert result.confidence_band is not None
            assert result.evidence_reference is not None
            assert result.evidence_reference.summaries
            assert result.evidence_reference.counts

    assert configuration.content_analysis.state is ContentAnalysisState.ENABLED


def test_content_blocking_preserves_non_content_record_and_signals():
    configuration = make_configuration(
        categories=(
            CategoryDefinition(
                "non-content",
                "Non-content",
                "Direct invocation metadata.",
                ("operation", "input_token_count", "output_token_count"),
            ),
        )
    )
    record = make_record(sensitive_content=None)

    decision = make_gate().authorize(record, configuration)

    assert not decision.allowed
    assert decision.non_content_record is not None
    assert decision.non_content_record.sensitive_content is None
    assert decision.non_content_record.source_request_id == "request-1"
    assert decision.non_content_record.input_token_count == 12
    assert decision.non_content_record.output_token_count == 20

    signals = extract_signals(decision.non_content_record, configuration)
    assert {signal.name for signal in signals} == {
        "operation",
        "input_token_count",
        "output_token_count",
    }
    assert all(signal.value is not None for signal in signals)
    assessment = DeterministicPromptAssessor().assess(decision, configuration)
    assert_not_assessed(assessment, "content_unavailable")
