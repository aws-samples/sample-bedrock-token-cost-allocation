"""Coverage checks for reusable usage-insights synthetic fixtures."""

from hypothesis import given, settings

from bedrock_usage_insights.oracle import CanonicalSchemaOracle, ConfigurationSchemaOracle
from bedrock_usage_insights.models import (
    ContentAnalysisState,
    PromptAssessmentStatus,
    SignalProvenance,
)
from fixture_support import (
    ContentAvailability,
    aggregate_group_strategy,
    analysis_run_strategy,
    configuration_strategy,
    content_case_strategy,
    invocation_record_strategy,
    long_text,
    malformed_value_strategy,
    redaction_failure_strategy,
    result_collection_strategy,
    run_outcome_strategy,
    signal_strategy,
    unicode_text,
)


@settings(max_examples=100)
@given(configuration_strategy())
def test_configuration_strategy_produces_valid_versioned_configurations(configuration):
    assert not ConfigurationSchemaOracle.validate(configuration)
    assert configuration.configuration_version
    assert configuration.analysis_scope is not None


@settings(max_examples=100)
@given(invocation_record_strategy())
def test_invocation_strategy_produces_traceable_records(record):
    assert not CanonicalSchemaOracle.validate_invocation(record)
    assert record.source_timestamp_utc.tzinfo is not None


@settings(max_examples=100)
@given(signal_strategy())
def test_signal_strategy_preserves_explicit_provenance(signal):
    assert signal.name
    assert signal.source_field_class
    assert signal.provenance in set(SignalProvenance)
    if signal.name.startswith("prompt_"):
        assert signal.provenance is SignalProvenance.CONTENT_DERIVED


@settings(max_examples=100)
@given(content_case_strategy())
def test_content_case_strategy_covers_explicit_availability_contract(case):
    availability, content = case
    if availability is ContentAvailability.AVAILABLE:
        assert content is not None
        assert content.prompt_preview or content.response_preview
    elif availability is ContentAvailability.REDACTED:
        assert content is not None
        assert content.redaction_status.value == "redacted"
    else:
        assert content is None


@settings(max_examples=100)
@given(result_collection_strategy())
def test_result_collection_strategy_produces_schema_valid_rows(results):
    assert isinstance(results, tuple)
    assert all(not CanonicalSchemaOracle.validate_result(result) for result in results)
    assert all("prompt_preview" not in result.to_dict() for result in results)


@settings(max_examples=100)
@given(aggregate_group_strategy())
def test_aggregate_group_strategy_produces_valid_suppressed_or_publishable_groups(group):
    assert not CanonicalSchemaOracle.validate_aggregate(group)
    if group.suppressed:
        assert group.group_key is None
        assert group.distinct_qualifying_subject_count < group.minimum_group_size


@settings(max_examples=100)
@given(run_outcome_strategy())
def test_run_outcome_strategy_covers_failure_isolation_inputs(outcome):
    assert not CanonicalSchemaOracle.validate_run(outcome.run)
    if outcome.results and outcome.errors:
        assert outcome.run.status.value == "partial"
    elif outcome.results:
        assert outcome.run.status.value == "completed"
    else:
        assert outcome.run.status.value == "failed"


@settings(max_examples=100)
@given(analysis_run_strategy())
def test_analysis_run_strategy_covers_valid_lifecycle_records(run):
    assert not CanonicalSchemaOracle.validate_run(run)


@settings(max_examples=100)
@given(unicode_text(), long_text(), malformed_value_strategy(), redaction_failure_strategy())
def test_edge_case_strategies_generate_reusable_non_aws_inputs(text, oversized, malformed, failure):
    assert text.strip()
    assert len(oversized) >= 501
    assert malformed is not None
    assert failure.code in {"REDACTION_FAILED", "EXCLUSION_FAILED", "CONTENT_POLICY_BLOCKED"}


@settings(max_examples=100)
@given(result_collection_strategy())
def test_result_collections_keep_prompt_assessment_states_explicit(results):
    for result in results:
        assert result.prompt_quality.content_state in set(ContentAnalysisState)
        for dimension in result.prompt_quality.dimensions.values():
            assert dimension.status in set(PromptAssessmentStatus)
            if dimension.status is PromptAssessmentStatus.NOT_ASSESSED:
                assert dimension.score is None
