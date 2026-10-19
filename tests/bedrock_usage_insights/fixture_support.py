"""Pure synthetic fixtures and Hypothesis strategies for usage-insights tests.

This module deliberately lives with the tests so production feature imports do
not acquire a Hypothesis dependency.  Builders return canonical model values;
strategies compose those builders without making AWS calls or using global
state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Sequence

from hypothesis import strategies as st

from bedrock_usage_insights.models import (
    Applicability,
    ApplicabilityMarkers,
    AnalysisRun,
    AnalysisScope,
    AggregateInsightResult,
    CategoryDefinition,
    ClassificationResult,
    ConfidenceBand,
    ContentAnalysisPolicy,
    ContentAnalysisState,
    DashboardPublicationPolicy,
    ErrorRecord,
    ErrorStage,
    EvidenceReference,
    ExposureLevel,
    IdentityMetadata,
    InsightConfiguration,
    InsightsResult,
    InvocationRecord,
    ModelAssistancePolicy,
    PrivacyPolicy,
    PromptAssessmentStatus,
    PromptDimension,
    PromptDimensionConfiguration,
    PromptQualityAssessment,
    PromptQualityDimensionResult,
    RedactionStatus,
    ResultStatus,
    Signal,
    SignalAvailability,
    SignalProvenance,
    SourceFieldClass,
    SUPPORTED_SIGNALS,
    SensitiveContent,
    UnknownReason,
    UsageCategory,
    default_category_catalog,
    default_signal_definitions,
)

UTC = timezone.utc


class ContentAvailability(str, Enum):
    """Synthetic content states used by privacy and prompt tests."""

    ABSENT = "absent"
    AVAILABLE = "available"
    REDACTED = "redacted"
    MALFORMED = "malformed"
    EXPIRED = "expired"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class RedactionFailure:
    """Content-free representation of a failed redaction operation."""

    code: str
    detail: str


@dataclass(frozen=True)
class RunOutcome:
    """Inputs and run record used by run-accounting tests."""

    run: AnalysisRun
    results: tuple[InsightsResult, ...]
    errors: tuple[ErrorRecord, ...]


def make_scope(
    *,
    start_date: date = date(2026, 1, 1),
    end_date: date = date(2026, 6, 30),
    accounts: tuple[str, ...] = ("123456789012",),
    regions: tuple[str, ...] = ("us-east-1",),
    models: tuple[str, ...] = ("amazon.nova-pro-v1:0",),
    operations: tuple[str, ...] = ("InvokeModel",),
) -> AnalysisScope:
    """Build a finite, approved source scope."""

    return AnalysisScope(
        accounts=accounts,
        regions=regions,
        models=models,
        operations=operations,
        start_date=start_date,
        end_date=end_date,
    )


def make_configuration(**changes: Any) -> InsightConfiguration:
    """Build a valid configuration with the complete initial catalog."""

    values: dict[str, Any] = {
        "configuration_version": "config-2026-01-01-v1",
        "effective_at": datetime(2026, 1, 1, tzinfo=UTC),
        "author_or_service": "fixture-service",
        "change_reason": "synthetic fixture",
        "analysis_scope": make_scope(),
        "categories": default_category_catalog(),
        "signal_definitions": default_signal_definitions(),
        "content_analysis": ContentAnalysisPolicy(),
        "privacy": PrivacyPolicy(),
        "dashboard_publication": DashboardPublicationPolicy(),
        "model_assistance": ModelAssistancePolicy(),
    }
    values.update(changes)
    return InsightConfiguration(**values)


def make_invocation_record(**changes: Any) -> InvocationRecord:
    """Build a valid normalized invocation record."""

    values: dict[str, Any] = {
        "source_request_id": "request-1",
        "source_timestamp_utc": datetime(2026, 2, 3, 4, 5, tzinfo=UTC),
        "source_account": "123456789012",
        "source_region": "us-east-1",
        "model_id": "amazon.nova-pro-v1:0",
        "operation": "InvokeModel",
        "analysis_run_id": "run-1",
        "configuration_version": "config-2026-01-01-v1",
        "analysis_logic_version": "1.0",
        "input_token_count": 10,
        "output_token_count": 20,
    }
    values.update(changes)
    return InvocationRecord(**values)


def make_signal(**changes: Any) -> Signal:
    """Build a valid signal with explicit provenance and source-field class."""

    values: dict[str, Any] = {
        "name": "operation",
        "value_type": "string",
        "value": "InvokeModel",
        "source_field_class": SourceFieldClass.DIRECT_METADATA,
        "provenance": SignalProvenance.DIRECT_OBSERVATION,
    }
    values.update(changes)
    return Signal(**values)


def make_classification(
    *,
    configuration: InsightConfiguration | None = None,
    unknown: bool = False,
    **changes: Any,
) -> ClassificationResult:
    """Build a schema-valid known or unknown classification result."""

    configuration = configuration or make_configuration()
    category_id = next(
        category.category_id
        for category in configuration.categories
        if category.category_id != UsageCategory.INSUFFICIENT_EVIDENCE.value
    )
    if unknown:
        values: dict[str, Any] = {
            "unknown_reason": UnknownReason.INSUFFICIENT_EVIDENCE,
            "applicability": ApplicabilityMarkers(
                unknown_reason=Applicability.APPLICABLE,
            ),
        }
    else:
        category = next(
            category for category in configuration.categories if category.category_id == category_id
        )
        values = {
            "primary_category": category_id,
            "category_scores": {category_id: 0.8},
            "confidence_band": ConfidenceBand.MEDIUM,
            "evidence_thresholds": {category_id: category.evidence_threshold},
            "evidence_reference": EvidenceReference(
                signal_names=category.signals[:1],
                source_field_classes=(SourceFieldClass.DIRECT_METADATA,),
                summaries=("direct metadata signal",),
                counts={category.signals[0]: 1},
            ),
            "applicability": ApplicabilityMarkers(
                primary_category=Applicability.APPLICABLE,
                scores=Applicability.APPLICABLE,
                confidence_band=Applicability.APPLICABLE,
                evidence_reference=Applicability.APPLICABLE,
            ),
        }
    values.update(changes)
    return ClassificationResult(**values)


def make_prompt_quality(
    *,
    content_state: ContentAnalysisState = ContentAnalysisState.DISABLED,
    assessed: bool = False,
    **changes: Any,
) -> PromptQualityAssessment:
    """Build prompt-quality dimensions in an applicable state."""

    dimensions: dict[str, PromptQualityDimensionResult] = {}
    for dimension in PromptDimension:
        if assessed:
            dimensions[dimension.value] = PromptQualityDimensionResult(
                dimension=dimension.value,
                status=PromptAssessmentStatus.ASSESSED,
                score=0.75,
                confidence_band=ConfidenceBand.MEDIUM,
                evidence_reference=EvidenceReference(
                    signal_names=(f"prompt_{dimension.value}_marker",),
                    source_field_classes=(SourceFieldClass.PERMITTED_CONTENT_DERIVED_FEATURE,),
                    summaries=("bounded content-derived feature",),
                    counts={dimension.value: 1},
                ),
                threshold_interpretation="Score meets the configured threshold.",
            )
        else:
            dimensions[dimension.value] = PromptQualityDimensionResult(
                dimension=dimension.value,
                status=PromptAssessmentStatus.NOT_ASSESSED,
                not_assessed_reason="content_disabled",
            )
    values: dict[str, Any] = {
        "dimensions": dimensions,
        "content_state": content_state,
    }
    values.update(changes)
    return PromptQualityAssessment(**values)


def make_insights_result(
    *,
    configuration: InsightConfiguration | None = None,
    unknown: bool = False,
    **changes: Any,
) -> InsightsResult:
    """Build a result whose labels and versions match its configuration."""

    configuration = configuration or make_configuration()
    values: dict[str, Any] = {
        "source_request_id": "request-1",
        "source_timestamp_utc": datetime(2026, 2, 3, 4, 5, tzinfo=UTC),
        "result_timestamp_utc": datetime(2026, 2, 3, 4, 6, tzinfo=UTC),
        "source_account": "123456789012",
        "source_region": "us-east-1",
        "model_id": "amazon.nova-pro-v1:0",
        "analysis_run_id": "run-1",
        "configuration_version": configuration.configuration_version,
        "analysis_logic_version": configuration.analysis_logic_version,
        "classification": make_classification(configuration=configuration, unknown=unknown),
        "prompt_quality": make_prompt_quality(),
        "input_token_count": 10,
        "output_token_count": 20,
        "label_metadata": configuration.label_metadata,
    }
    values.update(changes)
    return InsightsResult(**values)


def make_aggregate_group(**changes: Any) -> AggregateInsightResult:
    """Build a valid, unsuppressed aggregate group."""

    values: dict[str, Any] = {
        "group_type": "account",
        "group_key": "account:123456789012",
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 1, 31),
        "analyzed_record_count": 10,
        "classified_record_count": 8,
        "unknown_result_count": 2,
        "not_assessed_count": 10,
        "unknown_result_proportion": 0.2,
        "input_token_total": 100,
        "output_token_total": 200,
        "category_distribution": {"code_development": 8},
        "confidence_coverage": {"medium": 8},
        "prompt_quality_coverage": {"not_assessed": 10},
        "minimum_group_size": 10,
        "distinct_qualifying_subject_count": 10,
    }
    values.update(changes)
    return AggregateInsightResult(**values)


def make_error_record(**changes: Any) -> ErrorRecord:
    """Build a bounded, content-free processing error."""

    values: dict[str, Any] = {
        "error_code": "PARSE_ERROR",
        "stage": ErrorStage.NORMALIZATION,
        "analysis_run_id": "run-1",
        "detail": "synthetic malformed source field",
        "retryable": False,
        "occurred_at": datetime(2026, 2, 3, 4, 7, tzinfo=UTC),
    }
    values.update(changes)
    return ErrorRecord(**values)


def make_analysis_run(
    *,
    configuration: InsightConfiguration | None = None,
    status: ResultStatus = ResultStatus.RUNNING,
    **changes: Any,
) -> AnalysisRun:
    """Build a run record with valid traceability and scope metadata."""

    configuration = configuration or make_configuration()
    started_at = datetime(2026, 2, 3, 4, 0, tzinfo=UTC)
    values: dict[str, Any] = {
        "analysis_run_id": "run-1",
        "status": status,
        "source_scope": configuration.analysis_scope,
        "configuration_version": configuration.configuration_version,
        "analysis_logic_version": configuration.analysis_logic_version,
        "started_at": started_at,
        "ended_at": None,
        "requested_scope": configuration.analysis_scope,
    }
    if status in {ResultStatus.COMPLETED, ResultStatus.PARTIAL, ResultStatus.FAILED, ResultStatus.CANCELLED}:
        values["ended_at"] = started_at + timedelta(minutes=1)
    values.update(changes)
    return AnalysisRun(**values)


def make_run_outcome(
    *,
    configuration: InsightConfiguration | None = None,
    results: Sequence[InsightsResult] = (),
    errors: Sequence[ErrorRecord] = (),
    status: ResultStatus | None = None,
) -> RunOutcome:
    """Build a run outcome and derive its default status from eligible work."""

    configuration = configuration or make_configuration()
    result_tuple = tuple(results)
    error_tuple = tuple(errors)
    if status is None:
        status = (
            ResultStatus.PARTIAL
            if result_tuple and error_tuple
            else ResultStatus.COMPLETED
            if result_tuple
            else ResultStatus.FAILED
        )
    run = make_analysis_run(configuration=configuration, status=status)
    return RunOutcome(run=run, results=result_tuple, errors=error_tuple)


def unicode_text(*, max_size: int = 64) -> st.SearchStrategy[str]:
    """Generate non-empty Unicode text, excluding surrogate code points."""

    return st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=max_size,
    ).filter(lambda value: bool(value.strip()))


def long_text(*, min_size: int = 501, max_size: int = 700) -> st.SearchStrategy[str]:
    """Generate text longer than the largest documented category description."""

    return st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=min_size,
        max_size=max_size,
    )


def malformed_value_strategy() -> st.SearchStrategy[Any]:
    """Generate representative values rejected by typed source parsers."""

    return st.one_of(
        st.just("not-a-count"),
        st.integers(max_value=-1),
        st.booleans(),
        st.floats(allow_nan=True, allow_infinity=True),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(st.text(max_size=8), st.integers(), max_size=3),
    )


def redaction_failure_strategy() -> st.SearchStrategy[RedactionFailure]:
    """Generate bounded, content-free redaction failures."""

    return st.builds(
        RedactionFailure,
        code=st.sampled_from(("REDACTION_FAILED", "EXCLUSION_FAILED", "CONTENT_POLICY_BLOCKED")),
        detail=st.sampled_from(
            (
                "configured redaction rule failed",
                "content exclusion could not be applied",
                "approved content boundary was not satisfied",
            )
        ),
    )


def content_availability_strategy() -> st.SearchStrategy[ContentAvailability]:
    """Generate all content states relevant to privacy and assessment tests."""

    return st.sampled_from(ContentAvailability)


@st.composite
def content_case_strategy(draw: Any) -> tuple[ContentAvailability, SensitiveContent | None]:
    """Generate a content state paired with the permitted synthetic payload."""

    state = draw(content_availability_strategy())
    if state is ContentAvailability.AVAILABLE:
        return state, SensitiveContent(
            prompt_preview=draw(unicode_text(max_size=120)),
            response_preview=draw(unicode_text(max_size=120)),
        )
    if state is ContentAvailability.REDACTED:
        return state, SensitiveContent(
            prompt_preview=None,
            response_preview=None,
            redaction_status=RedactionStatus.REDACTED,
        )
    return state, None


def _unit_float_strategy() -> st.SearchStrategy[float]:
    return st.integers(min_value=0, max_value=100).map(lambda value: value / 100)


def _non_empty_identifier(max_size: int = 32) -> st.SearchStrategy[str]:
    return unicode_text(max_size=max_size).map(lambda value: value.strip() or "fixture")


def scope_strategy() -> st.SearchStrategy[AnalysisScope]:
    """Generate finite scopes suitable for source planning."""

    @st.composite
    def build(draw: Any) -> AnalysisScope:
        start = draw(st.dates(min_value=date(2025, 1, 1), max_value=date(2026, 12, 1)))
        duration = draw(st.integers(min_value=0, max_value=180))
        end = min(start + timedelta(days=duration), date(2026, 12, 31))
        return make_scope(
            start_date=start,
            end_date=end,
            accounts=tuple(draw(st.lists(st.from_regex(r"[0-9]{12}", fullmatch=True), min_size=1, max_size=2, unique=True))),
            regions=tuple(draw(st.lists(st.sampled_from(("us-east-1", "eu-west-1", "ap-southeast-1")), min_size=1, max_size=2, unique=True))),
            models=tuple(draw(st.lists(st.sampled_from(("amazon.nova-pro-v1:0", "anthropic.claude-3-haiku-20240307-v1:0", "amazon.titan-text-express-v1")), min_size=1, max_size=2, unique=True))),
            operations=tuple(draw(st.lists(st.sampled_from(("InvokeModel", "Converse")), min_size=1, max_size=2, unique=True))),
        )

    return build()


@st.composite
def configuration_strategy(draw: Any) -> InsightConfiguration:
    """Generate schema-valid configurations with varied optional settings."""

    content_enabled = draw(st.booleans())
    if content_enabled:
        content = ContentAnalysisPolicy(
            state=ContentAnalysisState.ENABLED,
            approved_accounts=(draw(st.from_regex(r"[0-9]{12}", fullmatch=True)),),
            approved_regions=(draw(st.sampled_from(("us-east-1", "eu-west-1"))),),
            access_policy_reference=draw(_non_empty_identifier()),
            retention_days=draw(st.integers(min_value=1, max_value=3650)),
            max_preview_length=draw(st.integers(min_value=1, max_value=4096)),
        )
    else:
        content = ContentAnalysisPolicy(
            state=ContentAnalysisState.DISABLED,
            max_preview_length=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=4096))),
        )

    low = draw(_unit_float_strategy())
    medium = draw(st.integers(min_value=int(low * 100), max_value=100).map(lambda value: value / 100))
    high = draw(st.integers(min_value=int(medium * 100), max_value=100).map(lambda value: value / 100))
    categories: list[CategoryDefinition] = []
    for category in default_category_catalog():
        score_min_ticks = draw(st.integers(min_value=0, max_value=100))
        score_max_ticks = draw(st.integers(min_value=score_min_ticks, max_value=100))
        score_min = score_min_ticks / 100
        score_max = score_max_ticks / 100
        categories.append(
            CategoryDefinition(
                category_id=category.category_id,
                display_name=draw(unicode_text(max_size=40)),
                description=draw(unicode_text(max_size=120)),
                signals=category.signals,
                score_min=score_min,
                score_max=score_max,
                evidence_threshold=draw(_unit_float_strategy()),
                enabled=draw(st.booleans()),
                exposure_level=(
                    ExposureLevel.NOT_EXPOSED
                    if category.category_id == UsageCategory.INSUFFICIENT_EVIDENCE.value
                    else draw(st.sampled_from((ExposureLevel.AGGREGATE_ONLY, ExposureLevel.NOT_EXPOSED)))
                ),
                threshold_interpretation=draw(unicode_text(max_size=120)),
            )
        )

    dimensions = {
        dimension.value: PromptDimensionConfiguration(
            enabled=draw(st.booleans()),
            threshold=draw(_unit_float_strategy()),
        )
        for dimension in PromptDimension
    }
    dashboard_enabled = draw(st.booleans())
    model_enabled = draw(st.booleans())
    return make_configuration(
        configuration_version=f"config-{draw(_non_empty_identifier(24))}",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service=draw(_non_empty_identifier()),
        change_reason=draw(unicode_text(max_size=120)),
        analysis_scope=draw(scope_strategy()),
        categories=tuple(categories),
        confidence_thresholds={"low": low, "medium": medium, "high": high},
        prompt_dimensions=dimensions,
        content_analysis=content,
        privacy=PrivacyPolicy(
            minimum_group_size=draw(st.integers(min_value=5, max_value=50)),
            exclusion_rules=tuple(draw(st.lists(_non_empty_identifier(24), max_size=2, unique=True))),
        ),
        dashboard_publication=(
            DashboardPublicationPolicy(
                enabled=dashboard_enabled,
                aggregate_only=True,
                approved_role_reference=draw(_non_empty_identifier()) if dashboard_enabled else None,
            )
        ),
        model_assistance=ModelAssistancePolicy(
            enabled=model_enabled,
            endpoint_reference=draw(_non_empty_identifier()) if model_enabled else None,
            model_id=draw(_non_empty_identifier()) if model_enabled else None,
            model_version=draw(_non_empty_identifier()) if model_enabled else None,
        ),
    )


def invocation_record_strategy() -> st.SearchStrategy[InvocationRecord]:
    """Generate canonical records with nullable tokens, identity, and content."""

    return st.builds(
        make_invocation_record,
        source_request_id=_non_empty_identifier(48),
        source_timestamp_utc=st.datetimes(
            min_value=datetime(2025, 1, 1),
            max_value=datetime(2026, 12, 31, 23, 59, 59),
            timezones=st.just(UTC),
        ),
        source_account=st.from_regex(r"[0-9]{12}", fullmatch=True),
        source_region=st.sampled_from(("us-east-1", "eu-west-1", "ap-southeast-1")),
        model_id=st.sampled_from(("amazon.nova-pro-v1:0", "anthropic.claude-3-haiku-20240307-v1:0", "amazon.titan-text-express-v1")),
        operation=st.sampled_from(("InvokeModel", "Converse")),
        analysis_run_id=_non_empty_identifier(32),
        configuration_version=_non_empty_identifier(32),
        analysis_logic_version=st.sampled_from(("1.0", "1.1")),
        input_token_count=st.one_of(st.none(), st.integers(min_value=0, max_value=100_000)),
        output_token_count=st.one_of(st.none(), st.integers(min_value=0, max_value=100_000)),
        cache_read_token_count=st.one_of(st.none(), st.integers(min_value=0, max_value=100_000)),
        cache_write_token_count=st.one_of(st.none(), st.integers(min_value=0, max_value=100_000)),
        stop_reason=st.one_of(st.none(), _non_empty_identifier(24)),
        content_type=st.one_of(st.none(), st.sampled_from(("application/json", "text/plain"))),
        request_metadata=st.dictionaries(_non_empty_identifier(16), _non_empty_identifier(32), max_size=3),
        model_format=st.one_of(st.none(), st.sampled_from(("nova", "anthropic", "titan"))),
        identity_metadata=st.one_of(
            st.none(),
            st.builds(
                IdentityMetadata,
                identity_arn=st.one_of(st.none(), _non_empty_identifier(80)),
                session_name=st.one_of(st.none(), _non_empty_identifier(32)),
                name=st.one_of(st.none(), _non_empty_identifier(32)),
                account_owner=st.one_of(st.none(), _non_empty_identifier(32)),
            ),
        ),
        sensitive_content=st.one_of(
            st.none(),
            st.builds(
                SensitiveContent,
                prompt_preview=st.one_of(st.none(), unicode_text(max_size=120)),
                response_preview=st.one_of(st.none(), unicode_text(max_size=120)),
                redaction_status=st.sampled_from(RedactionStatus),
            ),
        ),
    )


def signal_strategy() -> st.SearchStrategy[Signal]:
    """Generate direct and permitted content-derived signals."""

    direct_names = tuple(sorted({name for name in SUPPORTED_SIGNALS if not name.startswith("prompt_")}))
    content_names = tuple(sorted(SUPPORTED_SIGNALS - set(direct_names)))

    @st.composite
    def build(draw: Any) -> Signal:
        is_content = draw(st.booleans())
        name = draw(st.sampled_from(content_names if is_content else direct_names))
        if name.endswith("token_count"):
            value_type, value, source_class = "integer", draw(st.integers(min_value=0, max_value=100_000)), SourceFieldClass.TOKEN_USAGE
        elif name.startswith("prompt_"):
            value_type, value, source_class = "boolean", draw(st.booleans()), SourceFieldClass.PERMITTED_CONTENT_DERIVED_FEATURE
        else:
            value_type, value, source_class = "string", draw(_non_empty_identifier(80)), SourceFieldClass.DIRECT_METADATA
        return make_signal(
            name=name,
            value_type=value_type,
            value=value,
            source_field_class=source_class,
            provenance=SignalProvenance.CONTENT_DERIVED if is_content else SignalProvenance.DIRECT_OBSERVATION,
            availability=draw(st.sampled_from(SignalAvailability)),
            summary=draw(st.one_of(st.none(), _non_empty_identifier(80))),
        )

    return build()


def classification_result_strategy(
    category_ids: Sequence[str] | None = None,
) -> st.SearchStrategy[ClassificationResult]:
    """Generate valid known or unknown classifications for the given catalog."""

    ids = tuple(category_ids or (category.value for category in UsageCategory if category is not UsageCategory.INSUFFICIENT_EVIDENCE))

    @st.composite
    def build(draw: Any) -> ClassificationResult:
        unknown = draw(st.booleans())
        if unknown:
            return make_classification(
                unknown=True,
                unknown_reason=draw(st.sampled_from(UnknownReason)),
            )
        primary = draw(st.sampled_from(ids))
        secondary = tuple(
            draw(st.lists(st.sampled_from(ids), max_size=min(2, len(ids)), unique=True))
        )
        secondary = tuple(category for category in secondary if category != primary)
        scores = {category: draw(_unit_float_strategy()) for category in (primary, *secondary)}
        thresholds = {category: draw(_unit_float_strategy()) for category in scores}
        return make_classification(
            primary_category=primary,
            secondary_categories=secondary,
            category_scores=scores,
            confidence_band=draw(st.sampled_from(ConfidenceBand)),
            evidence_thresholds=thresholds,
        )

    return build()


def prompt_quality_assessment_strategy() -> st.SearchStrategy[PromptQualityAssessment]:
    """Generate assessments with independently assessed dimensions."""

    @st.composite
    def build(draw: Any) -> PromptQualityAssessment:
        dimensions: dict[str, PromptQualityDimensionResult] = {}
        for dimension in PromptDimension:
            if draw(st.booleans()):
                dimensions[dimension.value] = PromptQualityDimensionResult(
                    dimension=dimension.value,
                    status=PromptAssessmentStatus.ASSESSED,
                    score=draw(_unit_float_strategy()),
                    confidence_band=draw(st.sampled_from(ConfidenceBand)),
                    evidence_reference=EvidenceReference(
                        signal_names=(f"prompt_{dimension.value}_marker",),
                        source_field_classes=(SourceFieldClass.PERMITTED_CONTENT_DERIVED_FEATURE,),
                        summaries=("bounded feature",),
                    ),
                    threshold_interpretation="Configured threshold interpretation.",
                )
            else:
                dimensions[dimension.value] = PromptQualityDimensionResult(
                    dimension=dimension.value,
                    status=PromptAssessmentStatus.NOT_ASSESSED,
                    not_assessed_reason=draw(_non_empty_identifier(40)),
                )
        return PromptQualityAssessment(
            dimensions=dimensions,
            content_state=draw(st.sampled_from(ContentAnalysisState)),
        )

    return build()


def insights_result_strategy(
    configuration: InsightConfiguration | None = None,
) -> st.SearchStrategy[InsightsResult]:
    """Generate result rows with exact configuration labels and versions."""

    @st.composite
    def build(draw: Any) -> InsightsResult:
        selected = configuration or draw(configuration_strategy())
        unknown = draw(st.booleans())
        result = make_insights_result(
            configuration=selected,
            unknown=unknown,
            source_request_id=draw(_non_empty_identifier(48)),
            analysis_run_id=draw(_non_empty_identifier(32)),
            classification=make_classification(configuration=selected, unknown=unknown),
            prompt_quality=draw(prompt_quality_assessment_strategy()),
        )
        return result

    return build()


def result_collection_strategy(
    configuration: InsightConfiguration | None = None,
) -> st.SearchStrategy[tuple[InsightsResult, ...]]:
    """Generate finite collections of derived results."""

    return st.lists(
        insights_result_strategy(configuration),
        min_size=0,
        max_size=12,
    ).map(tuple)


def aggregate_group_strategy() -> st.SearchStrategy[AggregateInsightResult]:
    """Generate suppressed and publishable aggregate groups."""

    @st.composite
    def build(draw: Any) -> AggregateInsightResult:
        analyzed = draw(st.integers(min_value=0, max_value=50))
        unknown = draw(st.integers(min_value=0, max_value=analyzed))
        classified = draw(st.integers(min_value=0, max_value=analyzed - unknown))
        not_assessed = draw(st.integers(min_value=0, max_value=analyzed))
        minimum = draw(st.integers(min_value=5, max_value=20))
        suppressed = draw(st.booleans())
        subject_count = draw(
            st.integers(min_value=0, max_value=minimum - 1 if suppressed else 50)
        )
        return make_aggregate_group(
            group_type=draw(st.sampled_from(("account", "region", "model", "period"))),
            group_key=None if suppressed else draw(_non_empty_identifier(48)),
            analyzed_record_count=analyzed,
            classified_record_count=classified,
            unknown_result_count=unknown,
            not_assessed_count=not_assessed,
            unknown_result_proportion=None if analyzed == 0 else unknown / analyzed,
            input_token_total=draw(st.integers(min_value=0, max_value=1_000_000)),
            output_token_total=draw(st.integers(min_value=0, max_value=1_000_000)),
            category_distribution={"code_development": classified} if classified else {},
            confidence_coverage={"medium": classified} if classified else {},
            prompt_quality_coverage={"not_assessed": not_assessed} if not_assessed else {},
            minimum_group_size=minimum,
            distinct_qualifying_subject_count=subject_count,
            suppressed=suppressed,
        )

    return build()


def error_record_strategy() -> st.SearchStrategy[ErrorRecord]:
    """Generate bounded operational errors without sensitive details."""

    return st.builds(
        make_error_record,
        error_code=st.sampled_from(("PARSE_ERROR", "SOURCE_QUERY_FAILED", "ANALYSIS_ERROR")),
        stage=st.sampled_from(ErrorStage),
        analysis_run_id=_non_empty_identifier(32),
        detail=st.sampled_from(("source field was malformed", "query did not complete", "bounded analysis failure")),
        retryable=st.booleans(),
        occurred_at=st.datetimes(
            min_value=datetime(2025, 1, 1),
            max_value=datetime(2026, 12, 31, 23, 59, 59),
            timezones=st.just(UTC),
        ),
        source_request_reference=st.one_of(st.none(), _non_empty_identifier(48)),
    )


def analysis_run_strategy() -> st.SearchStrategy[AnalysisRun]:
    """Generate valid run records across lifecycle states."""

    @st.composite
    def build(draw: Any) -> AnalysisRun:
        configuration = draw(configuration_strategy())
        status = draw(st.sampled_from(ResultStatus))
        return make_analysis_run(
            configuration=configuration,
            status=status,
            analysis_run_id=draw(_non_empty_identifier(32)),
            input_record_count=draw(st.integers(min_value=0, max_value=100)),
            classified_record_count=draw(st.integers(min_value=0, max_value=100)),
            unknown_result_count=draw(st.integers(min_value=0, max_value=100)),
            not_assessed_count=draw(st.integers(min_value=0, max_value=100)),
            processing_error_count=draw(st.integers(min_value=0, max_value=100)),
            redaction_event_count=draw(st.integers(min_value=0, max_value=100)),
            suppressed_group_count=draw(st.integers(min_value=0, max_value=100)),
        )

    return build()


def run_outcome_strategy() -> st.SearchStrategy[RunOutcome]:
    """Generate result/error mixtures for run-accounting tests."""

    @st.composite
    def build(draw: Any) -> RunOutcome:
        configuration = draw(configuration_strategy())
        results = tuple(draw(st.lists(insights_result_strategy(configuration), max_size=8)))
        errors = tuple(draw(st.lists(error_record_strategy(), max_size=4)))
        if results and errors:
            status = ResultStatus.PARTIAL
        elif results:
            status = ResultStatus.COMPLETED
        else:
            status = ResultStatus.FAILED
        return make_run_outcome(
            configuration=configuration,
            results=results,
            errors=errors,
            status=status,
        )

    return build()


__all__ = [
    "ContentAvailability",
    "RedactionFailure",
    "RunOutcome",
    "aggregate_group_strategy",
    "analysis_run_strategy",
    "classification_result_strategy",
    "configuration_strategy",
    "content_availability_strategy",
    "content_case_strategy",
    "error_record_strategy",
    "insights_result_strategy",
    "invocation_record_strategy",
    "long_text",
    "make_aggregate_group",
    "make_analysis_run",
    "make_classification",
    "make_configuration",
    "make_error_record",
    "make_insights_result",
    "make_invocation_record",
    "make_prompt_quality",
    "make_run_outcome",
    "make_scope",
    "make_signal",
    "malformed_value_strategy",
    "prompt_quality_assessment_strategy",
    "redaction_failure_strategy",
    "result_collection_strategy",
    "run_outcome_strategy",
    "scope_strategy",
    "signal_strategy",
    "unicode_text",
]
