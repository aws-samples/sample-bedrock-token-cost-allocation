"""Contract tests for the bedrock_usage_insights canonical models and oracle."""

from datetime import date, datetime, timezone

import pytest

from bedrock_usage_insights.models import (
    Applicability,
    ApplicabilityMarkers,
    AnalysisScope,
    AggregateInsightResult,
    CategoryDefinition,
    ClassificationResult,
    ConfidenceBand,
    DEFAULT_DERIVED_RESULT_FIELDS,
    ContentAnalysisPolicy,
    ContentAnalysisState,
    EvidenceReference,
    ExposureLevel,
    InsightConfiguration,
    InsightsResult,
    InvocationRecord,
    LabelMetadata,
    PromptAssessmentStatus,
    PromptQualityAssessment,
    PromptQualityDimensionResult,
    SensitiveContent,
    SENSITIVE_CONTENT_FIELDS,
    UsageCategory,
    UnknownReason,
)
from bedrock_usage_insights.oracle import (
    CanonicalSchemaOracle,
    ConfigurationSchemaOracle,
    SCHEMA_ORACLE,
    SchemaValidationError,
)

UTC = timezone.utc


def make_scope() -> AnalysisScope:
    return AnalysisScope(
        accounts=("123456789012",),
        regions=("us-east-1",),
        models=("amazon.nova-pro-v1:0",),
        operations=("InvokeModel",),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 6, 30),
    )


def make_configuration(**changes) -> InsightConfiguration:
    values = {
        "configuration_version": "2026-01-01T00:00:00Z-v1",
        "effective_at": datetime(2026, 1, 1, tzinfo=UTC),
        "author_or_service": "test-service",
        "change_reason": "contract test",
        "analysis_scope": make_scope(),
    }
    values.update(changes)
    return InsightConfiguration(**values)


def make_record(**changes) -> InvocationRecord:
    values = {
        "source_request_id": "request-1",
        "source_timestamp_utc": datetime(2026, 2, 3, 4, 5, tzinfo=UTC),
        "source_account": "123456789012",
        "source_region": "us-east-1",
        "model_id": "amazon.nova-pro-v1:0",
        "operation": "InvokeModel",
        "analysis_run_id": "run-1",
        "configuration_version": "2026-01-01T00:00:00Z-v1",
        "analysis_logic_version": "1.0",
        "input_token_count": 10,
        "output_token_count": 20,
    }
    values.update(changes)
    return InvocationRecord(**values)


def test_initial_catalog_and_privacy_defaults_are_encoded():
    configuration = make_configuration()
    category_ids = {category.category_id for category in configuration.categories}

    assert category_ids == {
        "code_development",
        "personal_work",
        "potentially_wasteful_work",
        "other",
        "insufficient_evidence",
    }
    personal = next(category for category in configuration.categories if category.category_id == "personal_work")
    assert personal.exposure_level is not ExposureLevel.INDIVIDUAL
    assert configuration.privacy.minimum_group_size == 10
    assert configuration.content_analysis.state is ContentAnalysisState.DISABLED
    assert configuration.privacy.exclude_raw_content_from_derived_output is True
    assert not ConfigurationSchemaOracle.validate(configuration)


def test_schema_oracle_exposes_stable_enums_and_default_derived_exclusions():
    assert set(SCHEMA_ORACLE["categories"]) == {category.value for category in UsageCategory}
    assert SCHEMA_ORACLE["minimum_group_size_default"] == 10
    assert SCHEMA_ORACLE["minimum_group_size_minimum"] == 5
    assert "prompt_preview" in SCHEMA_ORACLE["derived_result_forbidden_fields"]
    assert "response_preview" in SCHEMA_ORACLE["derived_result_forbidden_fields"]


def test_schema_oracle_exposes_all_stable_enum_values_and_default_exclusions():
    assert SCHEMA_ORACLE["categories"] == tuple(category.value for category in UsageCategory)
    assert SCHEMA_ORACLE["confidence_bands"] == tuple(band.value for band in ConfidenceBand)
    assert SCHEMA_ORACLE["unknown_reasons"] == tuple(reason.value for reason in UnknownReason)
    assert set(DEFAULT_DERIVED_RESULT_FIELDS).isdisjoint(SENSITIVE_CONTENT_FIELDS)
    assert SENSITIVE_CONTENT_FIELDS == {"prompt_preview", "response_preview"}


def test_applicability_markers_match_present_result_components():
    cases = (
        ("primary_category", ClassificationResult(primary_category="code_development")),
        ("secondary_categories", ClassificationResult(secondary_categories=("other",))),
        ("scores", ClassificationResult(category_scores={"other": 0.5})),
        ("confidence_band", ClassificationResult(confidence_band=ConfidenceBand.LOW)),
        (
            "evidence_reference",
            ClassificationResult(evidence_reference=EvidenceReference(signal_names=("operation",))),
        ),
        ("unknown_reason", ClassificationResult(unknown_reason=UnknownReason.PARSE_ERROR)),
    )

    for marker_name, classification in cases:
        issues = CanonicalSchemaOracle.validate_classification(classification)
        assert any(issue.path == f"applicability.{marker_name}" for issue in issues)


def test_configuration_version_metadata_is_required_and_utc():
    configuration = make_configuration(configuration_version="", effective_at=datetime(2026, 1, 1))

    paths = {issue.path for issue in ConfigurationSchemaOracle.validate(configuration)}

    assert {"configuration_version", "effective_at"} <= paths


def test_invocation_content_is_explicit_opt_in_and_omitted_by_default():
    record = make_record(
        sensitive_content=SensitiveContent(
            prompt_preview="secret prompt",
            response_preview="secret response",
        )
    )

    default_output = record.to_dict()
    explicit_output = record.to_dict(include_sensitive=True)

    assert "sensitive_content" not in default_output
    assert explicit_output["sensitive_content"]["prompt_preview"] == "secret prompt"
    assert explicit_output["sensitive_content"]["response_preview"] == "secret response"


def test_rejected_configuration_reports_multiple_fields_atomically():
    category = CategoryDefinition(
        category_id="",
        display_name="x" * 101,
        description="y" * 501,
        signals=("unsupported_signal",),
        score_min=0.9,
        score_max=0.1,
        evidence_threshold=float("nan"),
        exposure_level="invalid",  # type: ignore[arg-type]
    )
    configuration = make_configuration(
        categories=(category, category),
        analysis_scope=None,
        content_analysis=ContentAnalysisPolicy(
            state=ContentAnalysisState.ENABLED,
            approved_accounts=(),
            approved_regions=(),
            access_policy_reference=None,
            retention_days=None,
        ),
        privacy=make_configuration().privacy.__class__(minimum_group_size=4),
    )

    issues = ConfigurationSchemaOracle.validate(configuration)
    paths = {issue.path for issue in issues}

    assert "analysis_scope" in paths
    assert "categories" in paths
    assert any(path.startswith("categories[0].") for path in paths)
    assert "content_analysis.approved_accounts" in paths
    assert "content_analysis.access_policy_reference" in paths
    assert "privacy.minimum_group_size" in paths
    with pytest.raises(SchemaValidationError):
        ConfigurationSchemaOracle.assert_valid(configuration)


def test_result_requires_exact_configuration_label_metadata_and_has_no_raw_content():
    configuration = make_configuration()
    classification = ClassificationResult(
        primary_category="code_development",
        confidence_band=ConfidenceBand.MEDIUM,
        category_scores={"code_development": 0.8},
        evidence_thresholds={"code_development": 0.5},
        evidence_reference=EvidenceReference(
            signal_names=("operation",),
            summaries=("direct operation signal",),
        ),
        applicability=ApplicabilityMarkers(
            primary_category=Applicability.APPLICABLE,
            scores=Applicability.APPLICABLE,
            confidence_band=Applicability.APPLICABLE,
            evidence_reference=Applicability.APPLICABLE,
        ),
    )
    result = InsightsResult(
        source_request_id="request-1",
        source_timestamp_utc=datetime(2026, 2, 3, 4, 5, tzinfo=UTC),
        result_timestamp_utc=datetime(2026, 2, 3, 4, 6, tzinfo=UTC),
        source_account="123456789012",
        source_region="us-east-1",
        model_id="amazon.nova-pro-v1:0",
        analysis_run_id="run-1",
        configuration_version=configuration.configuration_version,
        analysis_logic_version="1.0",
        classification=classification,
        label_metadata=configuration.label_metadata,
    )

    assert not CanonicalSchemaOracle.validate_result(result, configuration)
    serialized = result.to_dict()
    assert "prompt_preview" not in serialized
    assert "response_preview" not in serialized
    assert all(label["configuration_version"] == configuration.configuration_version for label in serialized["label_metadata"])


def test_prompt_quality_not_assessed_has_no_score_or_evidence():
    assessment = PromptQualityAssessment(
        dimensions={
            "clarity": PromptQualityDimensionResult(
                dimension="clarity",
                status=PromptAssessmentStatus.NOT_ASSESSED,
                not_assessed_reason="content_disabled",
            )
        }
    )

    assert not CanonicalSchemaOracle.validate_prompt_quality(assessment)


def test_prompt_quality_rejects_invalid_confidence_band_enum():
    assessment = PromptQualityAssessment(
        dimensions={
            "clarity": PromptQualityDimensionResult(
                dimension="clarity",
                status=PromptAssessmentStatus.ASSESSED,
                score=0.5,
                confidence_band="invalid",  # type: ignore[arg-type]
                evidence_reference=EvidenceReference(signal_names=("operation",)),
            )
        }
    )

    assert any(
        issue.path == "dimensions.clarity.confidence_band"
        for issue in CanonicalSchemaOracle.validate_prompt_quality(assessment)
    )


def test_unknown_result_uses_machine_readable_reason():
    classification = ClassificationResult(
        unknown_reason=UnknownReason.CONTENT_REDACTED,
        applicability=ApplicabilityMarkers(unknown_reason=Applicability.APPLICABLE),
    )

    assert not CanonicalSchemaOracle.validate_classification(classification)


def test_zero_record_aggregate_has_unavailable_unknown_proportion():
    aggregate = AggregateInsightResult(
        group_type="account",
        group_key=None,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 1),
        analyzed_record_count=0,
        classified_record_count=0,
        unknown_result_count=0,
        not_assessed_count=0,
        unknown_result_proportion=None,
        input_token_total=0,
        output_token_total=0,
        category_distribution={},
        confidence_coverage={},
        prompt_quality_coverage={},
    )

    assert not CanonicalSchemaOracle.validate_aggregate(aggregate)


def test_result_rejects_label_text_from_a_different_configuration_version():
    configuration = make_configuration()
    label = configuration.label_metadata[0]
    result = InsightsResult(
        source_request_id="request-1",
        source_timestamp_utc=datetime(2026, 2, 3, 4, 5, tzinfo=UTC),
        result_timestamp_utc=datetime(2026, 2, 3, 4, 6, tzinfo=UTC),
        source_account="123456789012",
        source_region="us-east-1",
        model_id="amazon.nova-pro-v1:0",
        analysis_run_id="run-1",
        configuration_version=configuration.configuration_version,
        analysis_logic_version="1.0",
        label_metadata=(
            LabelMetadata(
                label_id=label.label_id,
                display_name="stale label",
                description=label.description,
                threshold_interpretation=label.threshold_interpretation,
                configuration_version=configuration.configuration_version,
            ),
        ),
    )

    assert any(
        issue.path == "label_metadata.code_development"
        for issue in CanonicalSchemaOracle.validate_result(result, configuration)
    )


def test_invocation_and_result_required_traceability_fields_are_validated():
    invocation_issues = CanonicalSchemaOracle.validate_invocation(
        make_record(
            source_request_id=None,
            source_timestamp_utc=None,
            source_account="",
            source_region=None,
            model_id="",
            operation=None,
            analysis_run_id="",
            configuration_version=None,
            analysis_logic_version="",
        )
    )
    invocation_paths = {issue.path for issue in invocation_issues}
    assert {
        "source_request_id",
        "source_timestamp_utc",
        "source_account",
        "source_region",
        "model_id",
        "operation",
        "analysis_run_id",
        "configuration_version",
        "analysis_logic_version",
    } <= invocation_paths

    result = InsightsResult(
        source_request_id=None,
        source_timestamp_utc=None,
        result_timestamp_utc=None,
        source_account="",
        source_region=None,
        model_id="",
        analysis_run_id="",
        configuration_version=None,
        analysis_logic_version="",
    )
    result_paths = {issue.path for issue in CanonicalSchemaOracle.validate_result(result)}
    assert {
        "source_request_id",
        "source_timestamp_utc",
        "result_timestamp_utc",
        "source_account",
        "source_region",
        "model_id",
        "analysis_run_id",
        "configuration_version",
        "analysis_logic_version",
    } <= result_paths


def test_permitted_enums_and_applicability_markers_round_trip():
    markers = ApplicabilityMarkers(
        primary_category=Applicability.APPLICABLE,
        secondary_categories=Applicability.NOT_APPLICABLE,
        scores=Applicability.NOT_APPLICABLE,
        confidence_band=Applicability.APPLICABLE,
        evidence_reference=Applicability.NOT_APPLICABLE,
        unknown_reason=Applicability.NOT_APPLICABLE,
    )
    classification = ClassificationResult(
        primary_category=UsageCategory.CODE_DEVELOPMENT.value,
        confidence_band=ConfidenceBand.HIGH,
        applicability=markers,
    )

    assert not CanonicalSchemaOracle.validate_classification(classification)
    serialized = classification.to_dict()
    assert serialized["confidence_band"] == ConfidenceBand.HIGH.value
    assert serialized["applicability"]["primary_category"] == Applicability.APPLICABLE.value
    assert serialized["applicability"]["unknown_reason"] == Applicability.NOT_APPLICABLE.value


def test_invalid_applicability_marker_is_rejected_by_schema_oracle():
    classification = ClassificationResult(
        applicability=ApplicabilityMarkers(primary_category="invalid"),  # type: ignore[arg-type]
    )

    assert any(
        issue.path == "applicability.primary_category"
        for issue in CanonicalSchemaOracle.validate_classification(classification)
    )


@pytest.mark.parametrize("field_name", ("score_min", "score_max", "evidence_threshold"))
@pytest.mark.parametrize("invalid_value", (-0.01, 1.01, float("nan"), float("inf")))
def test_category_score_and_threshold_bounds_are_inclusive(field_name, invalid_value):
    category_values = {
        "category_id": "custom",
        "display_name": "Custom",
        "description": "A custom category.",
        "signals": ("operation",),
        field_name: invalid_value,
    }
    category = CategoryDefinition(**category_values)
    configuration = make_configuration(categories=(category,))

    assert any(
        issue.path == f"categories[0].{field_name}"
        for issue in ConfigurationSchemaOracle.validate(configuration)
    )


def test_category_score_range_rejects_reversed_bounds():
    category = CategoryDefinition(
        category_id="custom",
        display_name="Custom",
        description="A custom category.",
        signals=("operation",),
        score_min=0.8,
        score_max=0.2,
    )

    assert any(
        issue.path == "categories[0].score_range"
        for issue in ConfigurationSchemaOracle.validate(make_configuration(categories=(category,)))
    )


@pytest.mark.parametrize("field_name", ("category_scores", "evidence_thresholds"))
@pytest.mark.parametrize("invalid_value", (-0.01, 1.01, float("nan"), float("inf")))
def test_classification_scores_and_thresholds_reject_out_of_bounds(field_name, invalid_value):
    classification = ClassificationResult(**{field_name: {"code_development": invalid_value}})

    assert any(
        issue.path == f"{field_name}.code_development"
        for issue in CanonicalSchemaOracle.validate_classification(classification)
    )


def test_exact_configuration_version_is_required_for_result_metadata():
    configuration = make_configuration()
    result = InsightsResult(
        source_request_id="request-1",
        source_timestamp_utc=datetime(2026, 2, 3, 4, 5, tzinfo=UTC),
        result_timestamp_utc=datetime(2026, 2, 3, 4, 6, tzinfo=UTC),
        source_account="123456789012",
        source_region="us-east-1",
        model_id="amazon.nova-pro-v1:0",
        analysis_run_id="run-1",
        configuration_version="different-version",
        analysis_logic_version="1.0",
        label_metadata=(configuration.label_metadata[0],),
    )

    paths = {issue.path for issue in CanonicalSchemaOracle.validate_result(result, configuration)}
    assert "configuration_version" in paths
    assert "label_metadata" in paths


def test_default_output_models_exclude_raw_prompt_and_response_content():
    configuration = make_configuration()
    result = InsightsResult(
        source_request_id="request-1",
        source_timestamp_utc=datetime(2026, 2, 3, 4, 5, tzinfo=UTC),
        result_timestamp_utc=datetime(2026, 2, 3, 4, 6, tzinfo=UTC),
        source_account="123456789012",
        source_region="us-east-1",
        model_id="amazon.nova-pro-v1:0",
        analysis_run_id="run-1",
        configuration_version=configuration.configuration_version,
        analysis_logic_version="1.0",
    )
    aggregate = AggregateInsightResult(
        group_type="account",
        group_key="123456789012",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 1),
        analyzed_record_count=1,
        classified_record_count=0,
        unknown_result_count=1,
        not_assessed_count=1,
        unknown_result_proportion=1.0,
        input_token_total=10,
        output_token_total=20,
        category_distribution={},
        confidence_coverage={},
        prompt_quality_coverage={},
    )

    for output in (result, aggregate):
        serialized = str(output.to_dict())
        assert "prompt_preview" not in serialized
        assert "response_preview" not in serialized
