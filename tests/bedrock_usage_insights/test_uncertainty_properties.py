"""Property tests for conservative uncertainty and explicit format handling."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any

from hypothesis import given, settings, strategies as st

from bedrock_usage_insights.models import (
    AnalysisScope,
    CategoryDefinition,
    ConfidenceBand,
    ContentAnalysisPolicy,
    ContentAnalysisState,
    InsightConfiguration,
    InvocationRecord,
    RedactionStatus,
    SensitiveContent,
    UnknownReason,
)
from bedrock_usage_insights.normalization import InvocationRecordNormalizer
from bedrock_usage_insights.privacy import (
    ContentAccessState,
    DefaultPrivacyGate,
    PrivacyBoundary,
)
from bedrock_usage_insights.signals import extract_signals

UTC = timezone.utc
_CONTENT_SIGNALS = (
    "prompt_task_marker",
    "prompt_output_constraint_marker",
    "prompt_context_marker",
    "prompt_specificity_marker",
)
_FORMATS = {
    "nova": "amazon.nova-pro-v1:0",
    "anthropic": "anthropic.claude-3-sonnet-20240229-v1:0",
    "titan": "amazon.titan-text-express-v1",
    "cohere": "cohere.command-text-v14",
    "llama": "meta.llama3-8b-instruct-v1:0",
    "mistral": "mistral.mistral-7b-instruct-v0:2",
}
_ALLOWED_UNKNOWN_REASONS = frozenset(reason.value for reason in UnknownReason)


@dataclass(frozen=True)
class _Outcome:
    model_format: str | None
    error_codes: frozenset[str]
    unknown_reason: str | None
    confidence: str | None
    category: str | None
    content_state: ContentAccessState


def _configuration() -> InsightConfiguration:
    return InsightConfiguration(
        configuration_version="uncertainty-property-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="property-test",
        change_reason="conservative uncertainty",
        analysis_scope=AnalysisScope(
            accounts=("123456789012",),
            regions=("us-east-1",),
            models=tuple(_FORMATS.values()),
            operations=("InvokeModel",),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        ),
        categories=(
            CategoryDefinition(
                category_id="content_pattern",
                display_name="Content pattern",
                description="A pattern supported by permitted prompt signals.",
                signals=_CONTENT_SIGNALS,
                evidence_threshold=0.5,
            ),
        ),
        enabled=True,
        content_analysis=ContentAnalysisPolicy(
            state=ContentAnalysisState.ENABLED,
            approved_accounts=("123456789012",),
            approved_regions=("us-east-1",),
            access_policy_reference="approved-policy",
            retention_days=30,
            max_preview_length=500,
        ),
    )


def _source_row(format_name: str) -> dict[str, Any]:
    return {
        "source_request_id": "request-1",
        "source_timestamp_utc": "2026-01-02T00:00:00+00:00",
        "source_account": "123456789012",
        "source_region": "us-east-1",
        "model_id": _FORMATS[format_name],
        "operation": "InvokeModel",
        "model_format": format_name,
        "input_body_json": "{}",
        "output_body_json": "{}",
        "prompt_preview": (
            "Please create a concise JSON response with context and specific requirements."
        ),
        "response_preview": '{"status": "ok"}',
    }


def _unknown_for_content_state(state: ContentAccessState) -> str:
    if state is ContentAccessState.REDACTION_FAILED:
        return UnknownReason.CONTENT_REDACTED.value
    if state is ContentAccessState.REDACTED:
        return UnknownReason.CONTENT_REDACTED.value
    if state is ContentAccessState.MALFORMED:
        return UnknownReason.PARSE_ERROR.value
    return UnknownReason.CONTENT_UNAVAILABLE.value


def _analyze(
    source_row: dict[str, Any],
    mutation: str,
    configuration: InsightConfiguration,
) -> _Outcome:
    normalized = InvocationRecordNormalizer().normalize(
        source_row,
        analysis_run_id="run-1",
        configuration_version=configuration.configuration_version,
        analysis_logic_version="1.0",
        include_sensitive=True,
    )
    if normalized.record is None:
        return _Outcome(
            model_format=None,
            error_codes=frozenset(error.code for error in normalized.errors),
            unknown_reason=UnknownReason.PARSE_ERROR.value,
            confidence=None,
            category=None,
            content_state=ContentAccessState.MALFORMED,
        )

    record = normalized.record
    if normalized.errors:
        return _Outcome(
            model_format=record.model_format,
            error_codes=frozenset(error.code for error in normalized.errors),
            unknown_reason=UnknownReason.PARSE_ERROR.value,
            confidence=None,
            category=None,
            content_state=ContentAccessState.MALFORMED,
        )
    if mutation == "redact":
        record = replace(
            record,
            sensitive_content=SensitiveContent(
                prompt_preview="",
                response_preview="",
                redaction_status=RedactionStatus.REDACTED,
            ),
        )
    elif mutation == "invalidate":
        record = replace(
            record,
            sensitive_content=SensitiveContent(
                prompt_preview=123,
                response_preview=None,
            ),
        )

    decision = DefaultPrivacyGate(
        boundary=PrivacyBoundary(
            now=datetime(2026, 1, 3, tzinfo=UTC),
            approved_access_policies=frozenset({"approved-policy"}),
        )
    ).authorize(record, configuration)
    if not decision.allowed:
        return _Outcome(
            model_format=normalized.record.model_format,
            error_codes=frozenset(error.code for error in normalized.errors),
            unknown_reason=_unknown_for_content_state(decision.content_state),
            confidence=None,
            category=None,
            content_state=decision.content_state,
        )

    assert decision.record is not None
    analysis_record = replace(decision.record, sensitive_content=decision.content)
    signals = extract_signals(analysis_record, configuration)
    available = tuple(
        signal
        for signal in signals
        if signal.value is not None and signal.availability.value == "available"
    )
    score = len(available) / len(signals) if signals else 0.0
    category = (
        configuration.categories[0].category_id
        if score >= configuration.categories[0].evidence_threshold
        else None
    )
    confidence = (
        ConfidenceBand.HIGH.value
        if score >= 0.8
        else ConfidenceBand.MEDIUM.value
        if score >= 0.5
        else ConfidenceBand.LOW.value
    )
    return _Outcome(
        model_format=normalized.record.model_format,
        error_codes=frozenset(error.code for error in normalized.errors),
        unknown_reason=(
            None if category is not None else UnknownReason.INSUFFICIENT_EVIDENCE.value
        ),
        confidence=confidence,
        category=category,
        content_state=decision.content_state,
    )


@st.composite
def unsupported_or_degraded_cases(draw: st.DrawFn):
    format_name = draw(st.sampled_from(tuple(_FORMATS)))
    mutation = draw(
        st.sampled_from(("remove", "redact", "invalidate", "unsupported"))
    )
    row = _source_row(format_name)
    if mutation == "remove":
        row.pop("prompt_preview")
    elif mutation == "unsupported":
        row["model_format"] = "unrelated-format-v9"
    return format_name, mutation, row


# Feature: bedrock-usage-insights, Property 9: Conservative uncertainty and unsupported formats
# **Validates: Requirements 3.5, 4.1, 4.2, 4.5, 5.3, 12.7**
@given(unsupported_or_degraded_cases())
@settings(max_examples=100, deadline=None)
def test_property_9_evidence_loss_is_conservative_and_format_specific(case):
    format_name, mutation, degraded_row = case
    configuration = _configuration()
    baseline = _analyze(_source_row(format_name), "baseline", configuration)
    degraded = _analyze(degraded_row, mutation, configuration)

    assert baseline.confidence == ConfidenceBand.HIGH.value
    assert baseline.category == "content_pattern"
    assert (
        degraded.unknown_reason in _ALLOWED_UNKNOWN_REASONS
        or degraded.confidence == ConfidenceBand.LOW.value
    )
    assert degraded.confidence != ConfidenceBand.HIGH.value
    assert not (
        degraded.category == baseline.category
        and degraded.confidence == ConfidenceBand.HIGH.value
    )

    if mutation == "unsupported":
        assert "UNSUPPORTED_MODEL_FORMAT" in degraded.error_codes
        assert degraded.model_format == "unrelated_format_v9"
        assert degraded.model_format not in _FORMATS
    elif mutation == "invalidate":
        assert degraded.content_state is ContentAccessState.MALFORMED
        assert degraded.unknown_reason == UnknownReason.PARSE_ERROR.value
        assert degraded.model_format == baseline.model_format
    elif mutation == "redact":
        assert degraded.content_state is ContentAccessState.REDACTED
        assert degraded.unknown_reason == UnknownReason.CONTENT_REDACTED.value
        assert degraded.model_format == baseline.model_format
    else:
        assert degraded.model_format == baseline.model_format
        assert degraded.unknown_reason == UnknownReason.INSUFFICIENT_EVIDENCE.value
        assert degraded.confidence == ConfidenceBand.LOW.value
