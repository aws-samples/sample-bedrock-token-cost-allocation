"""Property tests for configured-signal isolation."""

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any

from hypothesis import given, settings, strategies as st

from bedrock_usage_insights.models import (
    AnalysisScope,
    CategoryDefinition,
    ContentAnalysisPolicy,
    ContentAnalysisState,
    IdentityMetadata,
    InsightConfiguration,
    InvocationRecord,
    RedactionStatus,
    SensitiveContent,
)
from bedrock_usage_insights.prompt_assessment import assess_prompt
from bedrock_usage_insights.signals import extract_signals

UTC = timezone.utc

_DIRECT_SOURCE_FIELDS = (
    "operation",
    "model_id",
    "input_token_count",
    "output_token_count",
    "cache_read_token_count",
    "cache_write_token_count",
    "stop_reason",
    "content_type",
    "request_metadata",
    "model_format",
)
_IDENTITY_SOURCE_FIELDS = (
    "identity_arn",
    "identity_session_name",
    "identity_name",
    "account_owner",
)
_CONTENT_SIGNAL_FIELDS = (
    "prompt_task_marker",
    "prompt_output_constraint_marker",
    "prompt_context_marker",
    "prompt_specificity_marker",
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


def _configuration(enabled_signals: tuple[str, ...]) -> InsightConfiguration:
    return InsightConfiguration(
        configuration_version="config-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="property-test",
        change_reason="configured signal isolation",
        analysis_scope=_scope(),
        categories=(
            CategoryDefinition(
                category_id="configured",
                display_name="Configured signals",
                description="A test category based only on configured signals.",
                signals=enabled_signals,
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


def _record(**changes: Any) -> InvocationRecord:
    values: dict[str, Any] = {
        "source_request_id": "request-1",
        "source_timestamp_utc": datetime(2026, 1, 2, tzinfo=UTC),
        "source_account": "123456789012",
        "source_region": "us-east-1",
        "model_id": "model",
        "operation": "InvokeModel",
        "analysis_run_id": "run-1",
        "configuration_version": "config-v1",
        "analysis_logic_version": "1.0",
        "input_token_count": 12,
        "output_token_count": 24,
        "cache_read_token_count": 0,
        "cache_write_token_count": 0,
        "stop_reason": "end_turn",
        "content_type": "application/json",
        "request_metadata": {"team": "platform"},
        "model_format": "messages-v1",
        "identity_metadata": IdentityMetadata(
            identity_arn="arn:aws:iam::123456789012:role/role-a",
            session_name="session-a",
            name="name-a",
            account_owner="owner-a",
        ),
        "sensitive_content": SensitiveContent(
            prompt_preview="Given the requirements, create a concise JSON response.",
            response_preview="{\"status\": \"ok\"}",
            redaction_status=RedactionStatus.REDACTED,
        ),
    }
    values.update(changes)
    return InvocationRecord(**values)


def _field_strategy(field_name: str) -> st.SearchStrategy[Any]:
    if field_name in {"input_token_count", "output_token_count", "cache_read_token_count", "cache_write_token_count"}:
        return st.integers(min_value=0, max_value=10_000)
    if field_name == "request_metadata":
        return st.dictionaries(
            keys=st.sampled_from(("team", "project", "purpose")),
            values=st.text(min_size=1, max_size=20),
            max_size=3,
        )
    if field_name == "operation":
        return st.sampled_from(("InvokeModel", "Converse", "InvokeModelWithResponseStream"))
    if field_name == "model_id":
        return st.sampled_from(("model", "other-model", "third-model"))
    if field_name == "stop_reason":
        return st.sampled_from(("end_turn", "max_tokens", "stop_sequence"))
    if field_name == "content_type":
        return st.sampled_from(("application/json", "text/plain", "application/xml"))
    if field_name == "model_format":
        return st.sampled_from(("messages-v1", "completion-v1", "unknown-v1"))
    return st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=24)


@st.composite
def configured_signal_isolation_cases(draw: st.DrawFn):
    enabled_signals = tuple(
        draw(
            st.lists(
                st.sampled_from(_DIRECT_SOURCE_FIELDS + _IDENTITY_SOURCE_FIELDS),
                unique=True,
                min_size=0,
                max_size=len(_DIRECT_SOURCE_FIELDS + _IDENTITY_SOURCE_FIELDS) - 1,
            )
        )
        + draw(
            st.lists(
                st.sampled_from(_CONTENT_SIGNAL_FIELDS),
                unique=True,
                min_size=0,
                max_size=len(_CONTENT_SIGNAL_FIELDS),
            )
        )
    )
    disabled_fields = [
        field_name
        for field_name in _DIRECT_SOURCE_FIELDS + _IDENTITY_SOURCE_FIELDS
        if field_name not in enabled_signals
    ]
    field_name = draw(st.sampled_from(disabled_fields))
    original = draw(_field_strategy(field_name))
    changed = draw(_field_strategy(field_name).filter(lambda value: value != original))

    base_record = _record()
    if field_name in _IDENTITY_SOURCE_FIELDS:
        identity = base_record.identity_metadata
        assert identity is not None
        identity_field = {
            "identity_arn": "identity_arn",
            "identity_session_name": "session_name",
            "identity_name": "name",
            "account_owner": "account_owner",
        }[field_name]
        base_record = replace(
            base_record,
            identity_metadata=replace(identity, **{identity_field: original}),
        )
        changed_record = replace(
            base_record,
            identity_metadata=replace(identity, **{identity_field: changed}),
        )
    else:
        base_record = replace(base_record, **{field_name: original})
        changed_record = replace(base_record, **{field_name: changed})

    return _configuration(enabled_signals), base_record, changed_record


def _downstream_snapshot(
    record: InvocationRecord,
    configuration: InsightConfiguration,
) -> dict[str, Any]:
    """Project pure extractor output into the Property 4 result surface.

    The classifier is a later implementation task. This deterministic test
    projection exercises the same configured Signal boundary for classification,
    scores, confidence, evidence, and prompt-quality output without inventing
    an AWS or model dependency.
    """

    signals = extract_signals(record, configuration)
    available = tuple(signal for signal in signals if signal.value is not None)
    score = len(available) / len(signals) if signals else 0.0
    classification = (
        configuration.categories[0].category_id
        if score >= configuration.categories[0].evidence_threshold
        else "insufficient_evidence"
    )
    confidence = "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"
    evidence = tuple(
        (
            signal.name,
            signal.source_field_class.value,
            signal.provenance.value,
            signal.summary,
        )
        for signal in available
    )
    return {
        "signals": tuple(signal.to_dict() for signal in signals),
        "classification": classification,
        "score": score,
        "confidence": confidence,
        "evidence": evidence,
        "prompt_quality": assess_prompt(record.sensitive_content, configuration).to_dict(),
    }


# Feature: bedrock-usage-insights, Property 4: Configured-signal isolation
# **Validates: Requirements 3.1, 3.4**
@given(configured_signal_isolation_cases())
@settings(max_examples=100, deadline=None)
def test_property_4_non_enabled_source_fields_cannot_change_analysis(case):
    configuration, original_record, changed_record = case

    assert _downstream_snapshot(original_record, configuration) == _downstream_snapshot(
        changed_record, configuration
    )
