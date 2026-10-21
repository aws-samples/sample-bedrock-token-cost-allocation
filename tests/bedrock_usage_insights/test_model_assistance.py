from datetime import date, datetime, timezone

from bedrock_usage_insights.classification import classify
from bedrock_usage_insights.model_assistance import (
    ModelAssistedFeatureAdapter,
    ModelError,
    ModelErrorCode,
    ProposedFeatures,
)
from bedrock_usage_insights.prompt_assessment import assess_prompt
from bedrock_usage_insights.models import (
    AnalysisScope,
    CategoryDefinition,
    ConfidenceBand,
    InsightConfiguration,
    ModelAssistancePolicy,
    PromptAssessmentStatus,
    PromptDimension,
    PromptDimensionConfiguration,
    RedactionStatus,
    SensitiveContent,
    Signal,
    SignalAvailability,
    SignalDefinition,
    SignalProvenance,
    SourceFieldClass,
    UnknownReason,
)

UTC = timezone.utc


def policy(**changes) -> ModelAssistancePolicy:
    values = {
        "enabled": True,
        "approved": True,
        "endpoint_reference": "endpoint/model-features",
        "model_id": "model-a",
        "model_version": "2026-01",
        "allowed_features": ("prompt_task_marker",),
        "response_schema_version": "1",
        "max_input_length": 24,
        "timeout_seconds": 0.25,
        "max_attempts": 2,
    }
    values.update(changes)
    return ModelAssistancePolicy(**values)


def content(status=RedactionStatus.REDACTED) -> SensitiveContent:
    return SensitiveContent(
        prompt_preview="Please create a short report.",
        response_preview=None,
        redaction_status=status,
    )


def deterministic_configuration() -> InsightConfiguration:
    return InsightConfiguration(
        configuration_version="model-assistance-fallback-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="model-assistance-tests",
        change_reason="deterministic fallback examples",
        analysis_scope=AnalysisScope(
            accounts=("123456789012",),
            regions=("us-east-1",),
            models=("model-a",),
            operations=("InvokeModel",),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        ),
        categories=(
            CategoryDefinition(
                category_id="candidate",
                display_name="Candidate",
                description="A likely signal pattern.",
                signals=("operation", "model_id"),
                evidence_threshold=0.5,
            ),
        ),
        signal_definitions=(
            SignalDefinition(
                name="operation",
                source_field_class=SourceFieldClass.DIRECT_METADATA,
                provenance=SignalProvenance.DIRECT_OBSERVATION,
            ),
            SignalDefinition(
                name="model_id",
                source_field_class=SourceFieldClass.DIRECT_METADATA,
                provenance=SignalProvenance.DIRECT_OBSERVATION,
            ),
        ),
        enabled=True,
        confidence_thresholds={"low": 0.0, "medium": 0.8, "high": 0.9},
    )


def direct_signal(name: str) -> Signal:
    return Signal(
        name=name,
        value_type="string",
        value=name,
        source_field_class=SourceFieldClass.DIRECT_METADATA,
        provenance=SignalProvenance.DIRECT_OBSERVATION,
        availability=SignalAvailability.AVAILABLE,
        summary=f"{name} was observed.",
    )


def valid_response():
    return {
        "schema_version": "1",
        "model_id": "model-a",
        "model_version": "2026-01",
        "features": [
            {
                "name": "prompt_task_marker",
                "value": 1.0,
                "evidence": {
                    "signal_names": ["prompt_task_marker"],
                    "source_field_classes": ["permitted_content_derived_feature"],
                    "summaries": ["task marker observed"],
                    "counts": {"prompt_task_marker": 1},
                },
            }
        ],
    }


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.timeouts = []

    def invoke(self, request, **kwargs):
        self.requests.append(request)
        self.timeouts.append(kwargs["timeout_seconds"])
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


def test_disabled_policy_does_not_call_model():
    client = FakeClient([valid_response()])
    result = ModelAssistedFeatureAdapter(client).extract_features(content(), policy(enabled=False))

    assert isinstance(result, ModelError)
    assert result.code is ModelErrorCode.DISABLED
    assert client.requests == []


def test_requires_approval_before_endpoint_or_redaction_checks():
    client = FakeClient([valid_response()])
    adapter = ModelAssistedFeatureAdapter(
        client,
        approved_endpoint_references={"endpoint/model-features"},
    )

    result = adapter.extract_features(
        content(RedactionStatus.NOT_REQUESTED),
        policy(approved=False),
    )

    assert isinstance(result, ModelError)
    assert result.code is ModelErrorCode.APPROVAL_REQUIRED
    assert client.requests == []


def test_rejects_endpoint_and_redaction_before_invoking_model():
    client = FakeClient([valid_response()])
    adapter = ModelAssistedFeatureAdapter(client, approved_endpoint_references={"other-endpoint"})

    endpoint_result = adapter.extract_features(content(), policy())
    content_result = ModelAssistedFeatureAdapter(
        client,
        approved_endpoint_references={"endpoint/model-features"},
    ).extract_features(content(RedactionStatus.NOT_REQUESTED), policy())

    assert isinstance(endpoint_result, ModelError)
    assert endpoint_result.code is ModelErrorCode.ENDPOINT_REJECTED
    assert isinstance(content_result, ModelError)
    assert content_result.code is ModelErrorCode.CONTENT_REDACTED
    assert client.requests == []


def test_minimizes_redacted_content_and_returns_advisory_features_only():
    client = FakeClient([valid_response()])
    adapter = ModelAssistedFeatureAdapter(client, approved_endpoint_references={"endpoint/model-features"})

    result = adapter.extract_features(content(), policy())

    assert isinstance(result, ProposedFeatures)
    assert [feature.name for feature in result.features] == ["prompt_task_marker"]
    assert len(client.requests[0]["content"]["prompt"]) <= 24
    assert "category" not in client.requests[0]
    assert "publication" not in result.to_dict()
    assert client.timeouts == [0.25]


def test_rejects_malformed_response_before_any_fallback():
    for malformed_response in (
        None,
        [],
        {
            "schema_version": "1",
            "model_id": "model-a",
            "model_version": "2026-01",
            "features": None,
        },
    ):
        client = FakeClient([malformed_response])
        result = ModelAssistedFeatureAdapter(client).extract_features(content(), policy())

        assert isinstance(result, ModelError)
        assert result.code is ModelErrorCode.INVALID_RESPONSE
        assert len(client.requests) == 1


def test_rejects_model_version_and_category_or_publication_fields():
    client = FakeClient([
        {**valid_response(), "model_version": "old"},
    ])
    adapter = ModelAssistedFeatureAdapter(client)

    result = adapter.extract_features(content(), policy())

    assert isinstance(result, ModelError)
    assert result.code is ModelErrorCode.UNSUPPORTED_MODEL_VERSION

    client = FakeClient([{**valid_response(), "category": "code_development"}])
    result = ModelAssistedFeatureAdapter(client).extract_features(content(), policy())
    assert isinstance(result, ModelError)
    assert result.code is ModelErrorCode.INVALID_RESPONSE


def test_rejects_unbounded_feature_and_content_bearing_evidence():
    response = valid_response()
    response["features"][0]["value"] = 2.0
    client = FakeClient([response])
    result = ModelAssistedFeatureAdapter(client).extract_features(content(), policy())
    assert isinstance(result, ModelError)
    assert result.code is ModelErrorCode.INVALID_RESPONSE

    response = valid_response()
    response["features"][0]["evidence"]["summaries"] = ["Please create a short report."]
    client = FakeClient([response])
    result = ModelAssistedFeatureAdapter(client).extract_features(content(), policy())
    assert isinstance(result, ModelError)
    assert result.code is ModelErrorCode.INVALID_RESPONSE


def test_retries_timeout_then_returns_valid_advisory_features():
    client = FakeClient([TimeoutError(), valid_response()])
    sleeps = []
    adapter = ModelAssistedFeatureAdapter(client, sleeper=sleeps.append)

    result = adapter.extract_features(content(), policy(max_attempts=2))

    assert isinstance(result, ProposedFeatures)
    assert len(client.requests) == 2
    assert sleeps == [0.05]
    assert all(request["content"] for request in client.requests)


def test_retries_timeout_with_bounded_attempts_and_maps_failure():
    client = FakeClient([TimeoutError(), TimeoutError(), TimeoutError()])
    sleeps = []
    adapter = ModelAssistedFeatureAdapter(
        client,
        sleeper=sleeps.append,
    )

    result = adapter.extract_features(content(), policy(max_attempts=2))

    assert isinstance(result, ModelError)
    assert result.code is ModelErrorCode.RETRY_EXHAUSTED
    assert result.attempts == 2
    assert result.retryable is False
    assert len(client.requests) == 2
    assert sleeps == [0.05]


def test_model_failures_are_content_free_inputs_to_deterministic_fallbacks():
    client = FakeClient([TimeoutError()])
    result = ModelAssistedFeatureAdapter(client).extract_features(
        content(),
        policy(max_attempts=1),
    )

    assert isinstance(result, ModelError)
    assert result.code is ModelErrorCode.RETRY_EXHAUSTED
    serialized = result.to_dict()
    assert set(serialized) == {"code", "detail", "retryable", "attempts"}
    assert "prompt" not in serialized["detail"].lower()
    assert "response" not in serialized["detail"].lower()
    assert not hasattr(result, "primary_category")
    assert not hasattr(result, "confidence_band")


def test_model_failures_fall_back_to_not_assessed_unknown_or_low_confidence():
    client = FakeClient([TimeoutError()])
    model_result = ModelAssistedFeatureAdapter(client).extract_features(
        content(),
        policy(max_attempts=1),
    )
    assert isinstance(model_result, ModelError)

    not_assessed = assess_prompt(
        None,
        {
            dimension.value: PromptDimensionConfiguration(enabled=True)
            for dimension in PromptDimension
        },
    )
    assert all(
        result.status is PromptAssessmentStatus.NOT_ASSESSED
        and result.not_assessed_reason == "content_unavailable"
        for result in not_assessed.dimensions.values()
    )

    configuration = deterministic_configuration()
    unknown = classify((), configuration)
    low_confidence = classify((direct_signal("operation"),), configuration)

    assert unknown.unknown_reason is UnknownReason.INSUFFICIENT_EVIDENCE
    assert unknown.primary_category is None
    assert low_confidence.primary_category == "candidate"
    assert low_confidence.confidence_band is ConfidenceBand.LOW


def test_configuration_policy_can_be_disabled_without_model_fields():
    disabled = ModelAssistancePolicy()
    client = FakeClient([valid_response()])

    result = ModelAssistedFeatureAdapter(client).extract_features(content(), disabled)

    assert isinstance(result, ModelError)
    assert result.code is ModelErrorCode.DISABLED
