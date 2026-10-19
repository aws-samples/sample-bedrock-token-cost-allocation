"""Validation and schema oracle for Bedrock usage insights contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from .models import (
    ATHENA_WORKGROUP,
    DEFAULT_MINIMUM_GROUP_SIZE,
    MINIMUM_ALLOWED_GROUP_SIZE,
    SOURCE_VIEW,
    SUPPORTED_SIGNALS,
    Applicability,
    AnalysisRun,
    AnalysisScope,
    AggregateInsightResult,
    ApplicabilityMarkers,
    CategoryDefinition,
    ClassificationResult,
    ConfidenceBand,
    ConfigurationAuditRecord,
    ContentAnalysisState,
    ErrorRecord,
    EvidenceReference,
    ExposureLevel,
    InsightConfiguration,
    InsightsResult,
    InvocationRecord,
    LabelMetadata,
    PromptAssessmentStatus,
    PromptDimension,
    PromptDimensionConfiguration,
    PromptQualityAssessment,
    PromptQualityDimensionResult,
    ResultStatus,
    SignalDefinition,
    SignalProvenance,
    SourceFieldClass,
    UnknownReason,
    UsageCategory,
)

MAX_CATEGORY_ID_LENGTH = 64
MAX_DISPLAY_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 500
MAX_CHANGE_REASON_LENGTH = 500
MAX_ERROR_DETAIL_LENGTH = 500
MAX_PRECEDENCE_RULE_LENGTH = 100
MAX_PRECEDENCE_RULES = 50
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str


class SchemaValidationError(ValueError):
    """Raised when a canonical model does not satisfy the feature contract."""

    def __init__(self, issues: Iterable[ValidationIssue]):
        self.issues = tuple(issues)
        message = "; ".join(f"{issue.path}: {issue.message}" for issue in self.issues)
        super().__init__(message)


def _issue(path: str, message: str) -> ValidationIssue:
    return ValidationIssue(path, message)


def _is_utc(value: Optional[datetime]) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() == timezone.utc.utcoffset(value)


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite_unit(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0.0 <= value <= 1.0


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_scope(scope: Optional[AnalysisScope], path: str = "analysis_scope") -> list[ValidationIssue]:
    if scope is None:
        return [_issue(path, "an approved bounded source scope is required")]
    issues: list[ValidationIssue] = []
    for name, values in (
        ("accounts", scope.accounts),
        ("regions", scope.regions),
        ("models", scope.models),
        ("operations", scope.operations),
    ):
        if isinstance(values, str) or not isinstance(values, (tuple, list)) or not values or any(not _non_empty(value) for value in values):
            issues.append(_issue(f"{path}.{name}", "must contain at least one non-empty value"))
    if not isinstance(scope.start_date, date) or isinstance(scope.start_date, datetime) or not isinstance(scope.end_date, date) or isinstance(scope.end_date, datetime):
        issues.append(_issue(path, "start_date and end_date are required UTC date values; unbounded dates are not allowed"))
    elif scope.start_date > scope.end_date:
        issues.append(_issue(path, "start_date must not be after end_date"))
    return issues


def _validate_category(category: CategoryDefinition, index: int) -> list[ValidationIssue]:
    path = f"categories[{index}]"
    if not isinstance(category, CategoryDefinition):
        return [_issue(path, "must be a CategoryDefinition")]
    issues: list[ValidationIssue] = []
    if not _non_empty(category.category_id) or len(category.category_id) > MAX_CATEGORY_ID_LENGTH:
        issues.append(_issue(f"{path}.category_id", f"must be non-empty and at most {MAX_CATEGORY_ID_LENGTH} characters"))
    if not _non_empty(category.display_name) or len(category.display_name) > MAX_DISPLAY_NAME_LENGTH:
        issues.append(_issue(f"{path}.display_name", f"must be non-empty and at most {MAX_DISPLAY_NAME_LENGTH} characters"))
    if not _non_empty(category.description) or len(category.description) > MAX_DESCRIPTION_LENGTH:
        issues.append(_issue(f"{path}.description", f"must be non-empty and at most {MAX_DESCRIPTION_LENGTH} characters"))
    score_fields_valid = all(_finite_unit(getattr(category, field_name, None)) for field_name in ("score_min", "score_max"))
    if score_fields_valid and category.score_min > category.score_max:
        issues.append(_issue(f"{path}.score_range", "score_min must not exceed score_max"))
    for field_name in ("score_min", "score_max", "evidence_threshold"):
        if not _finite_unit(getattr(category, field_name, None)):
            issues.append(_issue(f"{path}.{field_name}", "must be finite and within 0.00 through 1.00"))
    signals = category.signals if isinstance(category.signals, (tuple, list)) else ()
    if not signals and category.category_id != UsageCategory.INSUFFICIENT_EVIDENCE.value:
        issues.append(_issue(f"{path}.signals", "must contain at least one signal"))
    for signal in signals:
        if not isinstance(signal, str) or signal not in SUPPORTED_SIGNALS:
            issues.append(_issue(f"{path}.signals", f"unsupported Signal: {signal}"))
    if not isinstance(category.enabled, bool):
        issues.append(_issue(f"{path}.enabled", "must be a boolean"))
    if category.exposure_level not in set(ExposureLevel):
        issues.append(_issue(f"{path}.exposure_level", "must be individual, aggregate_only, or not_exposed"))
    return issues


def _validate_signal(signal: SignalDefinition, index: int) -> list[ValidationIssue]:
    path = f"signal_definitions[{index}]"
    if not isinstance(signal, SignalDefinition):
        return [_issue(path, "must be a SignalDefinition")]
    issues: list[ValidationIssue] = []
    if not isinstance(signal.name, str) or signal.name not in SUPPORTED_SIGNALS:
        issues.append(_issue(f"{path}.name", f"unsupported Signal: {signal.name}"))
    if not isinstance(signal.provenance, SignalProvenance) or signal.provenance not in set(SignalProvenance):
        issues.append(_issue(f"{path}.provenance", "must identify direct_observation or content_derived"))
    if not isinstance(signal.source_field_class, SourceFieldClass) or signal.source_field_class not in set(SourceFieldClass):
        issues.append(_issue(f"{path}.source_field_class", "is not a permitted source field class"))
    if not isinstance(signal.enabled, bool):
        issues.append(_issue(f"{path}.enabled", "must be a boolean"))
    return issues


def _validate_evidence(evidence: Optional[EvidenceReference], path: str) -> list[ValidationIssue]:
    if evidence is None:
        return []
    issues: list[ValidationIssue] = []
    if any(not _non_empty(name) for name in evidence.signal_names):
        issues.append(_issue(f"{path}.signal_names", "must contain only non-empty names"))
    if any(not isinstance(count, int) or count < 0 for count in evidence.counts.values()):
        issues.append(_issue(f"{path}.counts", "counts must be nonnegative integers"))
    if any("prompt" in summary.lower() or "response" in summary.lower() for summary in evidence.summaries):
        issues.append(_issue(f"{path}.summaries", "must not contain raw prompt or response content"))
    return issues


class ConfigurationSchemaOracle:
    """Pure validation oracle for candidate and accepted configurations."""

    @staticmethod
    def validate(configuration: InsightConfiguration) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        if not _non_empty(configuration.configuration_version):
            issues.append(_issue("configuration_version", "must be non-empty"))
        if not _is_utc(configuration.effective_at):
            issues.append(_issue("effective_at", "must be a timezone-aware UTC timestamp"))
        if not _non_empty(configuration.author_or_service):
            issues.append(_issue("author_or_service", "must be non-empty"))
        if not _non_empty(configuration.change_reason) or len(configuration.change_reason) > MAX_CHANGE_REASON_LENGTH:
            issues.append(_issue("change_reason", f"must be non-empty and at most {MAX_CHANGE_REASON_LENGTH} characters"))
        if not isinstance(configuration.enabled, bool):
            issues.append(_issue("enabled", "must be an explicit boolean"))
        issues.extend(_validate_scope(configuration.analysis_scope))
        if configuration.source_view != SOURCE_VIEW:
            issues.append(_issue("source_view", f"must be {SOURCE_VIEW}"))
        if configuration.workgroup != ATHENA_WORKGROUP:
            issues.append(_issue("workgroup", f"must be {ATHENA_WORKGROUP}"))
        categories = configuration.categories
        if not isinstance(categories, (tuple, list)) or not categories:
            issues.append(_issue("categories", "must contain the initial category catalog"))
            categories = ()
        category_ids = [category.category_id for category in categories if isinstance(category, CategoryDefinition)]
        try:
            duplicate_categories = len(category_ids) != len(set(category_ids))
        except TypeError:
            duplicate_categories = False
        if duplicate_categories:
            issues.append(_issue("categories", "category identifiers must be unique"))
        issues.extend(issue for index, category in enumerate(categories) for issue in _validate_category(category, index))
        signal_definitions = configuration.signal_definitions
        if not isinstance(signal_definitions, (tuple, list)):
            issues.append(_issue("signal_definitions", "must be a sequence"))
            signal_definitions = ()
        signal_names = [signal.name for signal in signal_definitions if isinstance(signal, SignalDefinition)]
        try:
            duplicate_signals = len(signal_names) != len(set(signal_names))
        except TypeError:
            duplicate_signals = False
        if duplicate_signals:
            issues.append(_issue("signal_definitions", "Signal names must be unique"))
        issues.extend(issue for index, signal in enumerate(signal_definitions) for issue in _validate_signal(signal, index))
        precedence_rules = configuration.precedence_rules
        if not isinstance(precedence_rules, (tuple, list)):
            issues.append(_issue("precedence_rules", "must be a sequence of named rules"))
        else:
            if len(precedence_rules) > MAX_PRECEDENCE_RULES:
                issues.append(_issue("precedence_rules", f"must contain at most {MAX_PRECEDENCE_RULES} rules"))
            try:
                duplicate_rules = len(precedence_rules) != len(set(precedence_rules))
            except TypeError:
                duplicate_rules = False
            if duplicate_rules:
                issues.append(_issue("precedence_rules", "rule names must be unique"))
            for index, rule in enumerate(precedence_rules):
                if not _non_empty(rule) or len(rule) > MAX_PRECEDENCE_RULE_LENGTH:
                    issues.append(_issue(f"precedence_rules[{index}]", f"must be non-empty and at most {MAX_PRECEDENCE_RULE_LENGTH} characters"))
        try:
            configured_signals = set(name for name in signal_names if isinstance(name, str))
        except TypeError:
            configured_signals = set()
        for index, category in enumerate(categories):
            if not isinstance(category, CategoryDefinition):
                continue
            for signal in category.signals:
                if signal not in configured_signals:
                    issues.append(_issue(f"categories[{index}].signals", f"Signal {signal} has no definition"))
        thresholds = configuration.confidence_thresholds
        if not isinstance(thresholds, Mapping):
            issues.append(_issue("confidence_thresholds", "must define exactly low, medium, and high"))
        elif set(thresholds) != {band.value for band in ConfidenceBand}:
            issues.append(_issue("confidence_thresholds", "must define exactly low, medium, and high"))
        else:
            if any(not _finite_unit(value) for value in thresholds.values()):
                issues.append(_issue("confidence_thresholds", "all thresholds must be finite and within 0.00 through 1.00"))
            elif not (thresholds["low"] <= thresholds["medium"] <= thresholds["high"]):
                issues.append(_issue("confidence_thresholds", "thresholds must be ordered low <= medium <= high"))
        prompt_dimensions = configuration.prompt_dimensions
        if not isinstance(prompt_dimensions, Mapping):
            issues.append(_issue("prompt_dimensions", "must be a mapping of supported dimensions"))
            prompt_dimensions = {}
        configured_dimensions = set(prompt_dimensions)
        allowed_dimensions = {dimension.value for dimension in PromptDimension}
        for dimension in configured_dimensions - allowed_dimensions:
            issues.append(_issue("prompt_dimensions", f"unsupported prompt dimension: {dimension}"))
        for dimension, settings in prompt_dimensions.items():
            if not isinstance(settings, PromptDimensionConfiguration):
                issues.append(_issue(f"prompt_dimensions.{dimension}", "must contain dimension configuration"))
            elif not _finite_unit(settings.threshold):
                issues.append(_issue(f"prompt_dimensions.{dimension}.threshold", "must be finite and within 0.00 through 1.00"))
        content = configuration.content_analysis
        if not isinstance(content.state, ContentAnalysisState):
            issues.append(_issue("content_analysis.state", "must be explicitly disabled or enabled"))
        elif content.state == ContentAnalysisState.ENABLED:
            if not content.approved_accounts or any(not _non_empty(value) for value in content.approved_accounts):
                issues.append(_issue("content_analysis.approved_accounts", "approved non-empty account values are required when content analysis is enabled"))
            if not content.approved_regions or any(not _non_empty(value) for value in content.approved_regions):
                issues.append(_issue("content_analysis.approved_regions", "approved non-empty Region values are required when content analysis is enabled"))
            if not _non_empty(content.access_policy_reference):
                issues.append(_issue("content_analysis.access_policy_reference", "required when content analysis is enabled"))
            if not isinstance(content.retention_days, int) or isinstance(content.retention_days, bool) or not MIN_RETENTION_DAYS <= content.retention_days <= MAX_RETENTION_DAYS:
                issues.append(_issue("content_analysis.retention_days", f"must be between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}"))
            if content.max_preview_length is not None and (not isinstance(content.max_preview_length, int) or isinstance(content.max_preview_length, bool) or content.max_preview_length <= 0):
                issues.append(_issue("content_analysis.max_preview_length", "must be a positive integer when present"))
        elif content.max_preview_length is not None and (not isinstance(content.max_preview_length, int) or isinstance(content.max_preview_length, bool) or content.max_preview_length <= 0):
            issues.append(_issue("content_analysis.max_preview_length", "must be a positive integer when present"))
        privacy = configuration.privacy
        if not isinstance(privacy.exclude_raw_content_from_derived_output, bool):
            issues.append(_issue("privacy.exclude_raw_content_from_derived_output", "must be a boolean"))
        if not isinstance(privacy.redaction_required, bool):
            issues.append(_issue("privacy.redaction_required", "must be a boolean"))
        if privacy.exclude_raw_content_from_derived_output is False:
            issues.append(_issue("privacy.exclude_raw_content_from_derived_output", "raw content must be excluded from derived output"))
        if content.state == ContentAnalysisState.ENABLED and privacy.redaction_required is False:
            issues.append(_issue("privacy.redaction_required", "content analysis requires redaction"))
        if not isinstance(privacy.exclusion_rules, (tuple, list)) or any(not _non_empty(rule) for rule in privacy.exclusion_rules):
            issues.append(_issue("privacy.exclusion_rules", "must contain only non-empty rules"))
        if not isinstance(configuration.retention_days, int) or isinstance(configuration.retention_days, bool) or not MIN_RETENTION_DAYS <= configuration.retention_days <= MAX_RETENTION_DAYS:
            issues.append(_issue("retention_days", f"must be between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}"))
        if not isinstance(privacy.minimum_group_size, int) or isinstance(privacy.minimum_group_size, bool) or privacy.minimum_group_size < MINIMUM_ALLOWED_GROUP_SIZE:
            issues.append(_issue("privacy.minimum_group_size", f"must be an integer of at least {MINIMUM_ALLOWED_GROUP_SIZE}"))
        if not _non_empty(privacy.qualifying_subject_definition):
            issues.append(_issue("privacy.qualifying_subject_definition", "must be non-empty"))
        dashboard = configuration.dashboard_publication
        if not isinstance(dashboard.enabled, bool) or not isinstance(dashboard.aggregate_only, bool):
            issues.append(_issue("dashboard_publication", "enabled and aggregate_only must be booleans"))
        if dashboard.enabled and not dashboard.aggregate_only:
            issues.append(_issue("dashboard_publication.aggregate_only", "must remain true for the feature dashboard"))
        if dashboard.enabled and not _non_empty(dashboard.approved_role_reference):
            issues.append(_issue("dashboard_publication.approved_role_reference", "required when dashboard publication is enabled"))
        model = configuration.model_assistance
        if not isinstance(model.enabled, bool):
            issues.append(_issue("model_assistance.enabled", "must be a boolean"))
        if model.enabled and (not _non_empty(model.endpoint_reference) or not _non_empty(model.model_id) or not _non_empty(model.model_version)):
            issues.append(_issue("model_assistance", "endpoint_reference, model_id, and model_version are required when enabled"))
        return tuple(issues)

    @staticmethod
    def assert_valid(configuration: InsightConfiguration) -> None:
        issues = ConfigurationSchemaOracle.validate(configuration)
        if issues:
            raise SchemaValidationError(issues)


class CanonicalSchemaOracle:
    """Validation for records and outputs that cross the feature data contract."""

    @staticmethod
    def validate_invocation(record: InvocationRecord) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        for name, value in (
            ("source_request_id", record.source_request_id),
            ("source_account", record.source_account),
            ("source_region", record.source_region),
            ("model_id", record.model_id),
            ("operation", record.operation),
            ("analysis_run_id", record.analysis_run_id),
            ("configuration_version", record.configuration_version),
            ("analysis_logic_version", record.analysis_logic_version),
        ):
            if not _non_empty(value):
                issues.append(_issue(name, "required traceability metadata must be non-empty"))
        if not _is_utc(record.source_timestamp_utc):
            issues.append(_issue("source_timestamp_utc", "must be a timezone-aware UTC timestamp"))
        for name in ("input_token_count", "output_token_count", "cache_read_token_count", "cache_write_token_count"):
            value = getattr(record, name)
            if value is not None and not _nonnegative_int(value):
                issues.append(_issue(name, "must be a nonnegative integer when present"))
        return tuple(issues)

    @staticmethod
    def validate_classification(result: ClassificationResult, configuration: Optional[InsightConfiguration] = None) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        allowed_categories = {category.category_id for category in configuration.categories} if configuration else {category.value for category in UsageCategory}
        if result.primary_category and result.primary_category not in allowed_categories:
            issues.append(_issue("primary_category", "is not a permitted Usage_Category"))
        if len(result.secondary_categories) != len(set(result.secondary_categories)):
            issues.append(_issue("secondary_categories", "must contain distinct category identifiers"))
        if any(category not in allowed_categories for category in result.secondary_categories):
            issues.append(_issue("secondary_categories", "contains a category outside the active configuration"))
        if result.primary_category == UsageCategory.INSUFFICIENT_EVIDENCE.value:
            issues.append(_issue("primary_category", "insufficient_evidence must be represented by unknown_reason, not a primary category"))
        if result.unknown_reason is not None and not isinstance(result.unknown_reason, UnknownReason):
            issues.append(_issue("unknown_reason", "is not a permitted Unknown_Result reason"))
        if result.confidence_band is not None and not isinstance(result.confidence_band, ConfidenceBand):
            issues.append(_issue("confidence_band", "is not a permitted Confidence_Band"))
        if not isinstance(result.applicability, ApplicabilityMarkers):
            issues.append(_issue("applicability", "must contain applicability markers"))
        else:
            for marker_name, marker in vars(result.applicability).items():
                if not isinstance(marker, Applicability):
                    issues.append(_issue(f"applicability.{marker_name}", "must be applicable or not_applicable"))
        if result.unknown_reason is None and result.primary_category and result.confidence_band is None:
            issues.append(_issue("confidence_band", "required for a non-unknown category result"))
        for category, score in result.category_scores.items():
            if category not in allowed_categories or not _finite_unit(score):
                issues.append(_issue(f"category_scores.{category}", "must use a permitted category and score in [0.00, 1.00]"))
        for category, threshold in result.evidence_thresholds.items():
            if not _finite_unit(threshold):
                issues.append(_issue(f"evidence_thresholds.{category}", "must be in [0.00, 1.00]"))
        issues.extend(_validate_evidence(result.evidence_reference, "evidence_reference"))
        return tuple(issues)

    @staticmethod
    def validate_prompt_quality(assessment: PromptQualityAssessment) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        allowed_dimensions = {dimension.value for dimension in PromptDimension}
        for dimension, result in assessment.dimensions.items():
            path = f"dimensions.{dimension}"
            if dimension not in allowed_dimensions or result.dimension != dimension:
                issues.append(_issue(path, "must use a permitted dimension identifier"))
            if result.status not in set(PromptAssessmentStatus):
                issues.append(_issue(f"{path}.status", "must be assessed or not_assessed"))
            elif result.status == PromptAssessmentStatus.ASSESSED:
                if not _finite_unit(result.score):
                    issues.append(_issue(f"{path}.score", "must be in [0.00, 1.00] when assessed"))
                if result.confidence_band is None:
                    issues.append(_issue(f"{path}.confidence_band", "required when assessed"))
                if result.evidence_reference is None:
                    issues.append(_issue(f"{path}.evidence_reference", "required when assessed"))
                issues.extend(_validate_evidence(result.evidence_reference, f"{path}.evidence_reference"))
            elif result.status == PromptAssessmentStatus.NOT_ASSESSED:
                if result.score is not None or result.confidence_band is not None or result.evidence_reference is not None:
                    issues.append(_issue(path, "not_assessed dimensions cannot carry a score, confidence, or evidence"))
                if not _non_empty(result.not_assessed_reason):
                    issues.append(_issue(f"{path}.not_assessed_reason", "required when not_assessed"))
        return tuple(issues)

    @staticmethod
    def validate_result(result: InsightsResult, configuration: Optional[InsightConfiguration] = None) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        for name, value in (
            ("source_request_id", result.source_request_id),
            ("source_account", result.source_account),
            ("source_region", result.source_region),
            ("model_id", result.model_id),
            ("analysis_run_id", result.analysis_run_id),
            ("configuration_version", result.configuration_version),
            ("analysis_logic_version", result.analysis_logic_version),
        ):
            if not _non_empty(value):
                issues.append(_issue(name, "required result traceability metadata must be non-empty"))
        for name, value in (("source_timestamp_utc", result.source_timestamp_utc), ("result_timestamp_utc", result.result_timestamp_utc)):
            if not _is_utc(value):
                issues.append(_issue(name, "must be a timezone-aware UTC timestamp"))
        issues.extend(CanonicalSchemaOracle.validate_classification(result.classification, configuration))
        issues.extend(CanonicalSchemaOracle.validate_prompt_quality(result.prompt_quality))
        if configuration and result.configuration_version != configuration.configuration_version:
            issues.append(_issue("configuration_version", "must match the exact configuration used for labels and thresholds"))
        for label in result.label_metadata:
            expected = None
            if configuration:
                expected = next((item for item in configuration.label_metadata if item.label_id == label.label_id), None)
            if not _non_empty(label.label_id) or label.configuration_version != result.configuration_version:
                issues.append(_issue("label_metadata", "labels must be non-empty and carry the result configuration version"))
            if configuration and expected is None:
                issues.append(_issue(f"label_metadata.{label.label_id}", "label is not defined by the exact configuration version"))
            elif expected and label != expected:
                issues.append(_issue(f"label_metadata.{label.label_id}", "display metadata must match the exact configuration version"))
        for name in ("input_token_count", "output_token_count", "cache_read_token_count", "cache_write_token_count"):
            value = getattr(result, name)
            if value is not None and not _nonnegative_int(value):
                issues.append(_issue(name, "must be a nonnegative integer when present"))
        if "prompt_preview" in result.to_dict() or "response_preview" in result.to_dict():
            issues.append(_issue("result", "raw prompt and response fields are not permitted in derived results"))
        return tuple(issues)

    @staticmethod
    def validate_aggregate(result: AggregateInsightResult) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        for name in ("analyzed_record_count", "classified_record_count", "unknown_result_count", "not_assessed_count", "input_token_total", "output_token_total", "minimum_group_size"):
            if not _nonnegative_int(getattr(result, name)):
                issues.append(_issue(name, "must be a nonnegative integer"))
        if not isinstance(result.minimum_group_size, int) or result.minimum_group_size < MINIMUM_ALLOWED_GROUP_SIZE:
            issues.append(_issue("minimum_group_size", f"must be an integer of at least {MINIMUM_ALLOWED_GROUP_SIZE}"))
        if result.analyzed_record_count == 0 and result.unknown_result_proportion is not None:
            issues.append(_issue("unknown_result_proportion", "must be unavailable when analyzed_record_count is zero"))
        if result.analyzed_record_count > 0 and not _finite_unit(result.unknown_result_proportion):
            issues.append(_issue("unknown_result_proportion", "must be in [0.00, 1.00] for non-empty aggregates"))
        if result.suppressed and result.group_key is not None:
            issues.append(_issue("group_key", "must be absent for a suppressed group"))
        if result.suppressed and result.distinct_qualifying_subject_count is not None and isinstance(result.minimum_group_size, int) and result.distinct_qualifying_subject_count >= result.minimum_group_size:
            issues.append(_issue("distinct_qualifying_subject_count", "a suppressed group must be below minimum group size"))
        if result.cost_measure_label and result.cost_measure_label not in {"estimated", "allocated"}:
            issues.append(_issue("cost_measure_label", "must be estimated or allocated"))
        return tuple(issues)

    @staticmethod
    def validate_run(run: AnalysisRun) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        if not _non_empty(run.analysis_run_id) or not _non_empty(run.configuration_version) or not _non_empty(run.analysis_logic_version):
            issues.append(_issue("run_metadata", "run ID, configuration version, and logic version are required"))
        if run.status not in set(ResultStatus):
            issues.append(_issue("status", "is not a permitted run status"))
        if not _is_utc(run.started_at) or (run.ended_at is not None and not _is_utc(run.ended_at)):
            issues.append(_issue("timing", "start and end times must be timezone-aware UTC timestamps"))
        if run.ended_at is not None and run.ended_at < run.started_at:
            issues.append(_issue("ended_at", "must not precede started_at"))
        for name in ("input_record_count", "classified_record_count", "unknown_result_count", "not_assessed_count", "processing_error_count", "redaction_event_count", "suppressed_group_count"):
            if not _nonnegative_int(getattr(run, name)):
                issues.append(_issue(name, "must be a nonnegative integer"))
        return tuple(issues)

    @staticmethod
    def validate_audit(record: ConfigurationAuditRecord) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        if not _non_empty(record.configuration_version) or not _non_empty(record.author_or_service) or not _non_empty(record.change_reason):
            issues.append(_issue("audit_metadata", "version, author/service, and change reason are required"))
        if not _is_utc(record.effective_at) or not _is_utc(record.recorded_at):
            issues.append(_issue("audit_timestamps", "must be timezone-aware UTC timestamps"))
        issues.extend(_validate_scope(record.analysis_scope))
        return tuple(issues)

    @staticmethod
    def validate_error(record: ErrorRecord) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        if not _non_empty(record.error_code) or not _non_empty(record.analysis_run_id):
            issues.append(_issue("error_metadata", "error code and run ID are required"))
        if not _non_empty(record.detail) or len(record.detail) > MAX_ERROR_DETAIL_LENGTH:
            issues.append(_issue("detail", f"must be non-empty and at most {MAX_ERROR_DETAIL_LENGTH} characters"))
        if not _is_utc(record.occurred_at):
            issues.append(_issue("occurred_at", "must be timezone-aware UTC timestamp"))
        if not record.content_free:
            issues.append(_issue("content_free", "error records must be content-free"))
        return tuple(issues)


# Stable field contracts used by schema/static tests and downstream adapters.
SCHEMA_ORACLE = {
    "schema_version": "1.0",
    "categories": tuple(category.value for category in UsageCategory),
    "confidence_bands": tuple(band.value for band in ConfidenceBand),
    "unknown_reasons": tuple(reason.value for reason in UnknownReason),
    "prompt_dimensions": tuple(dimension.value for dimension in PromptDimension),
    "minimum_group_size_default": DEFAULT_MINIMUM_GROUP_SIZE,
    "minimum_group_size_minimum": MINIMUM_ALLOWED_GROUP_SIZE,
    "precedence_rule_name_max_length": MAX_PRECEDENCE_RULE_LENGTH,
    "precedence_rule_count_maximum": MAX_PRECEDENCE_RULES,
    "derived_result_forbidden_fields": ("prompt_preview", "response_preview"),
    "required_result_fields": (
        "source_request_id",
        "source_timestamp_utc",
        "result_timestamp_utc",
        "source_account",
        "source_region",
        "model_id",
        "analysis_run_id",
        "configuration_version",
        "analysis_logic_version",
    ),
}
