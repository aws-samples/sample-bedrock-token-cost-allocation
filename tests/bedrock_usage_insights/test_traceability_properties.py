"""Property tests for traceability preservation and failure-closed metadata."""

from datetime import datetime, timezone
from typing import Any

from hypothesis import given, settings, strategies as st

from bedrock_usage_insights.models import InsightsResult, InvocationRecord
from bedrock_usage_insights.oracle import CanonicalSchemaOracle

UTC = timezone.utc

_TRACEABILITY_FIELDS = (
    "source_request_id",
    "source_timestamp_utc",
    "source_account",
    "source_region",
    "model_id",
    "analysis_run_id",
    "configuration_version",
    "analysis_logic_version",
)
_IDENTIFIER = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9:./_-]{0,31}", fullmatch=True)
_ACCOUNT = st.from_regex(r"[0-9]{12}", fullmatch=True)
_UTC_DATETIME = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31, 23, 59, 59),
    timezones=st.just(UTC),
)


def _record(**changes: Any) -> InvocationRecord:
    values: dict[str, Any] = {
        "source_request_id": "request-1",
        "source_timestamp_utc": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "source_account": "123456789012",
        "source_region": "us-east-1",
        "model_id": "amazon.nova-pro-v1:0",
        "operation": "InvokeModel",
        "analysis_run_id": "run-1",
        "configuration_version": "config-v1",
        "analysis_logic_version": "logic-v1",
    }
    values.update(changes)
    return InvocationRecord(**values)


def _result_from_record(
    record: InvocationRecord,
    result_timestamp_utc: datetime,
) -> InsightsResult:
    """Project normalized metadata into a result without filling any fields."""

    return InsightsResult(
        source_request_id=record.source_request_id,
        source_timestamp_utc=record.source_timestamp_utc,
        result_timestamp_utc=result_timestamp_utc,
        source_account=record.source_account,
        source_region=record.source_region,
        model_id=record.model_id,
        analysis_run_id=record.analysis_run_id,
        configuration_version=record.configuration_version,
        analysis_logic_version=record.analysis_logic_version,
    )


@st.composite
def valid_records(draw: st.DrawFn) -> tuple[InvocationRecord, datetime]:
    record = _record(
        source_request_id=draw(_IDENTIFIER),
        source_timestamp_utc=draw(_UTC_DATETIME),
        source_account=draw(_ACCOUNT),
        source_region=draw(_IDENTIFIER),
        model_id=draw(_IDENTIFIER),
        analysis_run_id=draw(_IDENTIFIER),
        configuration_version=draw(_IDENTIFIER),
        analysis_logic_version=draw(_IDENTIFIER),
    )
    return record, draw(_UTC_DATETIME)


@st.composite
def invalid_traceability_cases(draw: st.DrawFn) -> tuple[InvocationRecord, str, datetime]:
    record, result_timestamp_utc = draw(valid_records())
    field_name = draw(st.sampled_from(_TRACEABILITY_FIELDS))
    if field_name == "source_timestamp_utc":
        malformed_value = draw(
            st.one_of(
                st.none(),
                st.datetimes(
                    min_value=datetime(2020, 1, 1),
                    max_value=datetime(2030, 12, 31, 23, 59, 59),
                    timezones=st.none(),
                ),
                st.sampled_from(("not-a-timestamp", 0, False)),
            )
        )
    else:
        malformed_value = draw(st.sampled_from((None, "", "   ", 0, False)))
    return _record(**{field_name: malformed_value}), field_name, result_timestamp_utc


# Feature: bedrock-usage-insights, Property 3: Traceability preservation and failure-closed metadata
# **Validates: Requirements 1.4, 6.1, 6.5, 8.2**
@given(valid_records())
@settings(max_examples=100, deadline=None)
def test_property_3_valid_normalized_records_preserve_traceability(case):
    record, result_timestamp_utc = case
    result = _result_from_record(record, result_timestamp_utc)

    assert not CanonicalSchemaOracle.validate_invocation(record)
    assert not CanonicalSchemaOracle.validate_result(result)
    for field_name in _TRACEABILITY_FIELDS:
        assert getattr(result, field_name) == getattr(record, field_name)


@given(invalid_traceability_cases())
@settings(max_examples=100, deadline=None)
def test_property_3_missing_or_malformed_metadata_is_invalid_without_fabrication(case):
    record, invalid_field, result_timestamp_utc = case
    result = _result_from_record(record, result_timestamp_utc)

    invocation_issues = CanonicalSchemaOracle.validate_invocation(record)
    result_issues = CanonicalSchemaOracle.validate_result(result)

    assert any(issue.path == invalid_field for issue in invocation_issues)
    assert any(issue.path == invalid_field for issue in result_issues)
    assert result_issues
    for field_name in _TRACEABILITY_FIELDS:
        assert getattr(result, field_name) == getattr(record, field_name)
