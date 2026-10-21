"""Property tests for confidence-band threshold provenance."""

from datetime import date, datetime, timezone

from hypothesis import given, settings, strategies as st

from bedrock_usage_insights.models import (
    Applicability,
    ApplicabilityMarkers,
    AnalysisScope,
    CategoryDefinition,
    ClassificationResult,
    ConfidenceBand,
    EvidenceReference,
    InsightConfiguration,
    InsightsResult,
    UnknownReason,
)
from bedrock_usage_insights.oracle import CanonicalSchemaOracle, ConfigurationSchemaOracle

UTC = timezone.utc
_CONFIDENCE_BANDS = frozenset(ConfidenceBand)


def _confidence_band(score: float, thresholds: dict[str, float]) -> ConfidenceBand:
    if score >= thresholds[ConfidenceBand.HIGH.value]:
        return ConfidenceBand.HIGH
    if score >= thresholds[ConfidenceBand.MEDIUM.value]:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


@st.composite
def confidence_band_cases(draw: st.DrawFn):
    threshold_points = sorted(
        draw(st.lists(st.integers(min_value=0, max_value=1000), min_size=3, max_size=3))
    )
    low, medium, high = (point / 1000 for point in threshold_points)
    score = draw(st.integers(min_value=0, max_value=1000)) / 1000
    evidence_threshold = draw(st.integers(min_value=0, max_value=1000)) / 1000
    interpretation = draw(
        st.sampled_from(
            (
                "Evidence score must meet or exceed the configured threshold.",
                "Configured evidence threshold determines category eligibility.",
            )
        )
    )
    unknown = draw(st.booleans())

    category = CategoryDefinition(
        category_id="generated-category",
        display_name="Generated category",
        description="A category used to validate threshold provenance.",
        signals=("operation",),
        evidence_threshold=evidence_threshold,
        threshold_interpretation=interpretation,
    )
    configuration = InsightConfiguration(
        configuration_version="property-10-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="property-test",
        change_reason="confidence-band threshold provenance",
        analysis_scope=AnalysisScope(
            accounts=("123456789012",),
            regions=("us-east-1",),
            models=("model",),
            operations=("InvokeModel",),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        ),
        categories=(category,),
        enabled=True,
        confidence_thresholds={
            ConfidenceBand.LOW.value: low,
            ConfidenceBand.MEDIUM.value: medium,
            ConfidenceBand.HIGH.value: high,
        },
    )

    if unknown:
        classification = ClassificationResult(
            unknown_reason=UnknownReason.INSUFFICIENT_EVIDENCE,
            applicability=ApplicabilityMarkers(
                unknown_reason=Applicability.APPLICABLE,
            ),
        )
    else:
        classification = ClassificationResult(
            primary_category=category.category_id,
            category_scores={category.category_id: score},
            confidence_band=_confidence_band(score, configuration.confidence_thresholds),
            evidence_thresholds={category.category_id: category.evidence_threshold},
            evidence_reference=EvidenceReference(signal_names=("operation",)),
            applicability=ApplicabilityMarkers(
                primary_category=Applicability.APPLICABLE,
                scores=Applicability.APPLICABLE,
                confidence_band=Applicability.APPLICABLE,
                evidence_reference=Applicability.APPLICABLE,
            ),
        )

    result = InsightsResult(
        source_request_id="request-1",
        source_timestamp_utc=datetime(2026, 1, 2, tzinfo=UTC),
        result_timestamp_utc=datetime(2026, 1, 2, 0, 1, tzinfo=UTC),
        source_account="123456789012",
        source_region="us-east-1",
        model_id="model",
        analysis_run_id="run-1",
        configuration_version=configuration.configuration_version,
        analysis_logic_version="1.0",
        classification=classification,
        label_metadata=configuration.label_metadata,
    )
    return configuration, result, score, unknown


# Feature: bedrock-usage-insights, Property 10: Confidence-band threshold provenance
# **Validates: Requirements 2.1, 4.3, 10.1, 12.2**
@given(confidence_band_cases())
@settings(max_examples=100, deadline=None)
def test_property_10_confidence_band_has_bounded_threshold_provenance(case):
    configuration, result, score, unknown = case

    assert not ConfigurationSchemaOracle.validate(configuration)
    assert all(0.0 <= value <= 1.0 for value in configuration.confidence_thresholds.values())
    assert all(
        0.0 <= category.evidence_threshold <= 1.0
        and category.score_min >= 0.0
        and category.score_max <= 1.0
        for category in configuration.categories
    )

    classification = result.classification
    if unknown:
        assert classification.unknown_reason is not None
        assert classification.confidence_band is None
        assert not classification.category_scores
        assert not classification.evidence_thresholds
    else:
        assert classification.unknown_reason is None
        assert classification.primary_category == "generated-category"
        assert 0.0 <= score <= 1.0
        assert len(_CONFIDENCE_BANDS & {classification.confidence_band}) == 1
        assert classification.confidence_band in _CONFIDENCE_BANDS
        assert all(0.0 <= value <= 1.0 for value in classification.category_scores.values())
        assert all(0.0 <= value <= 1.0 for value in classification.evidence_thresholds.values())

        category = configuration.categories[0]
        assert classification.evidence_thresholds[category.category_id] == category.evidence_threshold
        label = result.label_metadata[0]
        assert label.label_id == category.category_id
        assert label.threshold_interpretation == category.threshold_interpretation
        assert label.configuration_version == result.configuration_version
        assert classification.confidence_band == _confidence_band(
            score, configuration.confidence_thresholds
        )

    assert not CanonicalSchemaOracle.validate_result(result, configuration)
