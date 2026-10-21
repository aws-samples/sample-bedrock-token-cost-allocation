"""Focused unit tests for configured Signal extraction."""

from dataclasses import replace
from datetime import date, datetime, timezone

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
    SignalAvailability,
    SignalProvenance,
    SourceFieldClass,
)
from bedrock_usage_insights.signals import (
    MAX_METADATA_ITEMS,
    MAX_METADATA_VALUE_LENGTH,
    MAX_SIGNAL_STRING_LENGTH,
    MAX_TOKEN_COUNT,
    extract_signals,
)

UTC = timezone.utc


def make_scope() -> AnalysisScope:
    return AnalysisScope(
        accounts=("123456789012",),
        regions=("us-east-1",),
        models=("model",),
        operations=("InvokeModel",),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )


def make_configuration(*signals: str, enabled: bool = True, **changes) -> InsightConfiguration:
    base = InsightConfiguration(
        configuration_version="config-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="test",
        change_reason="signal test",
        analysis_scope=make_scope(),
        categories=(CategoryDefinition("test", "Test", "Test signals", signals),),
        enabled=enabled,
        **changes,
    )
    return base


def make_record(**changes) -> InvocationRecord:
    values = {
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
        "request_metadata": {"team": "platform"},
    }
    values.update(changes)
    return InvocationRecord(**values)


def test_extracts_only_enabled_category_allowlist_in_stable_order():
    configuration = make_configuration("model_id", "operation", "request_metadata")

    signals = extract_signals(make_record(), configuration)

    assert [signal.name for signal in signals] == [
        "model_id",
        "operation",
        "request_metadata",
    ]
    assert all(signal.availability is SignalAvailability.AVAILABLE for signal in signals)
    assert signals[0].provenance is SignalProvenance.DIRECT_OBSERVATION
    assert signals[2].source_field_class is SourceFieldClass.REQUEST_METADATA
    assert "input_token_count" not in {signal.name for signal in signals}


def test_identity_metadata_requires_explicit_category_allowlist():
    identity = IdentityMetadata(
        identity_arn="arn:aws:iam::123456789012:role/role",
        session_name="session",
        name="name",
        account_owner="owner",
    )
    record = make_record(identity_metadata=identity)

    implicit = extract_signals(record, make_configuration("operation"))
    explicit = extract_signals(make_record(identity_metadata=identity), make_configuration("identity_arn"))

    assert {signal.name for signal in implicit} == {"operation"}
    assert [signal.name for signal in explicit] == ["identity_arn"]
    assert explicit[0].value == identity.identity_arn


def test_direct_values_are_bounded_and_malformed_values_are_not_coerced():
    configuration = make_configuration(
        "operation",
        "input_token_count",
        "request_metadata",
    )
    record = make_record(
        operation=" x " + "z" * MAX_SIGNAL_STRING_LENGTH,
        input_token_count=MAX_TOKEN_COUNT + 1,
        request_metadata={
            **{f"key-{index}": "v" for index in range(MAX_METADATA_ITEMS + 5)},
            "prompt": "sensitive prompt text",
            "long": "x" * (MAX_METADATA_VALUE_LENGTH + 20),
        },
    )

    signals = {signal.name: signal for signal in extract_signals(record, configuration)}

    assert len(signals["operation"].value) <= MAX_SIGNAL_STRING_LENGTH
    assert signals["input_token_count"].value == MAX_TOKEN_COUNT
    assert len(signals["request_metadata"].value) <= MAX_METADATA_ITEMS
    assert "prompt" not in signals["request_metadata"].value
    assert all(
        len(value) <= MAX_METADATA_VALUE_LENGTH
        for value in signals["request_metadata"].value.values()
    )

    malformed = extract_signals(
        replace(record, input_token_count=-1),
        configuration,
    )
    malformed_signal = next(signal for signal in malformed if signal.name == "input_token_count")
    assert malformed_signal.value is None
    assert malformed_signal.availability is SignalAvailability.MALFORMED

    malformed_metadata = extract_signals(
        replace(record, request_metadata={"team": 1}),
        configuration,
    )
    metadata_signal = next(signal for signal in malformed_metadata if signal.name == "request_metadata")
    assert metadata_signal.value is None
    assert metadata_signal.availability is SignalAvailability.MALFORMED


def test_content_signals_fail_closed_until_content_is_enabled_and_redacted():
    disabled = make_configuration("prompt_task_marker")
    disabled_signals = extract_signals(
        make_record(sensitive_content=SensitiveContent("Please complete this task")),
        disabled,
    )
    assert disabled_signals[0].availability is SignalAvailability.UNAVAILABLE

    enabled = make_configuration(
        "prompt_task_marker",
        content_analysis=ContentAnalysisPolicy(
            state=ContentAnalysisState.ENABLED,
            approved_accounts=("123456789012",),
            approved_regions=("us-east-1",),
            access_policy_reference="approved-policy",
            retention_days=30,
            max_preview_length=100,
        ),
    )
    not_redacted = extract_signals(
        make_record(
            sensitive_content=SensitiveContent(
                "Please complete this task",
                redaction_status=RedactionStatus.NOT_REQUESTED,
            )
        ),
        enabled,
    )
    redacted = extract_signals(
        make_record(
            sensitive_content=SensitiveContent(
                "Please complete this task",
                redaction_status=RedactionStatus.REDACTED,
            )
        ),
        enabled,
    )

    assert not_redacted[0].availability is SignalAvailability.REDACTED
    assert redacted[0].availability is SignalAvailability.AVAILABLE
    assert redacted[0].provenance is SignalProvenance.CONTENT_DERIVED
    assert redacted[0].value is True


def test_disabled_configuration_emits_no_signals():
    assert extract_signals(make_record(), make_configuration("operation", enabled=False)) == ()
