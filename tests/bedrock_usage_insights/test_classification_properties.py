"""Property tests for deterministic classification cardinality and fallback."""

from datetime import datetime, timezone

from hypothesis import given, settings, strategies as st

from bedrock_usage_insights.classification import classify
from bedrock_usage_insights.models import (
    AnalysisScope,
    CategoryDefinition,
    InsightConfiguration,
    Signal,
    SignalAvailability,
    SignalDefinition,
    SignalProvenance,
    SourceFieldClass,
    UnknownReason,
)

UTC = timezone.utc
_SIGNAL_NAMES = ("operation", "model_id", "input_token_count", "output_token_count")


@st.composite
def classification_cases(draw: st.DrawFn):
    category_count = draw(st.integers(min_value=1, max_value=5))
    category_signals = [
        tuple(
            sorted(
                draw(
                    st.sets(
                        st.sampled_from(_SIGNAL_NAMES),
                        min_size=1,
                        max_size=len(_SIGNAL_NAMES),
                    )
                )
            )
        )
        for _ in range(category_count)
    ]
    thresholds = [
        draw(st.integers(min_value=1, max_value=1000)) / 1000
        for _ in range(category_count)
    ]
    enabled = [draw(st.booleans()) for _ in range(category_count)]
    categories = tuple(
        CategoryDefinition(
            category_id=f"generated-category-{index}",
            display_name=f"Generated category {index}",
            description="A generated signal-consistent usage pattern.",
            signals=signals,
            evidence_threshold=threshold,
            enabled=is_enabled,
        )
        for index, (signals, threshold, is_enabled) in enumerate(
            zip(category_signals, thresholds, enabled)
        )
    )

    fallback = draw(st.booleans())
    available_names = (
        set()
        if fallback
        else draw(st.sets(st.sampled_from(_SIGNAL_NAMES), max_size=len(_SIGNAL_NAMES)))
    )
    signals = tuple(
        Signal(
            name=name,
            value_type="integer" if name.endswith("token_count") else "string",
            value=True if name in available_names else None,
            source_field_class=(
                SourceFieldClass.TOKEN_USAGE
                if name.endswith("token_count")
                else SourceFieldClass.DIRECT_METADATA
            ),
            provenance=SignalProvenance.DIRECT_OBSERVATION,
            availability=(
                SignalAvailability.AVAILABLE
                if name in available_names
                else SignalAvailability.UNAVAILABLE
            ),
        )
        for name in _SIGNAL_NAMES
    )
    configuration = InsightConfiguration(
        configuration_version="property-5-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="property-test",
        change_reason="classification cardinality and evidence fallback",
        analysis_scope=AnalysisScope(),
        categories=categories,
        signal_definitions=tuple(
            SignalDefinition(
                name=name,
                source_field_class=(
                    SourceFieldClass.TOKEN_USAGE
                    if name.endswith("token_count")
                    else SourceFieldClass.DIRECT_METADATA
                ),
                provenance=SignalProvenance.DIRECT_OBSERVATION,
            )
            for name in _SIGNAL_NAMES
        ),
        enabled=True,
        confidence_thresholds={"low": 0.0, "medium": 0.5, "high": 0.8},
    )
    return categories, signals, configuration


def _qualifying_categories(categories, signals):
    available = {
        signal.name
        for signal in signals
        if signal.availability is SignalAvailability.AVAILABLE and signal.value is not None
    }
    return tuple(
        category
        for category in categories
        if category.enabled
        and category.signals
        and len(available.intersection(category.signals)) / len(category.signals)
        >= category.evidence_threshold
    )


# Feature: bedrock-usage-insights, Property 5: Classification cardinality and evidence fallback
# **Validates: Requirements 2.3, 2.4**
@given(classification_cases())
@settings(max_examples=100, deadline=None)
def test_property_5_classification_cardinality_and_evidence_fallback(case):
    categories, signals, configuration = case
    result = classify(signals, configuration)
    qualifying = _qualifying_categories(categories, signals)

    assert result.primary_category is None or isinstance(result.primary_category, str)
    assert len(result.secondary_categories) == len(set(result.secondary_categories))
    assert result.primary_category not in result.secondary_categories

    if not qualifying:
        assert result.primary_category is None
        assert result.secondary_categories == ()
        assert result.unknown_reason is UnknownReason.INSUFFICIENT_EVIDENCE
        return

    assert result.primary_category == qualifying[0].category_id
    assert result.secondary_categories == tuple(
        category.category_id
        for category in qualifying[1:]
    )
    assert all(
        category_id in {candidate.category_id for candidate in qualifying}
        for category_id in result.secondary_categories
    )
    assert result.unknown_reason is None


@st.composite
def threshold_monotonicity_cases(st_draw: st.DrawFn):
    signal_names = tuple(
        st_draw(
            st.lists(
                st.sampled_from(_SIGNAL_NAMES),
                unique=True,
                min_size=1,
                max_size=len(_SIGNAL_NAMES),
            )
        )
    )
    lower_points = st_draw(st.integers(min_value=0, max_value=1000))
    higher_points = st_draw(
        st.integers(min_value=lower_points, max_value=1000)
    )
    evidence_sets = st_draw(
        st.lists(
            st.sets(
                st.sampled_from(signal_names),
                max_size=len(signal_names),
            ),
            min_size=1,
            max_size=12,
        )
    )
    return signal_names, evidence_sets, lower_points / 1000, higher_points / 1000


def _threshold_configuration(signal_names, threshold):
    categories = (
        CategoryDefinition(
            category_id="threshold-category",
            display_name="Threshold category",
            description="A generated signal-consistent usage pattern.",
            signals=signal_names,
            evidence_threshold=threshold,
        ),
    )
    return InsightConfiguration(
        configuration_version="property-17-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="property-test",
        change_reason="threshold monotonicity",
        analysis_scope=AnalysisScope(),
        categories=categories,
        signal_definitions=tuple(
            SignalDefinition(
                name=name,
                source_field_class=(
                    SourceFieldClass.TOKEN_USAGE
                    if name.endswith("token_count")
                    else SourceFieldClass.DIRECT_METADATA
                ),
                provenance=SignalProvenance.DIRECT_OBSERVATION,
            )
            for name in signal_names
        ),
        enabled=True,
        confidence_thresholds={"low": 0.0, "medium": 0.5, "high": 0.8},
    )


def _category_matches(result, category_id):
    return result.primary_category == category_id or category_id in result.secondary_categories


# Feature: bedrock-usage-insights, Property 17: Threshold monotonicity
# **Validates: Requirements 2.3, 2.4, 12.4**
@given(threshold_monotonicity_cases())
@settings(max_examples=100, deadline=None)
def test_property_17_threshold_monotonicity(case):
    signal_names, evidence_sets, lower_threshold, higher_threshold = case
    lower_configuration = _threshold_configuration(signal_names, lower_threshold)
    higher_configuration = _threshold_configuration(signal_names, higher_threshold)
    lower_matches = set()
    higher_matches = set()

    for record_index, available_names in enumerate(evidence_sets):
        signals = tuple(
            Signal(
                name=name,
                value_type="integer" if name.endswith("token_count") else "string",
                value=10 if name.endswith("token_count") else name,
                source_field_class=(
                    SourceFieldClass.TOKEN_USAGE
                    if name.endswith("token_count")
                    else SourceFieldClass.DIRECT_METADATA
                ),
                provenance=SignalProvenance.DIRECT_OBSERVATION,
                availability=SignalAvailability.AVAILABLE,
            )
            for name in sorted(available_names)
        )
        lower_result = classify(signals, lower_configuration)
        higher_result = classify(signals, higher_configuration)

        if _category_matches(lower_result, "threshold-category"):
            lower_matches.add(record_index)
        if _category_matches(higher_result, "threshold-category"):
            higher_matches.add(record_index)

    assert higher_matches <= lower_matches
