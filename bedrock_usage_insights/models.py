"""Canonical data contracts for the optional Bedrock usage insights plane.

The models in this module are dependency-free and intentionally separate from
AWS clients.  They are the schema/configuration oracle's in-memory contract.
Raw prompt and response previews are represented only by the opt-in
``SensitiveContent`` model and are never part of derived result models.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional

SCHEMA_VERSION = "1.0"
ANALYSIS_LOGIC_VERSION = "1.0"
SOURCE_VIEW = "bedrock_logs.bedrock_invocations_view"
ATHENA_WORKGROUP = "bedrock-analytics"
DEFAULT_MINIMUM_GROUP_SIZE = 10
MINIMUM_ALLOWED_GROUP_SIZE = 5


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class UsageCategory(_StringEnum):
    CODE_DEVELOPMENT = "code_development"
    PERSONAL_WORK = "personal_work"
    POTENTIALLY_WASTEFUL_WORK = "potentially_wasteful_work"
    OTHER = "other"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ExposureLevel(_StringEnum):
    INDIVIDUAL = "individual"
    AGGREGATE_ONLY = "aggregate_only"
    NOT_EXPOSED = "not_exposed"


class ConfidenceBand(_StringEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class UnknownReason(_StringEnum):
    OUT_OF_SCOPE = "out_of_scope"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    CONTENT_UNAVAILABLE = "content_unavailable"
    CONTENT_REDACTED = "content_redacted"
    PARSE_ERROR = "parse_error"
    ANALYSIS_ERROR = "analysis_error"


class SignalProvenance(_StringEnum):
    DIRECT_OBSERVATION = "direct_observation"
    CONTENT_DERIVED = "content_derived"


class SignalAvailability(_StringEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    REDACTED = "redacted"
    MALFORMED = "malformed"


class SourceFieldClass(_StringEnum):
    DIRECT_METADATA = "direct_metadata"
    TOKEN_USAGE = "token_usage"
    REQUEST_METADATA = "request_metadata"
    MODEL_FORMAT_PARSE = "model_format_parse"
    PERMITTED_CONTENT_DERIVED_FEATURE = "permitted_content_derived_feature"


class PromptDimension(_StringEnum):
    CLARITY = "clarity"
    SPECIFICITY = "specificity"
    CONTEXT_COMPLETENESS = "context_completeness"
    TASK_DEFINITION = "task_definition"
    OUTPUT_CONSTRAINTS = "output_constraints"


class ContentAnalysisState(_StringEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


class RedactionStatus(_StringEnum):
    NOT_REQUESTED = "not_requested"
    REDACTED = "redacted"
    FAILED = "failed"


class Applicability(_StringEnum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"


class ResultStatus(_StringEnum):
    PENDING = "pending"
    RUNNING = "running"
    PARTIAL = "partial"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PromptAssessmentStatus(_StringEnum):
    ASSESSED = "assessed"
    NOT_ASSESSED = "not_assessed"


class ErrorStage(_StringEnum):
    CONFIGURATION = "configuration"
    SOURCE = "source"
    NORMALIZATION = "normalization"
    PRIVACY = "privacy"
    ANALYSIS = "analysis"
    PERSISTENCE = "persistence"
    PUBLICATION = "publication"
    TRANSMISSION = "transmission"


SUPPORTED_SIGNALS = frozenset(
    {
        "operation",
        "model_id",
        "input_token_count",
        "output_token_count",
        "cache_read_token_count",
        "cache_write_token_count",
        "stop_reason",
        "content_type",
        "request_metadata",
        "model_format",
        "prompt_task_marker",
        "prompt_output_constraint_marker",
        "prompt_context_marker",
        "prompt_specificity_marker",
        "identity_arn",
        "identity_session_name",
        "identity_name",
        "account_owner",
    }
)

DIRECT_SIGNAL_NAMES = frozenset(
    {
        "operation",
        "model_id",
        "input_token_count",
        "output_token_count",
        "cache_read_token_count",
        "cache_write_token_count",
        "stop_reason",
        "content_type",
        "request_metadata",
        "model_format",
        "identity_arn",
        "identity_session_name",
        "identity_name",
        "account_owner",
    }
)
CONTENT_SIGNAL_NAMES = SUPPORTED_SIGNALS - DIRECT_SIGNAL_NAMES


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _enum_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_enum_value(item) for item in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


class Serializable:
    def to_dict(self) -> dict[str, Any]:
        return _enum_value(asdict(self))


@dataclass(frozen=True)
class CategoryDefinition(Serializable):
    category_id: str
    display_name: str
    description: str
    signals: tuple[str, ...]
    score_min: float = 0.0
    score_max: float = 1.0
    evidence_threshold: float = 0.5
    enabled: bool = True
    exposure_level: ExposureLevel = ExposureLevel.AGGREGATE_ONLY
    threshold_interpretation: str = "Evidence score must meet or exceed the configured threshold."


@dataclass(frozen=True)
class SignalDefinition(Serializable):
    name: str
    source_field_class: SourceFieldClass
    provenance: SignalProvenance
    enabled: bool = True


def default_signal_definitions() -> tuple[SignalDefinition, ...]:
    definitions = []
    for name in sorted(SUPPORTED_SIGNALS):
        definitions.append(
            SignalDefinition(
                name=name,
                source_field_class=(
                    SourceFieldClass.PERMITTED_CONTENT_DERIVED_FEATURE
                    if name in CONTENT_SIGNAL_NAMES
                    else (
                        SourceFieldClass.TOKEN_USAGE
                        if name.endswith("token_count")
                        else (
                            SourceFieldClass.REQUEST_METADATA
                            if name == "request_metadata"
                            else (
                                SourceFieldClass.MODEL_FORMAT_PARSE
                                if name == "model_format"
                                else SourceFieldClass.DIRECT_METADATA
                            )
                        )
                    )
                ),
                provenance=(
                    SignalProvenance.CONTENT_DERIVED
                    if name in CONTENT_SIGNAL_NAMES
                    else SignalProvenance.DIRECT_OBSERVATION
                ),
            )
        )
    return tuple(definitions)


def default_category_catalog() -> tuple[CategoryDefinition, ...]:
    return (
        CategoryDefinition(
            UsageCategory.CODE_DEVELOPMENT.value,
            "Code development",
            "A likely pattern consistent with software development signals.",
            ("operation", "model_id", "request_metadata"),
            exposure_level=ExposureLevel.AGGREGATE_ONLY,
        ),
        CategoryDefinition(
            UsageCategory.PERSONAL_WORK.value,
            "Personal work",
            "A likely pattern consistent with configured personal-work signals.",
            ("operation", "model_id", "request_metadata"),
            exposure_level=ExposureLevel.AGGREGATE_ONLY,
        ),
        CategoryDefinition(
            UsageCategory.POTENTIALLY_WASTEFUL_WORK.value,
            "Potentially wasteful work",
            "A pattern with configured signals that may indicate inefficient usage.",
            ("input_token_count", "output_token_count", "model_id"),
            exposure_level=ExposureLevel.AGGREGATE_ONLY,
        ),
        CategoryDefinition(
            UsageCategory.OTHER.value,
            "Other usage",
            "Usage that is consistent with configured signals but no named pattern.",
            ("operation", "model_id"),
            exposure_level=ExposureLevel.AGGREGATE_ONLY,
        ),
        CategoryDefinition(
            UsageCategory.INSUFFICIENT_EVIDENCE.value,
            "Insufficient evidence",
            "Available evidence does not support a configured usage category.",
            (),
            evidence_threshold=1.0,
            exposure_level=ExposureLevel.NOT_EXPOSED,
        ),
    )


@dataclass(frozen=True)
class AnalysisScope(Serializable):
    accounts: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    start_date: Optional[date] = None
    end_date: Optional[date] = None


@dataclass(frozen=True)
class ContentAnalysisPolicy(Serializable):
    state: ContentAnalysisState = ContentAnalysisState.DISABLED
    approved_accounts: tuple[str, ...] = ()
    approved_regions: tuple[str, ...] = ()
    access_policy_reference: Optional[str] = None
    retention_days: Optional[int] = None
    max_preview_length: Optional[int] = None


@dataclass(frozen=True)
class PrivacyPolicy(Serializable):
    exclude_raw_content_from_derived_output: bool = True
    redaction_required: bool = True
    exclusion_rules: tuple[str, ...] = ()
    minimum_group_size: int = DEFAULT_MINIMUM_GROUP_SIZE
    qualifying_subject_definition: str = "approved_non_sensitive_subject"


@dataclass(frozen=True)
class PromptDimensionConfiguration(Serializable):
    enabled: bool = False
    threshold: float = 0.5


@dataclass(frozen=True)
class DashboardPublicationPolicy(Serializable):
    enabled: bool = False
    aggregate_only: bool = True
    approved_role_reference: Optional[str] = None


@dataclass(frozen=True)
class ModelAssistancePolicy(Serializable):
    """Explicit, advisory-only model feature extraction policy."""

    enabled: bool = False
    approved: bool = False
    endpoint_reference: Optional[str] = None
    model_id: Optional[str] = None
    model_version: Optional[str] = None
    allowed_features: tuple[str, ...] = ()
    response_schema_version: str = "1"
    max_input_length: int = 2000
    timeout_seconds: float = 5.0
    max_attempts: int = 2


@dataclass(frozen=True)
class InsightConfiguration(Serializable):
    configuration_version: str
    effective_at: datetime
    author_or_service: str
    change_reason: str
    analysis_scope: Optional[AnalysisScope]
    categories: tuple[CategoryDefinition, ...] = field(default_factory=default_category_catalog)
    signal_definitions: tuple[SignalDefinition, ...] = field(default_factory=default_signal_definitions)
    # Named, versioned conflict policies. The classifier may only apply a rule
    # that is explicitly present in the active configuration.
    precedence_rules: tuple[str, ...] = ()
    enabled: bool = False
    source_view: str = SOURCE_VIEW
    workgroup: str = ATHENA_WORKGROUP
    confidence_thresholds: Mapping[str, float] = field(
        default_factory=lambda: {"low": 0.0, "medium": 0.5, "high": 0.8}
    )
    prompt_dimensions: Mapping[str, PromptDimensionConfiguration] = field(
        default_factory=lambda: {
            dimension.value: PromptDimensionConfiguration() for dimension in PromptDimension
        }
    )
    content_analysis: ContentAnalysisPolicy = field(default_factory=ContentAnalysisPolicy)
    privacy: PrivacyPolicy = field(default_factory=PrivacyPolicy)
    dashboard_publication: DashboardPublicationPolicy = field(
        default_factory=DashboardPublicationPolicy
    )
    model_assistance: ModelAssistancePolicy = field(default_factory=ModelAssistancePolicy)
    retention_days: int = 180
    analysis_logic_version: str = ANALYSIS_LOGIC_VERSION
    schema_version: str = SCHEMA_VERSION

    @property
    def label_metadata(self) -> tuple["LabelMetadata", ...]:
        return tuple(
            LabelMetadata(
                label_id=category.category_id,
                display_name=category.display_name,
                description=category.description,
                threshold_interpretation=category.threshold_interpretation,
                configuration_version=self.configuration_version,
            )
            for category in self.categories
        )


@dataclass(frozen=True)
class IdentityMetadata(Serializable):
    """Restricted source metadata; never copied into derived outputs by default."""

    identity_arn: Optional[str] = None
    session_name: Optional[str] = None
    name: Optional[str] = None
    account_owner: Optional[str] = None


@dataclass(frozen=True)
class SensitiveContent(Serializable):
    """Opt-in content payload used only inside an authorized analysis boundary."""

    prompt_preview: Optional[str] = None
    response_preview: Optional[str] = None
    redaction_status: RedactionStatus = RedactionStatus.NOT_REQUESTED


@dataclass(frozen=True)
class InvocationRecord(Serializable):
    source_request_id: Optional[str]
    source_timestamp_utc: Optional[datetime]
    source_account: Optional[str]
    source_region: Optional[str]
    model_id: Optional[str]
    operation: Optional[str]
    analysis_run_id: str
    configuration_version: str
    analysis_logic_version: str
    input_token_count: Optional[int] = None
    output_token_count: Optional[int] = None
    cache_read_token_count: Optional[int] = None
    cache_write_token_count: Optional[int] = None
    stop_reason: Optional[str] = None
    content_type: Optional[str] = None
    request_metadata: Mapping[str, str] = field(default_factory=dict)
    model_format: Optional[str] = None
    identity_metadata: Optional[IdentityMetadata] = None
    sensitive_content: Optional[SensitiveContent] = None

    def to_dict(self, include_sensitive: bool = False) -> dict[str, Any]:
        result = super().to_dict()
        if not include_sensitive:
            result.pop("sensitive_content", None)
        return result


@dataclass(frozen=True)
class Signal(Serializable):
    name: str
    value_type: str
    value: Any
    source_field_class: SourceFieldClass
    provenance: SignalProvenance
    availability: SignalAvailability = SignalAvailability.AVAILABLE
    summary: Optional[str] = None


@dataclass(frozen=True)
class EvidenceReference(Serializable):
    signal_names: tuple[str, ...] = ()
    source_field_classes: tuple[SourceFieldClass, ...] = ()
    summaries: tuple[str, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplicabilityMarkers(Serializable):
    primary_category: Applicability = Applicability.NOT_APPLICABLE
    secondary_categories: Applicability = Applicability.NOT_APPLICABLE
    scores: Applicability = Applicability.NOT_APPLICABLE
    confidence_band: Applicability = Applicability.NOT_APPLICABLE
    evidence_reference: Applicability = Applicability.NOT_APPLICABLE
    unknown_reason: Applicability = Applicability.NOT_APPLICABLE


@dataclass(frozen=True)
class ClassificationResult(Serializable):
    primary_category: Optional[str] = None
    secondary_categories: tuple[str, ...] = ()
    category_scores: Mapping[str, float] = field(default_factory=dict)
    confidence_band: Optional[ConfidenceBand] = None
    unknown_reason: Optional[UnknownReason] = None
    evidence_thresholds: Mapping[str, float] = field(default_factory=dict)
    evidence_reference: Optional[EvidenceReference] = None
    limitations: tuple[str, ...] = ()
    applicability: ApplicabilityMarkers = field(default_factory=ApplicabilityMarkers)


@dataclass(frozen=True)
class PromptQualityDimensionResult(Serializable):
    dimension: str
    status: PromptAssessmentStatus
    score: Optional[float] = None
    confidence_band: Optional[ConfidenceBand] = None
    evidence_reference: Optional[EvidenceReference] = None
    threshold_interpretation: Optional[str] = None
    not_assessed_reason: Optional[str] = None


@dataclass(frozen=True)
class PromptQualityAssessment(Serializable):
    dimensions: Mapping[str, PromptQualityDimensionResult] = field(default_factory=dict)
    content_state: ContentAnalysisState = ContentAnalysisState.DISABLED
    limitation: str = "Describes analyzed prompt characteristics, not a person or team."


@dataclass(frozen=True)
class LabelMetadata(Serializable):
    label_id: str
    display_name: str
    description: str
    threshold_interpretation: str
    configuration_version: str


@dataclass(frozen=True)
class InsightsResult(Serializable):
    source_request_id: Optional[str]
    source_timestamp_utc: Optional[datetime]
    result_timestamp_utc: Optional[datetime]
    source_account: Optional[str]
    source_region: Optional[str]
    model_id: Optional[str]
    analysis_run_id: str
    configuration_version: str
    analysis_logic_version: str
    classification: ClassificationResult = field(default_factory=ClassificationResult)
    prompt_quality: PromptQualityAssessment = field(default_factory=PromptQualityAssessment)
    input_token_count: Optional[int] = None
    output_token_count: Optional[int] = None
    cache_read_token_count: Optional[int] = None
    cache_write_token_count: Optional[int] = None
    label_metadata: tuple[LabelMetadata, ...] = ()
    privacy_mode: str = "default_no_raw_content"
    schema_version: str = SCHEMA_VERSION
    use_limitation: str = (
        "For aggregate usage analysis and prompt improvement; not for employee ranking, "
        "performance evaluation, disciplinary action, or automated access decisions."
    )


@dataclass(frozen=True)
class AggregateInsightResult(Serializable):
    group_type: str
    group_key: Optional[str]
    period_start: date
    period_end: date
    analyzed_record_count: int
    classified_record_count: int
    unknown_result_count: int
    not_assessed_count: int
    unknown_result_proportion: Optional[float]
    input_token_total: int
    output_token_total: int
    category_distribution: Mapping[str, int]
    confidence_coverage: Mapping[str, int]
    prompt_quality_coverage: Mapping[str, int]
    estimated_or_allocated_cost: Optional[float] = None
    cost_measure_label: Optional[str] = None
    minimum_group_size: int = DEFAULT_MINIMUM_GROUP_SIZE
    distinct_qualifying_subject_count: Optional[int] = None
    suppressed: bool = False
    analysis_run_id: str = ""
    configuration_version: str = ""
    analysis_logic_version: str = ANALYSIS_LOGIC_VERSION
    publication_status: str = "staged"


@dataclass(frozen=True)
class AnalysisRun(Serializable):
    analysis_run_id: str
    status: ResultStatus
    source_scope: AnalysisScope
    configuration_version: str
    analysis_logic_version: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    source_availability_window: Optional[tuple[date, date]] = None
    output_location: Optional[str] = None
    query_execution_ids: tuple[str, ...] = ()
    input_record_count: int = 0
    classified_record_count: int = 0
    unknown_result_count: int = 0
    not_assessed_count: int = 0
    processing_error_count: int = 0
    redaction_event_count: int = 0
    suppressed_group_count: int = 0
    dashboard_publication_requested: bool = False
    dashboard_publication_approved: bool = False
    transmission_requested: bool = False
    requested_scope: Optional[AnalysisScope] = None


@dataclass(frozen=True)
class ConfigurationAuditRecord(Serializable):
    configuration_version: str
    effective_at: datetime
    author_or_service: str
    change_reason: str
    enabled_categories: tuple[str, ...]
    enabled_prompt_dimensions: tuple[str, ...]
    privacy_settings: PrivacyPolicy
    analysis_scope: AnalysisScope
    recorded_at: datetime = field(default_factory=_utc_now)


@dataclass(frozen=True)
class ErrorRecord(Serializable):
    error_code: str
    stage: ErrorStage
    analysis_run_id: str
    detail: str
    retryable: bool
    occurred_at: datetime
    source_request_reference: Optional[str] = None
    content_free: bool = True


DEFAULT_DERIVED_RESULT_FIELDS = frozenset(
    field_info.name for field_info in fields(InsightsResult)
)
SENSITIVE_CONTENT_FIELDS = frozenset({"prompt_preview", "response_preview"})
