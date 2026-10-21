"""Property tests for identity-field isolation and explicit exposure controls."""

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any

from hypothesis import given, settings, strategies as st

from bedrock_usage_insights.models import (
    AnalysisScope,
    CategoryDefinition,
    ExposureLevel,
    IdentityMetadata,
    InsightConfiguration,
    InvocationRecord,
)
from bedrock_usage_insights.prompt_assessment import assess_prompt
from bedrock_usage_insights.signals import extract_signals

UTC = timezone.utc
_IDENTITY_FIELDS = (
    "identity_arn",
    "identity_session_name",
    "identity_name",
    "account_owner",
)


def _scope() -> AnalysisScope:
    return AnalysisScope(
        accounts=("123456789012",),
        regions=("us-east-1",),
        models=("model",),
        operations=("InvokeModel",),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )


def _configuration(
    identity_field: str | None,
    exposure_level: ExposureLevel,
) -> InsightConfiguration:
    signals = (identity_field,) if identity_field is not None else ("operation",)
    return InsightConfiguration(
        configuration_version="identity-isolation-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="property-test",
        change_reason="identity isolation",
        analysis_scope=_scope(),
        categories=(
            CategoryDefinition(
                category_id="configured",
                display_name="Configured signals",
                description="A test category based only on configured signals.",
                signals=signals,
                evidence_threshold=0.5,
                exposure_level=exposure_level,
            ),
        ),
        enabled=True,
    )


def _record(identity: IdentityMetadata) -> InvocationRecord:
    return InvocationRecord(
        source_request_id="request-1",
        source_timestamp_utc=datetime(2026, 1, 2, tzinfo=UTC),
        source_account="123456789012",
        source_region="us-east-1",
        model_id="model",
        operation="InvokeModel",
        analysis_run_id="run-1",
        configuration_version="identity-isolation-v1",
        analysis_logic_version="1.0",
        input_token_count=12,
        output_token_count=24,
        request_metadata={"team": "platform"},
        identity_metadata=identity,
    )


def _identity_value_strategy() -> st.SearchStrategy[str]:
    return st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=48,
    )


@st.composite
def identity_isolation_cases(draw: st.DrawFn):
    field = draw(st.sampled_from(_IDENTITY_FIELDS))
    original = draw(_identity_value_strategy())
    changed = draw(_identity_value_strategy().filter(lambda value: value != original))
    exposure_level = draw(st.sampled_from(tuple(ExposureLevel)))
    approved = draw(st.booleans())

    attribute_by_signal = {
        "identity_arn": "identity_arn",
        "identity_session_name": "session_name",
        "identity_name": "name",
        "account_owner": "account_owner",
    }
    identity_values: dict[str, str] = {
        attribute: f"{attribute}-original"
        for attribute in attribute_by_signal.values()
    }
    identity_values[attribute_by_signal[field]] = original
    changed_identity_values = dict(identity_values)
    changed_identity_values[attribute_by_signal[field]] = changed

    return (
        field,
        approved,
        exposure_level,
        _configuration(field if approved else None, exposure_level),
        _record(IdentityMetadata(**identity_values)),
        _record(IdentityMetadata(**changed_identity_values)),
    )


def _result_snapshot(
    record: InvocationRecord,
    configuration: InsightConfiguration,
) -> dict[str, Any]:
    """Project pure signal and prompt results into the Property 8 surface."""

    signals = extract_signals(record, configuration)
    available = tuple(signal for signal in signals if signal.value is not None)
    score = len(available) / len(signals) if signals else 0.0
    category = configuration.categories[0]
    return {
        "classification": (
            category.category_id
            if score >= category.evidence_threshold
            else "insufficient_evidence"
        ),
        "score": score,
        "confidence": "high" if score >= 0.8 else "medium" if score >= 0.5 else "low",
        "evidence": tuple(
            (
                signal.name,
                signal.value,
                signal.source_field_class.value,
                signal.provenance.value,
                signal.summary,
            )
            for signal in available
        ),
        "prompt_quality": assess_prompt(record.sensitive_content, configuration).to_dict(),
        "exposure_level": category.exposure_level.value,
    }


# Feature: bedrock-usage-insights, Property 8: Identity isolation
# **Validates: Requirements 3.4, 7.3**
@given(identity_isolation_cases())
@settings(max_examples=100, deadline=None)
def test_property_8_identity_fields_are_isolated_without_explicit_approval(case):
    field, approved, exposure_level, configuration, original_record, changed_record = case

    original_snapshot = _result_snapshot(original_record, configuration)
    changed_snapshot = _result_snapshot(changed_record, configuration)

    if not approved:
        assert changed_snapshot == original_snapshot
        assert all(
            signal.name != field
            for signal in extract_signals(original_record, configuration)
        )
        return

    assert configuration.categories[0].signals == (field,)
    assert configuration.categories[0].exposure_level is exposure_level
    assert [signal.name for signal in extract_signals(original_record, configuration)] == [field]
    assert [signal.name for signal in extract_signals(changed_record, configuration)] == [field]

    # Any changed result is permitted only because this identity Signal was
    # explicitly configured with an exposure control.
    if changed_snapshot != original_snapshot:
        assert approved
