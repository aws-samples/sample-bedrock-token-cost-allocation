"""Privacy, authorization, and redaction boundary for sensitive content.

The privacy gate is deliberately independent from the signal extractor and the
persistence layer.  Callers pass the returned ``record`` to non-content
analysis, and pass ``content`` only to an explicitly authorized content
adapter.  A denied content decision never carries the original content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Protocol

from .models import (
    ContentAnalysisState,
    InsightConfiguration,
    InvocationRecord,
    PromptAssessmentStatus,
    PromptDimension,
    PromptQualityAssessment,
    PromptQualityDimensionResult,
    RedactionStatus,
    SensitiveContent,
)
from .oracle import ConfigurationSchemaOracle

MAX_PRIVACY_REASON_LENGTH = 160
_FAILURE_REASONS = {
    "REDACTION_FAILED": "redaction_failed",
    "EXCLUSION_FAILED": "content_excluded",
    "CONTENT_POLICY_BLOCKED": "content_policy_blocked",
}


class ContentAccessState(str, Enum):
    """Machine-readable state of the content portion of a record."""

    AVAILABLE = "available"
    DISABLED = "content_disabled"
    ABSENT = "content_unavailable"
    REDACTED = "content_redacted"
    MALFORMED = "content_malformed"
    EXPIRED = "content_expired"
    EXCLUDED = "content_excluded"
    UNAUTHORIZED = "content_policy_blocked"
    REDACTION_FAILED = "redaction_failed"


@dataclass(frozen=True)
class PrivacyBoundary:
    """Runtime approvals used in addition to the immutable configuration.

    ``approved_access_policies`` is optional so a configuration's non-empty
    policy reference can act as the approval when the caller has no external
    policy registry.  When a registry is supplied, the reference must be a
    member of it.  This lets production callers fail closed while keeping the
    pure in-memory gate usable in tests and local analysis.
    """

    now: datetime | None = None
    approved_access_policies: frozenset[str] | None = None


@dataclass(frozen=True)
class RedactionFailure:
    """Bounded, content-free representation of a redaction failure."""

    code: str = "REDACTION_FAILED"
    detail: str = "configured redaction could not be completed"

    def safe_detail(self) -> str:
        return _FAILURE_REASONS.get(self.code, "redaction_failed")


@dataclass(frozen=True)
class RedactionResult:
    """Result of applying redaction and exclusion rules."""

    content: SensitiveContent | None = None
    failure: RedactionFailure | None = None
    excluded: bool = False


class ContentRedactor(Protocol):
    """Port for the operation that must run before content is exposed."""

    def redact(
        self,
        content: SensitiveContent,
        configuration: InsightConfiguration,
    ) -> RedactionResult:
        """Return sanitized content or a content-free failure."""
        ...


@dataclass(frozen=True)
class PrivacyDecision:
    """Failure-closed authorization result.

    ``record`` is always stripped of ``sensitive_content`` when a normalized
    record is available.  It is safe to use for independent non-content
    signals even when ``allowed`` is false.  ``content`` is present only after
    all boundary checks and redaction have succeeded.
    """

    allowed: bool
    content: SensitiveContent | None = None
    reason: str | None = None
    redaction_event: bool = False
    record: InvocationRecord | None = None
    content_state: ContentAccessState = ContentAccessState.ABSENT

    @property
    def non_content_record(self) -> InvocationRecord | None:
        """Return the privacy-filtered record for independent analysis."""

        return self.record

    @property
    def content_blocked(self) -> bool:
        """Whether content-dependent analysis and persistence must be blocked."""

        return not self.allowed or self.content is None


class PrivacyGate(Protocol):
    """Port that runs before content analysis, diagnostics, and persistence."""

    def authorize(
        self,
        record: InvocationRecord,
        configuration: InsightConfiguration,
    ) -> PrivacyDecision:
        """Authorize content only within the configured boundary."""
        ...


class RuleBasedRedactor:
    """Small deterministic redactor for configured, in-memory rules.

    Rules equal to ``prompt_preview`` or ``response_preview`` exclude that
    field.  Other rules are treated as regular expressions and replaced with a
    fixed marker.  Invalid expressions fail closed.  No exception detail ever
    includes the input content.
    """

    _REDACTION_MARKER = "[REDACTED]"
    _FIELD_NAMES = frozenset({"prompt_preview", "response_preview"})

    def redact(
        self,
        content: SensitiveContent,
        configuration: InsightConfiguration,
    ) -> RedactionResult:
        prompt = content.prompt_preview
        response = content.response_preview
        rules = configuration.privacy.exclusion_rules
        try:
            for rule in rules:
                if rule in self._FIELD_NAMES:
                    if rule == "prompt_preview":
                        prompt = None
                    else:
                        response = None
                    continue
                pattern = re.compile(rule)
                if prompt is not None:
                    prompt = pattern.sub(self._REDACTION_MARKER, prompt)
                if response is not None:
                    response = pattern.sub(self._REDACTION_MARKER, response)
        except (re.error, TypeError):
            return RedactionResult(
                failure=RedactionFailure(detail="configured exclusion rule is invalid")
            )

        max_length = configuration.content_analysis.max_preview_length
        if max_length is not None:
            prompt = _truncate(prompt, max_length)
            response = _truncate(response, max_length)

        if prompt is None and response is None:
            return RedactionResult(excluded=True)
        return RedactionResult(
            content=SensitiveContent(
                prompt_preview=prompt,
                response_preview=response,
                redaction_status=RedactionStatus.REDACTED,
            )
        )


RedactorFactory = Callable[[], ContentRedactor]


class DefaultPrivacyGate:
    """Authorize, redact, and isolate sensitive content before downstream use."""

    def __init__(
        self,
        redactor: ContentRedactor | None = None,
        *,
        boundary: PrivacyBoundary | None = None,
    ) -> None:
        self._redactor = redactor or RuleBasedRedactor()
        self._boundary = boundary or PrivacyBoundary()

    def authorize(
        self,
        record: InvocationRecord,
        configuration: InsightConfiguration,
    ) -> PrivacyDecision:
        """Return sanitized non-content metadata and, only if safe, content.

        Every rejection returns the sanitized record where possible.  This is
        what allows token, model, operation, and other independently approved
        signals to continue while content features, model calls, diagnostics,
        persistence, and dashboard handoff remain blocked.
        """

        safe_record = _without_sensitive_content(record)
        invalid_configuration = ConfigurationSchemaOracle.validate(configuration)
        if invalid_configuration:
            return self._denied(
                safe_record,
                ContentAccessState.UNAUTHORIZED,
                "configuration_invalid",
            )
        if not configuration.enabled:
            return self._denied(
                safe_record,
                ContentAccessState.DISABLED,
                "feature_disabled",
            )
        content_policy = configuration.content_analysis
        if content_policy.state is not ContentAnalysisState.ENABLED:
            return self._denied(
                safe_record,
                ContentAccessState.DISABLED,
                "content_disabled",
            )
        if record.source_account not in content_policy.approved_accounts:
            return self._denied(
                safe_record,
                ContentAccessState.UNAUTHORIZED,
                "account_not_approved",
            )
        if record.source_region not in content_policy.approved_regions:
            return self._denied(
                safe_record,
                ContentAccessState.UNAUTHORIZED,
                "region_not_approved",
            )
        policy_reference = content_policy.access_policy_reference
        approved_policies = self._boundary.approved_access_policies
        if (
            not policy_reference
            or approved_policies is not None
            and policy_reference not in approved_policies
        ):
            return self._denied(
                safe_record,
                ContentAccessState.UNAUTHORIZED,
                "access_policy_not_approved",
            )
        if _is_expired(record, content_policy.retention_days, self._boundary.now):
            return self._denied(
                safe_record,
                ContentAccessState.EXPIRED,
                "content_expired",
            )

        content = record.sensitive_content
        content_error = _content_error(content)
        if content_error is not None:
            return self._denied(safe_record, content_error[0], content_error[1])
        assert content is not None
        if not configuration.privacy.redaction_required:
            return self._denied(
                safe_record,
                ContentAccessState.REDACTION_FAILED,
                "redaction_required",
                redaction_event=True,
            )

        try:
            redaction = self._redactor.redact(content, configuration)
        except Exception:
            redaction = RedactionResult(
                failure=RedactionFailure(detail="redaction operation failed")
            )
        if redaction.failure is not None:
            return self._denied(
                safe_record,
                ContentAccessState.REDACTION_FAILED,
                redaction.failure.safe_detail(),
                redaction_event=True,
            )
        if redaction.excluded or redaction.content is None:
            return self._denied(
                safe_record,
                ContentAccessState.EXCLUDED,
                "content_excluded",
            )
        if redaction.content.redaction_status is not RedactionStatus.REDACTED:
            return self._denied(
                safe_record,
                ContentAccessState.REDACTION_FAILED,
                "redaction_status_not_confirmed",
                redaction_event=True,
            )
        output_error = _content_error(redaction.content)
        if output_error is not None:
            return self._denied(safe_record, output_error[0], output_error[1])
        return PrivacyDecision(
            allowed=True,
            content=redaction.content,
            record=safe_record,
            content_state=ContentAccessState.AVAILABLE,
        )

    @staticmethod
    def _denied(
        record: InvocationRecord,
        state: ContentAccessState,
        reason: str,
        *,
        redaction_event: bool = False,
    ) -> PrivacyDecision:
        return PrivacyDecision(
            allowed=False,
            content=None,
            reason=_safe_reason(reason),
            redaction_event=redaction_event,
            record=record,
            content_state=state,
        )


def not_assessed_prompt_quality(
    configuration: InsightConfiguration,
    reason: str,
) -> PromptQualityAssessment:
    """Build a content-safe assessment for unavailable content.

    All configured dimensions are represented, including disabled dimensions,
    so downstream consumers cannot mistake an omitted dimension for an
    assessed one.  There is no score, confidence, or evidence on these rows.
    """

    safe_reason = _safe_reason(reason)
    dimensions = {
        dimension.value: PromptQualityDimensionResult(
            dimension=dimension.value,
            status=PromptAssessmentStatus.NOT_ASSESSED,
            not_assessed_reason=safe_reason,
        )
        for dimension in PromptDimension
    }
    return PromptQualityAssessment(
        dimensions=dimensions,
        content_state=configuration.content_analysis.state,
    )


def _without_sensitive_content(record: InvocationRecord) -> InvocationRecord:
    return replace(record, sensitive_content=None)


def _content_error(
    content: SensitiveContent | None,
) -> tuple[ContentAccessState, str] | None:
    if content is None:
        return ContentAccessState.ABSENT, "content_unavailable"
    if not isinstance(content, SensitiveContent):
        return ContentAccessState.MALFORMED, "content_malformed"
    if content.redaction_status is RedactionStatus.FAILED:
        return ContentAccessState.REDACTION_FAILED, "redaction_failed"
    if not isinstance(content.prompt_preview, (str, type(None))) or not isinstance(
        content.response_preview, (str, type(None))
    ):
        return ContentAccessState.MALFORMED, "content_malformed"
    if not content.prompt_preview and not content.response_preview:
        if content.redaction_status is RedactionStatus.REDACTED:
            return ContentAccessState.REDACTED, "content_redacted"
        return ContentAccessState.ABSENT, "content_unavailable"
    return None


def _is_expired(
    record: InvocationRecord,
    retention_days: int | None,
    now: datetime | None,
) -> bool:
    if retention_days is None or record.source_timestamp_utc is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    timestamp = record.source_timestamp_utc.astimezone(timezone.utc)
    return current - timestamp > timedelta(days=retention_days)


def _truncate(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    return value[:max_length]


def _safe_reason(value: str) -> str:
    return " ".join(str(value).split())[:MAX_PRIVACY_REASON_LENGTH]


__all__ = [
    "ContentAccessState",
    "ContentRedactor",
    "DefaultPrivacyGate",
    "MAX_PRIVACY_REASON_LENGTH",
    "PrivacyBoundary",
    "PrivacyDecision",
    "PrivacyGate",
    "RedactionFailure",
    "RedactionResult",
    "RuleBasedRedactor",
    "not_assessed_prompt_quality",
]
