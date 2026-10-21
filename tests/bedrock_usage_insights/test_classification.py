from datetime import date, datetime, timezone

import pytest
from hypothesis import given, settings, strategies as st

from bedrock_usage_insights.classification import USE_LIMITATION, classify
from bedrock_usage_insights.models import (
    AnalysisScope,
    Applicability,
    CategoryDefinition,
    ConfidenceBand,
    InsightConfiguration,
    Signal,
    SignalAvailability,
    SignalDefinition,
    SignalProvenance,
    SourceFieldClass,
    UsageCategory,
    UnknownReason,
    default_category_catalog,
)
from bedrock_usage_insights.oracle import CanonicalSchemaOracle

UTC = timezone.utc


def _category(category_id: str, *signals: str, threshold: float = 0.5) -> CategoryDefinition:
    return CategoryDefinition(
        category_id=category_id,
        display_name=category_id.replace("_", " ").title(),
        description=f"A likely pattern consistent with {category_id} signals.",
        signals=signals,
        evidence_threshold=threshold,
    )


def _configuration(*categories, precedence_rules=(), confidence_thresholds=None):
    signal_names = sorted({signal for category in categories for signal in category.signals})
    return InsightConfiguration(
        configuration_version="classification-test-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="classification-tests",
        change_reason="classifier examples",
        analysis_scope=AnalysisScope(
            accounts=("123456789012",),
            regions=("us-east-1",),
            models=("model-1",),
            operations=("InvokeModel",),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        ),
        categories=tuple(categories),
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
        precedence_rules=precedence_rules,
        enabled=True,
        confidence_thresholds=confidence_thresholds or {"low": 0.0, "medium": 0.5, "high": 0.8},
    )


def _signal(name: str, value=True, availability=SignalAvailability.AVAILABLE) -> Signal:
    return Signal(
        name=name,
        value_type="integer" if name.endswith("token_count") else "string",
        value=value,
        source_field_class=(
            SourceFieldClass.TOKEN_USAGE
            if name.endswith("token_count")
            else SourceFieldClass.DIRECT_METADATA
        ),
        provenance=SignalProvenance.DIRECT_OBSERVATION,
        availability=availability,
        summary=f"{name} was observed.",
    )


def test_no_qualifying_category_returns_insufficient_evidence_without_labels():
    configuration = _configuration(_category("candidate", "operation", "model_id", threshold=1.0))

    result = classify((_signal("operation"),), configuration)

    assert result.primary_category is None
    assert result.secondary_categories == ()
    assert result.unknown_reason is UnknownReason.INSUFFICIENT_EVIDENCE
    assert result.applicability.primary_category is Applicability.NOT_APPLICABLE
    assert result.applicability.unknown_reason is Applicability.APPLICABLE
    assert USE_LIMITATION in result.limitations
    assert not CanonicalSchemaOracle.validate_classification(result, configuration)


def test_one_primary_and_multiple_secondary_categories_are_distinct():
    configuration = _configuration(
        _category("primary", "operation"),
        _category("secondary_a", "input_token_count"),
        _category("secondary_b", "output_token_count"),
    )

    result = classify(
        (
            _signal("operation"),
            _signal("input_token_count", 10),
            _signal("output_token_count", 20),
        ),
        configuration,
    )

    assert result.primary_category == "primary"
    assert result.secondary_categories == ("secondary_a", "secondary_b")
    assert len(result.secondary_categories) == len(set(result.secondary_categories))
    assert result.unknown_reason is None
    assert result.confidence_band is ConfidenceBand.HIGH
    assert not CanonicalSchemaOracle.validate_classification(result, configuration)


def test_duplicate_candidates_are_deduplicated_before_result_serialization():
    configuration = _configuration(
        _category("primary", "operation"),
        _category("secondary", "model_id"),
        _category("secondary", "model_id"),
    )

    result = classify((_signal("operation"), _signal("model_id")), configuration)

    assert result.primary_category == "primary"
    assert result.secondary_categories == ("secondary",)
    assert len(result.secondary_categories) == len(set(result.secondary_categories))


def test_conflicting_fixed_catalog_candidates_return_conflicting_evidence():
    configuration = _configuration(
        _category(UsageCategory.CODE_DEVELOPMENT.value, "operation"),
        _category(UsageCategory.PERSONAL_WORK.value, "operation"),
    )

    result = classify((_signal("operation"),), configuration)

    assert result.primary_category is None
    assert result.secondary_categories == ()
    assert result.unknown_reason is UnknownReason.CONFLICTING_EVIDENCE
    assert "code_development" in " ".join(result.limitations)
    assert "personal_work" in " ".join(result.limitations)
    assert result.applicability.unknown_reason is Applicability.APPLICABLE
    assert not CanonicalSchemaOracle.validate_classification(result, configuration)


def test_named_precedence_resolves_conflicting_candidates_deterministically():
    configuration = _configuration(
        _category(UsageCategory.CODE_DEVELOPMENT.value, "operation"),
        _category(UsageCategory.PERSONAL_WORK.value, "operation"),
        precedence_rules=("prefer:code_development",),
    )

    first = classify((_signal("operation"),), configuration)
    second = classify((_signal("operation"),), configuration)

    assert first == second
    assert first.primary_category == UsageCategory.CODE_DEVELOPMENT.value
    assert first.secondary_categories == (UsageCategory.PERSONAL_WORK.value,)
    assert first.unknown_reason is None


_CONFLICTING_CATEGORY_IDS = (
    UsageCategory.CODE_DEVELOPMENT.value,
    UsageCategory.PERSONAL_WORK.value,
    UsageCategory.POTENTIALLY_WASTEFUL_WORK.value,
)


@st.composite
def conflict_classification_cases(draw: st.DrawFn):
    candidate_ids = draw(
        st.lists(
            st.sampled_from(_CONFLICTING_CATEGORY_IDS),
            unique=True,
            min_size=2,
            max_size=len(_CONFLICTING_CATEGORY_IDS),
        )
    )
    ordered_ids = draw(st.permutations(candidate_ids))
    winner = draw(st.sampled_from(candidate_ids))
    rule_kind = draw(st.sampled_from(("none", "inactive", "active")))

    if rule_kind == "active":
        precedence_rules = ("prefer:unconfigured-category", f"prefer:{winner}")
    elif rule_kind == "inactive":
        precedence_rules = ("prefer:unconfigured-category",)
    else:
        precedence_rules = ()

    configuration = _configuration(
        *(_category(category_id, "operation") for category_id in ordered_ids),
        precedence_rules=precedence_rules,
    )
    return configuration, tuple(candidate_ids), winner, rule_kind


# Feature: bedrock-usage-insights, Property 6: Conflict policy is conservative and deterministic
# **Validates: Requirements 3.5**
@given(conflict_classification_cases())
@settings(max_examples=100, deadline=None)
def test_property_6_conflicts_are_conservative_unless_exact_precedence_applies(case):
    configuration, candidate_ids, winner, rule_kind = case
    signals = (_signal("operation"),)

    first = classify(signals, configuration)
    second = classify(signals, configuration)

    assert first == second
    if rule_kind == "active":
        assert first.primary_category == winner
        assert set(first.secondary_categories) == set(candidate_ids) - {winner}
        assert len(first.secondary_categories) == len(candidate_ids) - 1
        assert first.unknown_reason is None
    else:
        assert first.primary_category is None
        assert first.secondary_categories == ()
        assert first.unknown_reason is UnknownReason.CONFLICTING_EVIDENCE
        assert all(category_id in " ".join(first.limitations) for category_id in candidate_ids)


@pytest.mark.parametrize(
    ("signals", "expected_band"),
    (
        (("operation",), ConfidenceBand.LOW),
        (("operation", "model_id"), ConfidenceBand.MEDIUM),
        (("operation", "model_id", "input_token_count"), ConfidenceBand.HIGH),
    ),
)
def test_confidence_bands_follow_configured_score_thresholds(signals, expected_band):
    configuration = _configuration(
        _category("candidate", "operation", "model_id", "input_token_count", threshold=0.3),
    )

    result = classify(tuple(_signal(name, 10 if name.endswith("token_count") else name) for name in signals), configuration)

    assert result.primary_category == "candidate"
    assert result.confidence_band is expected_band
    assert result.applicability.confidence_band is Applicability.APPLICABLE


def test_result_records_category_and_confidence_threshold_provenance():
    configuration = _configuration(_category("candidate", "operation", "model_id"))

    result = classify((_signal("operation"), _signal("model_id")), configuration)

    assert result.evidence_thresholds["candidate"] == 0.5
    assert result.evidence_thresholds["confidence_low"] == 0.0
    assert result.evidence_thresholds["confidence_medium"] == 0.5
    assert result.evidence_thresholds["confidence_high"] == 0.8
    assert result.applicability.scores is Applicability.APPLICABLE
    assert result.applicability.evidence_reference is Applicability.APPLICABLE
    assert not CanonicalSchemaOracle.validate_classification(result, configuration)


def test_default_catalog_and_unknown_reason_examples_are_fixed_contracts():
    catalog = default_category_catalog()
    configuration = _configuration(*catalog)

    result = classify((), configuration)

    assert {category.category_id for category in catalog} == {
        "code_development",
        "personal_work",
        "potentially_wasteful_work",
        "other",
        "insufficient_evidence",
    }
    assert result.unknown_reason in {
        UnknownReason.INSUFFICIENT_EVIDENCE,
        UnknownReason.CONFLICTING_EVIDENCE,
        UnknownReason.CONTENT_UNAVAILABLE,
        UnknownReason.CONTENT_REDACTED,
        UnknownReason.PARSE_ERROR,
        UnknownReason.OUT_OF_SCOPE,
        UnknownReason.ANALYSIS_ERROR,
    }


def test_use_limitation_wording_does_not_make_intent_or_performance_claims():
    configuration = _configuration(_category("candidate", "operation"))

    result = classify((_signal("operation"),), configuration)
    wording = " ".join(result.limitations)

    assert USE_LIMITATION in wording
    assert "not confirmed intent" in wording
    assert "not employee performance" not in wording
