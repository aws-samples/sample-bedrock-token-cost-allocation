"""Unit tests for explicit source-row and model-format normalization."""

from datetime import datetime, timezone

from bedrock_usage_insights.normalization import (
    InvocationRecordNormalizer,
    MAX_DIAGNOSTIC_DETAIL_LENGTH,
)


UTC = timezone.utc


def make_row(**changes):
    row = {
        "requestid": "request-123",
        "timestamp": "2026-02-03T04:05:06Z",
        "accountid": "123456789012",
        "region": "us-east-1",
        "modelid": "amazon.nova-pro-v1:0",
        "operation": "InvokeModel",
        "usage_input_tokens": "11",
        "usage_output_tokens": 22,
        "usage_cache_read_tokens": None,
        "usage_cache_write_tokens": 3,
        "stop_reason": "end_turn",
        "content_type": "application/json",
        "requestmetadata": {"team": "platform"},
        "identity_arn": "arn:aws:sts::123456789012:assumed-role/role/session",
        "identity_session_name": "session",
        "identity_name": "example-user",
        "account_owner": "owner@example.invalid",
    }
    row.update(changes)
    return row


def normalize(row, **changes):
    return InvocationRecordNormalizer().normalize(
        row,
        analysis_run_id=changes.pop("analysis_run_id", "run-1"),
        configuration_version=changes.pop("configuration_version", "config-1"),
        analysis_logic_version=changes.pop("analysis_logic_version", "logic-1"),
        **changes,
    )


def test_flattened_view_aliases_preserve_traceability_tokens_and_protected_identity():
    result = normalize(make_row())

    assert result.error is None
    assert result.record is not None
    assert result.record.source_request_id == "request-123"
    assert result.record.source_timestamp_utc == datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    assert result.record.source_account == "123456789012"
    assert result.record.source_region == "us-east-1"
    assert result.record.model_id == "amazon.nova-pro-v1:0"
    assert result.record.operation == "InvokeModel"
    assert result.record.analysis_run_id == "run-1"
    assert result.record.configuration_version == "config-1"
    assert result.record.analysis_logic_version == "logic-1"
    assert result.record.input_token_count == 11
    assert result.record.output_token_count == 22
    assert result.record.cache_read_token_count is None
    assert result.record.cache_write_token_count == 3
    assert result.record.request_metadata == {"team": "platform"}
    assert result.record.identity_metadata is not None
    assert result.record.identity_metadata.identity_arn.endswith("/role/session")
    assert result.record.identity_metadata.session_name == "session"
    assert result.record.identity_metadata.name == "example-user"
    assert result.record.identity_metadata.account_owner == "owner@example.invalid"


def test_nullable_optional_fields_are_tolerated_without_fabricating_values():
    row = make_row(
        usage_input_tokens=None,
        usage_output_tokens=None,
        usage_cache_read_tokens=None,
        usage_cache_write_tokens=None,
        stop_reason=None,
        content_type=None,
        requestmetadata=None,
        identity_arn=None,
        identity_session_name=None,
        identity_name=None,
        account_owner=None,
    )

    result = normalize(row)

    assert result.record is not None
    assert result.errors == ()
    assert result.record.input_token_count is None
    assert result.record.output_token_count is None
    assert result.record.request_metadata == {}
    assert result.record.stop_reason is None
    assert result.record.identity_metadata is None


def test_malformed_optional_values_return_record_with_bounded_content_free_diagnostic():
    result = normalize(
        make_row(
            usage_input_tokens="not-a-count",
            requestmetadata="not-json",
            input_body_json='{"messages": [',
        )
    )

    assert result.record is not None
    assert {error.code for error in result.errors} == {
        "MALFORMED_TOKEN_COUNT",
        "MALFORMED_REQUEST_METADATA",
        "MALFORMED_MODEL_JSON",
    }
    assert all(len(error.detail) <= MAX_DIAGNOSTIC_DETAIL_LENGTH for error in result.errors)
    assert all("not-a-count" not in error.detail for error in result.errors)
    assert all("messages" not in error.detail for error in result.errors)
    assert result.record.input_token_count is None
    assert result.record.request_metadata == {}


def test_malformed_optional_text_is_diagnosed_without_inventing_a_value():
    result = normalize(make_row(stop_reason={"unexpected": "object"}))

    assert result.record is not None
    assert result.record.stop_reason is None
    assert result.error is not None
    assert result.error.code == "MALFORMED_SOURCE_FIELD"


def test_explicit_unsupported_format_does_not_use_a_different_adapter():
    result = normalize(
        make_row(
            model_format="future-model-format",
            input_body_json='{"messages": [{"content": "prompt"}]}',
            output_body_json='{"completion": "response"}',
        )
    )

    assert result.record is not None
    assert result.record.model_format == "future_model_format"
    assert any(error.code == "UNSUPPORTED_MODEL_FORMAT" for error in result.errors)
    assert not any(error.code == "MALFORMED_MODEL_JSON" for error in result.errors)


def test_known_model_adapter_validates_only_its_own_json_shape():
    result = normalize(
        make_row(
            model_format="nova",
            input_body_json='{"messages": [{"content": [{"text": "prompt"}]}]}',
            output_body_json='{"output": {"message": {"content": [{"text": "response"}]}}}',
        )
    )

    assert result.record is not None
    assert result.record.model_format == "nova"
    assert result.errors == ()
    assert result.record.sensitive_content is None


def test_sensitive_content_requires_explicit_opt_in():
    row = make_row(prompt_preview="private prompt", response_preview="private response")

    default_result = normalize(row)
    opted_in_result = normalize(row, include_sensitive=True)

    assert default_result.record is not None
    assert opted_in_result.record is not None
    assert default_result.record.sensitive_content is None
    assert opted_in_result.record.sensitive_content is not None
    assert opted_in_result.record.sensitive_content.prompt_preview == "private prompt"
    assert opted_in_result.record.sensitive_content.response_preview == "private response"


def test_missing_or_malformed_traceability_metadata_returns_no_record():
    missing_result = normalize(make_row(requestid=None))
    malformed_result = normalize(make_row(timestamp="2026-02-03T04:05:06"))
    missing_context_result = normalize(make_row(), analysis_run_id="")

    assert missing_result.record is None
    assert any(error.code == "MISSING_TRACEABILITY" for error in missing_result.errors)
    assert malformed_result.record is None
    assert any(error.code == "MALFORMED_TIMESTAMP" for error in malformed_result.errors)
    assert missing_context_result.record is None
    assert missing_context_result.error is not None
    assert missing_context_result.error.code == "MISSING_TRACEABILITY"


def test_unknown_model_with_raw_body_is_unsupported_instead_of_cross_format_extrapolation():
    result = normalize(
        make_row(
            modelid="vendor.unknown-model-v1",
            input_body_json='{"prompt": "text"}',
            output_body_json='{"completion": "text"}',
        )
    )

    assert result.record is not None
    assert any(error.code == "UNSUPPORTED_MODEL_FORMAT" for error in result.errors)
    assert result.record.model_format is None
