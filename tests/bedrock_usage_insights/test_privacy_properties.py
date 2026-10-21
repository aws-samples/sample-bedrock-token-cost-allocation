"""Property-based tests for default privacy and failure-closed redaction."""

from dataclasses import replace
from datetime import date, datetime, timezone

from hypothesis import given, settings, strategies as st

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
from bedrock_usage_insights.prompt_assessment import assess_prompt
from bedrock_usage_insights.signals import extract_signals

UTC = timezone.utc


@st.composite
def sensitive_content_cases(draw):
    """Generate raw content, redacted content, and every redaction outcome."""

    raw_prompt = draw(st.text(min_size=1, max_size=80)) + "__RAW_PROMPT__"
    raw_response = draw(st.text(min_size=1, max_size=80)) + "__RAW_RESPONSE__"
    redacted_prompt = draw(st.text(min_size=1, max_size=80)) + "__REDACTED_PROMPT__"
    redacted_response = draw(st.text(min_size=1, max_size=80)) + "__REDACTED_RESPONSE__"
    status = draw(st.sampled_from(tuple(RedactionStatus)))
    return (
        SensitiveContent(raw_prompt, raw_response, status),
        SensitiveContent(redacted_prompt, redacted_response, RedactionStatus.REDACTED),
        status,
    )


def make_scope() -> AnalysisScope:
    return AnalysisScope(
        accounts=("123456789012",),
        regions=("us-east-1",),
        models=("model",),
        operations=("InvokeModel",),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )


def make_configuration(*, enabled: bool, content_enabled: bool) -> InsightConfiguration:
    return InsightConfiguration(
        configuration_version="privacy-property-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="property-test",
        change_reason="privacy invariant",
        analysis_scope=make_scope(),
        categories=(
            CategoryDefinition(
                "content",
                "Content",
                "Configured content-derived signals.",
                (
                    "prompt_task_marker",
                    "prompt_output_constraint_marker",
                    "prompt_context_marker",
                    "prompt_specificity_marker",
                ),
            ),
        ),
        enabled=enabled,
        content_analysis=ContentAnalysisPolicy(
            state=(
                ContentAnalysisState.ENABLED
                if content_enabled
                else ContentAnalysisState.DISABLED
            ),
            approved_accounts=("123456789012",) if content_enabled else (),
            approved_regions=("us-east-1",) if content_enabled else (),
            access_policy_reference="approved-policy" if content_enabled else None,
            retention_days=30 if content_enabled else None,
            max_preview_length=256,
        ),
        prompt_dimensions={
            dimension.value: PromptDimensionConfiguration(enabled=True)
            for dimension in PromptDimension
        },
    )


def make_record(content: SensitiveContent) -> InvocationRecord:
    return InvocationRecord(
        source_request_id="request-1",
        source_timestamp_utc=datetime(2026, 1, 2, tzinfo=UTC),
        source_account="123456789012",
        source_region="us-east-1",
        model_id="model",
        operation="InvokeModel",
        analysis_run_id="run-1",
        configuration_version="privacy-property-v1",
        analysis_logic_version="1.0",
        sensitive_content=content,
    )


def derived_output(record: InvocationRecord, configuration: InsightConfiguration):
    """Model the pure downstream outputs at the privacy boundary."""

    signals = extract_signals(record, configuration)
    assessment_content = record.sensitive_content
    if (
        configuration.content_analysis.state is ContentAnalysisState.ENABLED
        and assessment_content is not None
        and assessment_content.redaction_status is not RedactionStatus.REDACTED
    ):
        assessment_content = (
            assessment_content
            if assessment_content.redaction_status is RedactionStatus.FAILED
            else None
        )
    assessment = assess_prompt(assessment_content, configuration)
    return {
        "record": record.to_dict(),
        "signals": [signal.to_dict() for signal in signals],
        "prompt_quality": assessment.to_dict(),
    }


# Feature: bedrock-usage-insights, Property 12: Default privacy and redaction failure invariants
# **Validates: Requirements 1.5, 5.3, 7.1, 7.3**
@given(sensitive_content_cases())
@settings(max_examples=100, deadline=None)
def test_property_12_default_privacy_and_redaction_failure_invariants(case):
    raw_content, redacted_content, status = case
    disabled_configuration = make_configuration(enabled=False, content_enabled=False)
    enabled_configuration = make_configuration(enabled=True, content_enabled=True)

    raw_record = make_record(raw_content)
    disabled_output = derived_output(raw_record, disabled_configuration)

    assert disabled_output["signals"] == []
    assert "sensitive_content" not in disabled_output["record"]
    assert "__RAW_PROMPT__" not in repr(disabled_output)
    assert "__RAW_RESPONSE__" not in repr(disabled_output)
    assert all(
        dimension["status"] == PromptAssessmentStatus.NOT_ASSESSED.value
        for dimension in disabled_output["prompt_quality"]["dimensions"].values()
    )

    if status is RedactionStatus.REDACTED:
        authorized_record = replace(raw_record, sensitive_content=redacted_content)
        enabled_output = derived_output(authorized_record, enabled_configuration)

        assert all(signal["availability"] == "available" for signal in enabled_output["signals"])
        assert all(
            dimension["status"] == PromptAssessmentStatus.ASSESSED.value
            for dimension in enabled_output["prompt_quality"]["dimensions"].values()
        )
        assert "__RAW_PROMPT__" not in repr(enabled_output)
        assert "__RAW_RESPONSE__" not in repr(enabled_output)
        assert "__REDACTED_PROMPT__" not in repr(enabled_output)
        assert "__REDACTED_RESPONSE__" not in repr(enabled_output)
        assert "sensitive_content" not in enabled_output["record"]
        return

    failed_or_unredacted_output = derived_output(raw_record, enabled_configuration)
    assert all(signal["availability"] != "available" for signal in failed_or_unredacted_output["signals"])
    assert all(
        dimension["status"] == PromptAssessmentStatus.NOT_ASSESSED.value
        for dimension in failed_or_unredacted_output["prompt_quality"]["dimensions"].values()
    )
    assert "sensitive_content" not in failed_or_unredacted_output["record"]
    assert "__RAW_PROMPT__" not in repr(failed_or_unredacted_output)
    assert "__RAW_RESPONSE__" not in repr(failed_or_unredacted_output)
