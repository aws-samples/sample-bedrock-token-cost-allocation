"""Pure configuration parsing, governance, versioning, and activation.

The default store is intentionally in-memory.  A durable implementation can
use the same validation and acceptance boundary without changing the model
contract or allowing an invalid candidate to replace the active version.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from threading import RLock
from typing import Any, Callable, Iterable, Mapping, Protocol
from uuid import uuid4

from .models import (
    AnalysisScope,
    CategoryDefinition,
    ConfigurationAuditRecord,
    ContentAnalysisPolicy,
    ContentAnalysisState,
    DashboardPublicationPolicy,
    ExposureLevel,
    InsightConfiguration,
    ModelAssistancePolicy,
    PrivacyPolicy,
    PromptDimensionConfiguration,
    SignalDefinition,
    SignalProvenance,
    SourceFieldClass,
)
from .oracle import ConfigurationSchemaOracle, SchemaValidationError, ValidationIssue


class FrozenDict(dict):
    """A small dict-compatible immutable mapping used by accepted versions."""

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenDict":
        return self

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        raise TypeError("accepted configuration mappings are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable


def _freeze_mapping(value: Mapping[Any, Any]) -> FrozenDict:
    return FrozenDict({key: item for key, item in value.items()})


def _freeze_configuration(configuration: InsightConfiguration) -> InsightConfiguration:
    """Freeze mutable mapping fields without changing the public model shape."""

    return replace(
        configuration,
        confidence_thresholds=_freeze_mapping(configuration.confidence_thresholds),
        prompt_dimensions=_freeze_mapping(configuration.prompt_dimensions),
        precedence_rules=tuple(configuration.precedence_rules),
        categories=tuple(configuration.categories),
        signal_definitions=tuple(configuration.signal_definitions),
    )


def _as_mapping(document: Mapping[str, Any] | str | bytes) -> Mapping[str, Any]:
    if isinstance(document, Mapping):
        return document
    if isinstance(document, bytes):
        document = document.decode("utf-8")
    if isinstance(document, str):
        try:
            parsed = json.loads(document)
        except (TypeError, ValueError) as error:
            raise SchemaValidationError((ValidationIssue("$", f"invalid configuration JSON: {error}"),)) from error
        if isinstance(parsed, Mapping):
            return parsed
    raise SchemaValidationError((ValidationIssue("$", "configuration must be a mapping or JSON object"),))


def _enum(enum_type: type[Any], value: Any, default: Any = None) -> Any:
    if value is None and default is not None:
        return default
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return value


def _tuple_values(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (tuple, list, set, frozenset)):
        return tuple(value)
    return ()


def _date_value(value: Any) -> Any:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    return value


def _datetime_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


def _parse_category(value: Any) -> CategoryDefinition:
    data = value if isinstance(value, Mapping) else {}
    return CategoryDefinition(
        category_id=data.get("category_id", ""),
        display_name=data.get("display_name", ""),
        description=data.get("description", ""),
        signals=_tuple_values(data.get("signals", ())),
        score_min=data.get("score_min", 0.0),
        score_max=data.get("score_max", 1.0),
        evidence_threshold=data.get("evidence_threshold", 0.5),
        enabled=data.get("enabled", True),
        exposure_level=_enum(ExposureLevel, data.get("exposure_level"), ExposureLevel.AGGREGATE_ONLY),
        threshold_interpretation=data.get(
            "threshold_interpretation",
            "Evidence score must meet or exceed the configured threshold.",
        ),
    )


def _parse_signal(value: Any) -> SignalDefinition:
    data = value if isinstance(value, Mapping) else {}
    return SignalDefinition(
        name=data.get("name", ""),
        source_field_class=_enum(SourceFieldClass, data.get("source_field_class")),
        provenance=_enum(SignalProvenance, data.get("provenance")),
        enabled=data.get("enabled", True),
    )


def _parse_scope(value: Any) -> AnalysisScope | None:
    if value is None:
        return None
    data = value if isinstance(value, Mapping) else {}
    return AnalysisScope(
        accounts=_tuple_values(data.get("accounts", ())),
        regions=_tuple_values(data.get("regions", ())),
        models=_tuple_values(data.get("models", ())),
        operations=_tuple_values(data.get("operations", ())),
        start_date=_date_value(data.get("start_date")),
        end_date=_date_value(data.get("end_date")),
    )


def _parse_prompt_dimensions(value: Any) -> dict[str, PromptDimensionConfiguration]:
    if not isinstance(value, Mapping):
        return {}
    dimensions: dict[str, PromptDimensionConfiguration] = {}
    for name, settings in value.items():
        data = settings if isinstance(settings, Mapping) else {}
        dimensions[str(name)] = PromptDimensionConfiguration(
            enabled=data.get("enabled", False),
            threshold=data.get("threshold", 0.5),
        )
    return dimensions


def _parse_document(document: Mapping[str, Any] | str | bytes) -> InsightConfiguration:
    data = _as_mapping(document)
    categories_value = data.get("categories")
    categories = (
        tuple(_parse_category(item) for item in categories_value)
        if isinstance(categories_value, (tuple, list))
        else InsightConfiguration.__dataclass_fields__["categories"].default_factory()
    )
    signals_value = data.get("signal_definitions")
    signal_definitions = (
        tuple(_parse_signal(item) for item in signals_value)
        if isinstance(signals_value, (tuple, list))
        else InsightConfiguration.__dataclass_fields__["signal_definitions"].default_factory()
    )
    content_data = data.get("content_analysis", {})
    content_data = content_data if isinstance(content_data, Mapping) else {}
    privacy_data = data.get("privacy", {})
    privacy_data = privacy_data if isinstance(privacy_data, Mapping) else {}
    dashboard_data = data.get("dashboard_publication", {})
    dashboard_data = dashboard_data if isinstance(dashboard_data, Mapping) else {}
    model_data = data.get("model_assistance", {})
    model_data = model_data if isinstance(model_data, Mapping) else {}
    precedence = data.get("precedence_rules", ())
    if isinstance(precedence, Mapping):
        precedence = tuple(str(name) for name in precedence)
    else:
        precedence = _tuple_values(precedence)

    return InsightConfiguration(
        configuration_version=data.get("configuration_version", ""),
        effective_at=_datetime_value(data.get("effective_at")),
        author_or_service=data.get("author_or_service", ""),
        change_reason=data.get("change_reason", ""),
        analysis_scope=_parse_scope(data.get("analysis_scope")),
        categories=categories,
        signal_definitions=signal_definitions,
        precedence_rules=precedence,
        enabled=data.get("enabled", False),
        source_view=data.get("source_view", InsightConfiguration.__dataclass_fields__["source_view"].default),
        workgroup=data.get("workgroup", InsightConfiguration.__dataclass_fields__["workgroup"].default),
        confidence_thresholds=(
            dict(data["confidence_thresholds"])
            if isinstance(data.get("confidence_thresholds"), Mapping)
            else {"low": 0.0, "medium": 0.5, "high": 0.8}
        ),
        prompt_dimensions=_parse_prompt_dimensions(data.get("prompt_dimensions", {})),
        content_analysis=ContentAnalysisPolicy(
            state=_enum(ContentAnalysisState, content_data.get("state"), ContentAnalysisState.DISABLED),
            approved_accounts=_tuple_values(content_data.get("approved_accounts", ())),
            approved_regions=_tuple_values(content_data.get("approved_regions", ())),
            access_policy_reference=content_data.get("access_policy_reference"),
            retention_days=content_data.get("retention_days"),
            max_preview_length=content_data.get("max_preview_length"),
        ),
        privacy=PrivacyPolicy(
            exclude_raw_content_from_derived_output=privacy_data.get("exclude_raw_content_from_derived_output", True),
            redaction_required=privacy_data.get("redaction_required", True),
            exclusion_rules=_tuple_values(privacy_data.get("exclusion_rules", ())),
            minimum_group_size=privacy_data.get("minimum_group_size", 10),
            qualifying_subject_definition=privacy_data.get(
                "qualifying_subject_definition", "approved_non_sensitive_subject"
            ),
        ),
        dashboard_publication=DashboardPublicationPolicy(
            enabled=dashboard_data.get("enabled", False),
            aggregate_only=dashboard_data.get("aggregate_only", True),
            approved_role_reference=dashboard_data.get("approved_role_reference"),
        ),
        model_assistance=ModelAssistancePolicy(
            enabled=model_data.get("enabled", False),
            endpoint_reference=model_data.get("endpoint_reference"),
            model_id=model_data.get("model_id"),
            model_version=model_data.get("model_version"),
        ),
        retention_days=data.get("retention_days", 180),
        analysis_logic_version=data.get("analysis_logic_version", "1.0"),
        schema_version=data.get("schema_version", "1.0"),
    )


def _explicit_content_state_issues(document: Mapping[str, Any] | str | bytes) -> tuple[ValidationIssue, ...]:
    data = _as_mapping(document)
    content = data.get("content_analysis")
    if not isinstance(content, Mapping) or "state" not in content:
        return (ValidationIssue("content_analysis.state", "must be explicitly provided as disabled or enabled"),)
    return ()


def parse_configuration(
    document: Mapping[str, Any] | str | bytes,
    *,
    validate: bool = True,
) -> InsightConfiguration:
    """Parse a mapping or JSON document into the canonical configuration.

    Validation is enabled by default.  The store uses ``validate=False`` so a
    candidate can omit server-assigned version/timestamp metadata before
    acceptance.
    """

    configuration = _parse_document(document)
    if validate:
        issues = (*_explicit_content_state_issues(document), *ConfigurationSchemaOracle.validate(configuration))
        if issues:
            raise SchemaValidationError(issues)
    return configuration


ConfigurationCandidate = InsightConfiguration | Mapping[str, Any] | str | bytes
ApprovalValidator = Callable[[InsightConfiguration], Iterable[ValidationIssue]]
Clock = Callable[[], datetime]


class ConfigurationStore(Protocol):
    """Port for configuration validation, versioning, and activation lookup."""

    def validate(self, candidate: ConfigurationCandidate) -> tuple[ValidationIssue, ...]:
        """Return all validation issues without changing active state."""
        ...

    def accept(
        self,
        candidate: ConfigurationCandidate,
        *,
        scope_key: str | None = None,
    ) -> InsightConfiguration:
        """Persist and activate a validated immutable configuration version."""
        ...

    def get(self, configuration_version: str) -> InsightConfiguration:
        """Return the exact configuration associated with a result or run."""
        ...

    def active(self, scope_key: str) -> InsightConfiguration | None:
        """Resolve an approved configuration for a scope, if one is active."""
        ...


def _scope_key(scope: AnalysisScope) -> str:
    return json.dumps(scope.to_dict(), sort_keys=True, separators=(",", ":"))


class InMemoryConfigurationStore:
    """Atomic, immutable configuration registry for pure tests and local use."""

    def __init__(
        self,
        *,
        approval_validator: ApprovalValidator | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._approval_validator = approval_validator
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._versions: dict[str, InsightConfiguration] = {}
        self._active_versions: dict[str, str] = {}
        self._audits: dict[str, ConfigurationAuditRecord] = {}
        self._lock = RLock()

    def _candidate(self, candidate: InsightConfiguration | Mapping[str, Any] | str | bytes) -> InsightConfiguration:
        if isinstance(candidate, InsightConfiguration):
            return candidate
        return parse_configuration(candidate, validate=False)

    def validate(
        self,
        candidate: InsightConfiguration | Mapping[str, Any] | str | bytes,
    ) -> tuple[ValidationIssue, ...]:
        try:
            configuration = self._candidate(candidate)
        except SchemaValidationError as error:
            return error.issues
        explicit_issues = () if isinstance(candidate, InsightConfiguration) else _explicit_content_state_issues(candidate)
        return (*explicit_issues, *ConfigurationSchemaOracle.validate(configuration))

    def _approval_issues(self, configuration: InsightConfiguration) -> tuple[ValidationIssue, ...]:
        if self._approval_validator is None:
            return ()
        outcome = self._approval_validator(configuration)
        if outcome is True or outcome is None:
            return ()
        if outcome is False:
            return (ValidationIssue("approval", "configuration approval was denied"),)
        return tuple(outcome)

    def _effective_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _new_version(now: datetime) -> str:
        return f"config-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:12]}"

    def accept(
        self,
        candidate: InsightConfiguration | Mapping[str, Any] | str | bytes,
        *,
        scope_key: str | None = None,
    ) -> InsightConfiguration:
        """Validate, approve, version, audit, and activate atomically."""

        with self._lock:
            configuration = self._candidate(candidate)
            now = self._effective_now()
            accepted = replace(
                configuration,
                configuration_version=self._new_version(now),
                effective_at=now,
            )
            explicit_issues = () if isinstance(candidate, InsightConfiguration) else _explicit_content_state_issues(candidate)
            issues = (*explicit_issues, *ConfigurationSchemaOracle.validate(accepted))
            issues = (*issues, *self._approval_issues(accepted))
            if issues:
                raise SchemaValidationError(issues)
            if accepted.analysis_scope is None:
                raise SchemaValidationError((ValidationIssue("analysis_scope", "an approved scope is required"),))
            resolved_scope_key = scope_key or _scope_key(accepted.analysis_scope)
            if not isinstance(resolved_scope_key, str) or not resolved_scope_key.strip():
                raise SchemaValidationError((ValidationIssue("scope_key", "must be non-empty"),))

            stored = _freeze_configuration(accepted)
            audit = ConfigurationAuditRecord(
                configuration_version=stored.configuration_version,
                effective_at=stored.effective_at,
                author_or_service=stored.author_or_service,
                change_reason=stored.change_reason,
                enabled_categories=tuple(
                    category.category_id for category in stored.categories if category.enabled
                ),
                enabled_prompt_dimensions=tuple(
                    name for name, settings in stored.prompt_dimensions.items() if settings.enabled
                ),
                privacy_settings=stored.privacy,
                analysis_scope=stored.analysis_scope,
            )
            # All state mutation is after validation, approval, and audit construction.
            self._versions[stored.configuration_version] = stored
            self._audits[stored.configuration_version] = audit
            self._active_versions[resolved_scope_key] = stored.configuration_version
            return stored

    def get(self, configuration_version: str) -> InsightConfiguration:
        with self._lock:
            try:
                return self._versions[configuration_version]
            except KeyError as error:
                raise KeyError(f"unknown configuration version: {configuration_version}") from error

    def active(self, scope_key: str | AnalysisScope) -> InsightConfiguration | None:
        if isinstance(scope_key, AnalysisScope):
            scope_key = _scope_key(scope_key)
        with self._lock:
            version = self._active_versions.get(scope_key)
            return self._versions.get(version) if version else None

    def audit(self, configuration_version: str) -> ConfigurationAuditRecord:
        with self._lock:
            try:
                return self._audits[configuration_version]
            except KeyError as error:
                raise KeyError(f"unknown configuration version: {configuration_version}") from error

    @property
    def versions(self) -> tuple[InsightConfiguration, ...]:
        with self._lock:
            return tuple(self._versions.values())

    @property
    def audits(self) -> tuple[ConfigurationAuditRecord, ...]:
        with self._lock:
            return tuple(self._audits.values())


__all__ = [
    "ApprovalValidator",
    "ConfigurationCandidate",
    "ConfigurationSchemaOracle",
    "ConfigurationStore",
    "FrozenDict",
    "InMemoryConfigurationStore",
    "SchemaValidationError",
    "ValidationIssue",
    "parse_configuration",
]
