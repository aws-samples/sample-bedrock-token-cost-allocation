"""Example-based tests for source-row normalization and format boundaries."""

from datetime import datetime, timezone

import pytest

from bedrock_usage_insights.normalization import (
    AnthropicModelFormatAdapter,
    CohereModelFormatAdapter,
    InvocationRecordNormalizer,
    LlamaModelFormatAdapter,
    MAX_DIAGNOSTIC_DETAIL_LENGTH,
    MistralModelFormatAdapter,
    NovaModelFormatAdapter,
    TitanModelFormatAdapter,
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


def test_valid_flattened_record_preserves_traceability_tokens_and_identity_metadata():
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


def test_nullable_fields_are_absent_without_fabricating_values():
    result = normalize(
        make_row(
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
    )

    assert result.record is not None
    assert result.errors == ()
    assert result.record.input_token_count is None
    assert result.record.output_token_count is None
    assert result.record.cache_read_token_count is None
    assert result.record.cache_write_token_count is None
    assert result.record.request_metadata == {}
    assert result.record.stop_reason is None
    assert result.record.content_type is None
    assert result.record.identity_metadata is None


@pytest.mark.parametrize(
    "row_change, expected_code",
    [
        ({"usage_input_tokens": "not-a-count"}, "MALFORMED_TOKEN_COUNT"),
        ({"usage_output_tokens": -1}, "MALFORMED_TOKEN_COUNT"),
        ({"requestmetadata": "not-json"}, "MALFORMED_REQUEST_METADATA"),
        ({"requestmetadata": {"team": 7}}, "MALFORMED_REQUEST_METADATA"),
        ({"input_body_json": '{"messages": ['}, "MALFORMED_MODEL_JSON"),
        ({"output_body_json": "[1, 2]"}, "MALFORMED_MODEL_JSON"),
    ],
)
def test_malformed_optional_values_are_classified_without_exposing_values(row_change, expected_code):
    result = normalize(make_row(**row_change))

    assert result.record is not None
    assert any(error.code == expected_code for error in result.errors)
    assert all(len(error.detail) <= MAX_DIAGNOSTIC_DETAIL_LENGTH for error in result.errors)
    assert all("not-a-count" not in error.detail for error in result.errors)
    assert all("messages" not in error.detail for error in result.errors)


def test_malformed_optional_text_is_diagnosed_without_inventing_a_value():
    result = normalize(make_row(stop_reason={"unexpected": "object"}))

    assert result.record is not None
    assert result.record.stop_reason is None
    assert result.error is not None
    assert result.error.code == "MALFORMED_SOURCE_FIELD"


@pytest.mark.parametrize(
    "adapter, model_id, declared_format",
    [
        (NovaModelFormatAdapter(), "amazon.nova-pro-v1:0", "nova"),
        (AnthropicModelFormatAdapter(), "anthropic.claude-3", "anthropic"),
        (TitanModelFormatAdapter(), "amazon.titan-text", "titan"),
        (CohereModelFormatAdapter(), "cohere.command", "cohere"),
        (LlamaModelFormatAdapter(), "meta.llama3", "llama"),
        (MistralModelFormatAdapter(), "mistral.mistral-large", "mistral"),
    ],
)
def test_explicit_model_adapters_validate_their_own_object_response_formats(
    adapter, model_id, declared_format
):
    result = InvocationRecordNormalizer(adapters=(adapter,)).normalize(
        make_row(
            modelid=model_id,
            model_format=declared_format,
            input_body_json='{"request": {}}',
            output_body_json='{"response": {}}',
        ),
        analysis_run_id="run-1",
        configuration_version="config-1",
        analysis_logic_version="logic-1",
    )

    assert result.record is not None
    assert result.record.model_format == declared_format
    assert result.errors == ()


def test_explicit_unsupported_format_does_not_fall_back_to_another_adapter():
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


def test_unknown_model_with_raw_json_is_unsupported_instead_of_cross_format_extrapolation():
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


def test_identity_fields_are_restricted_metadata_and_do_not_change_normalized_usage_fields():
    baseline = normalize(make_row()).record
    changed_identity = normalize(
        make_row(
            identity_arn="arn:aws:sts::123456789012:assumed-role/other/other-session",
            identity_session_name="other-session",
            identity_name="other-user",
            account_owner="other-owner@example.invalid",
        )
    ).record

    assert baseline is not None
    assert changed_identity is not None
    assert (
        baseline.source_request_id,
        baseline.source_timestamp_utc,
        baseline.source_account,
        baseline.source_region,
        baseline.model_id,
        baseline.operation,
        baseline.input_token_count,
        baseline.output_token_count,
        baseline.request_metadata,
        baseline.model_format,
    ) == (
        changed_identity.source_request_id,
        changed_identity.source_timestamp_utc,
        changed_identity.source_account,
        changed_identity.source_region,
        changed_identity.model_id,
        changed_identity.operation,
        changed_identity.input_token_count,
        changed_identity.output_token_count,
        changed_identity.request_metadata,
        changed_identity.model_format,
    )
    assert baseline.identity_metadata != changed_identity.identity_metadata
    assert baseline.to_dict()["identity_metadata"] != changed_identity.to_dict()["identity_metadata"]


@pytest.mark.parametrize(
    "row_change, expected_code",
    [
        ({"requestid": None}, "MISSING_TRACEABILITY"),
        ({"timestamp": "2026-02-03T04:05:06"}, "MALFORMED_TIMESTAMP"),
        ({"accountid": None}, "MISSING_TRACEABILITY"),
        ({"region": ""}, "INVALID_TRACEABILITY"),
        ({"modelid": None}, "MISSING_TRACEABILITY"),
        ({"operation": None}, "MISSING_TRACEABILITY"),
    ],
)
def test_traceability_failure_returns_no_record_and_never_invents_metadata(row_change, expected_code):
    result = normalize(make_row(**row_change))

    assert result.record is None
    assert any(error.code == expected_code for error in result.errors)


def test_missing_run_context_is_a_traceability_failure():
    result = normalize(make_row(), analysis_run_id="")

    assert result.record is None
    assert result.error is not None
    assert result.error.code == "MISSING_TRACEABILITY"


def test_sensitive_content_is_opt_in():
    row = make_row(prompt_preview="private prompt", response_preview="private response")

    default_result = normalize(row)
    opted_in_result = normalize(row, include_sensitive=True)

    assert default_result.record is not None
    assert opted_in_result.record is not None
    assert default_result.record.sensitive_content is None
    assert opted_in_result.record.sensitive_content is not None
    assert opted_in_result.record.sensitive_content.prompt_preview == "private prompt"
    assert opted_in_result.record.sensitive_content.response_preview == "private response"
