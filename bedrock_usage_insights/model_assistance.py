"""Fail-closed, advisory model-assisted feature extraction.

The adapter is intentionally a narrow boundary. It accepts only content that a
privacy gate marked as redacted, sends a minimized request to an approved
client, and returns validated feature proposals. It has no persistence or
publication interface, and its response schema has no category field.
"""
from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from .models import (
    CONTENT_SIGNAL_NAMES,
    EvidenceReference,
    ModelAssistancePolicy,
    RedactionStatus,
    SensitiveContent,
    SourceFieldClass,
)

MAX_FEATURE_NAME_LENGTH = 100
MAX_EVIDENCE_SUMMARY_LENGTH = 160
MAX_FEATURES_PER_RESPONSE = 32
MAX_EVIDENCE_ITEMS = 32
MAX_EVIDENCE_COUNT = 1_000_000
_SAFE_ERROR_LENGTH = 160
_ALLOWED_RESPONSE_KEYS = frozenset({"schema_version", "model_id", "model_version", "features"})
_ALLOWED_FEATURE_KEYS = frozenset({"name", "value", "evidence"})
_ALLOWED_EVIDENCE_KEYS = frozenset(
    {"signal_names", "source_field_classes", "summaries", "counts"}
)
_SENSITIVE_KEY = re.compile(
    r"(?:prompt|response|secret|password|credential|authorization|identity|session)",
    re.IGNORECASE,
)


class ModelErrorCode(str, Enum):
    """Stable content-free outcomes for an unavailable model proposal."""

    DISABLED = "model_assistance_disabled"
    APPROVAL_REQUIRED = "model_approval_required"
    CONTENT_REDACTED = "content_redacted"
    POLICY_INVALID = "model_policy_invalid"
    ENDPOINT_REJECTED = "endpoint_rejected"
    TIMEOUT = "model_timeout"
    RETRY_EXHAUSTED = "model_retry_exhausted"
    UNSUPPORTED_MODEL_VERSION = "unsupported_model_version"
    INVALID_RESPONSE = "invalid_model_response"
    CLIENT_ERROR = "model_client_error"


@dataclass(frozen=True)
class ProposedFeature:
    """One bounded content-derived proposal, not an active Signal or category."""

    name: str
    value: bool | float
    evidence_reference: EvidenceReference

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "evidence_reference": self.evidence_reference.to_dict(),
        }


@dataclass(frozen=True)
class ProposedFeatures:
    """Validated advisory output with reproducibility metadata only."""

    model_id: str
    model_version: str
    response_schema_version: str
    features: tuple[ProposedFeature, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "response_schema_version": self.response_schema_version,
            "features": tuple(feature.to_dict() for feature in self.features),
        }


@dataclass(frozen=True)
class ModelError:
    """Bounded, content-free model failure returned to the deterministic path."""

    code: ModelErrorCode
    detail: str
    retryable: bool = False
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "detail": _safe_detail(self.detail),
            "retryable": self.retryable,
            "attempts": self.attempts,
        }


class ModelFeatureClient(Protocol):
    """Client contract; the client owns endpoint authentication and transport."""

    def invoke(
        self,
        request: Mapping[str, Any],
        *,
        endpoint_reference: str,
        model_id: str,
        model_version: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class _MinimizedRequest:
    schema_version: str
    model_id: str
    model_version: str
    content: Mapping[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "content": dict(self.content),
        }


class ModelAssistedFeatureAdapter:
    """Invoke an approved model for bounded, content-derived proposals only."""

    def __init__(
        self,
        client: ModelFeatureClient,
        *,
        approved_endpoint_references: frozenset[str] | set[str] | tuple[str, ...] = (),
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._approved_endpoints = frozenset(approved_endpoint_references)
        self._sleep = sleeper

    def extract_features(
        self,
        redacted_content: SensitiveContent,
        model_policy: ModelAssistancePolicy,
    ) -> ProposedFeatures | ModelError:
        """Return proposals or a safe failure; never returns a publication result."""

        policy_error = self._validate_policy(model_policy)
        if policy_error is not None:
            return policy_error
        if not model_policy.enabled:
            return ModelError(ModelErrorCode.DISABLED, "model assistance is disabled")
        if not model_policy.approved:
            return ModelError(ModelErrorCode.APPROVAL_REQUIRED, "model assistance approval is required")
        assert model_policy.endpoint_reference is not None
        assert model_policy.model_id is not None
        assert model_policy.model_version is not None
        if (
            self._approved_endpoints
            and model_policy.endpoint_reference not in self._approved_endpoints
        ):
            return ModelError(ModelErrorCode.ENDPOINT_REJECTED, "model endpoint is not approved")
        content_error = self._validate_content(redacted_content)
        if content_error is not None:
            return content_error

        request = self._build_request(redacted_content, model_policy)
        last_error: ModelError | None = None
        for attempt in range(1, model_policy.max_attempts + 1):
            try:
                raw_response = self._client.invoke(
                    request.as_dict(),
                    endpoint_reference=model_policy.endpoint_reference,
                    model_id=model_policy.model_id,
                    model_version=model_policy.model_version,
                    timeout_seconds=model_policy.timeout_seconds,
                )
            except TimeoutError:
                last_error = ModelError(
                    ModelErrorCode.TIMEOUT,
                    "model request timed out",
                    retryable=True,
                    attempts=attempt,
                )
            except (ConnectionError, OSError):
                last_error = ModelError(
                    ModelErrorCode.CLIENT_ERROR,
                    "model endpoint was unavailable",
                    retryable=True,
                    attempts=attempt,
                )
            except Exception:
                last_error = ModelError(
                    ModelErrorCode.CLIENT_ERROR,
                    "model endpoint request failed",
                    retryable=False,
                    attempts=attempt,
                )
            else:
                validated = self._validate_response(
                    raw_response,
                    model_policy,
                    redacted_content,
                )
                if isinstance(validated, ProposedFeatures):
                    return validated
                return validated
            if attempt < model_policy.max_attempts:
                self._sleep(min(1.0, 0.05 * (2 ** (attempt - 1))))

        if last_error is None:
            return ModelError(ModelErrorCode.RETRY_EXHAUSTED, "model request failed")
        return ModelError(
            ModelErrorCode.RETRY_EXHAUSTED,
            last_error.detail,
            retryable=False,
            attempts=last_error.attempts,
        )

    @staticmethod
    def _validate_policy(policy: ModelAssistancePolicy) -> ModelError | None:
        if not isinstance(policy, ModelAssistancePolicy):
            return ModelError(ModelErrorCode.POLICY_INVALID, "model policy has an invalid schema")
        if not policy.enabled:
            return None
        if not policy.approved:
            return ModelError(ModelErrorCode.APPROVAL_REQUIRED, "model assistance approval is required")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (policy.endpoint_reference, policy.model_id, policy.model_version)
        ):
            return ModelError(ModelErrorCode.POLICY_INVALID, "model endpoint and version are required")
        if not isinstance(policy.allowed_features, (tuple, list)) or isinstance(policy.allowed_features, str):
            return ModelError(ModelErrorCode.POLICY_INVALID, "allowed model features must be a sequence")
        if any(not isinstance(name, str) or not name.strip() for name in policy.allowed_features):
            return ModelError(ModelErrorCode.POLICY_INVALID, "allowed model features must be named")
        if not policy.allowed_features or len(set(policy.allowed_features)) != len(policy.allowed_features):
            return ModelError(ModelErrorCode.POLICY_INVALID, "allowed model features must be unique and non-empty")
        if any(name not in CONTENT_SIGNAL_NAMES for name in policy.allowed_features):
            return ModelError(ModelErrorCode.POLICY_INVALID, "allowed model features must be content-derived")
        if not isinstance(policy.response_schema_version, str) or not policy.response_schema_version.strip():
            return ModelError(ModelErrorCode.POLICY_INVALID, "response schema version is required")
        if not isinstance(policy.max_input_length, int) or isinstance(policy.max_input_length, bool) or policy.max_input_length < 1:
            return ModelError(ModelErrorCode.POLICY_INVALID, "maximum model input length is invalid")
        if not isinstance(policy.timeout_seconds, (int, float)) or isinstance(policy.timeout_seconds, bool) or not math.isfinite(policy.timeout_seconds) or policy.timeout_seconds <= 0:
            return ModelError(ModelErrorCode.POLICY_INVALID, "model timeout is invalid")
        if not isinstance(policy.max_attempts, int) or isinstance(policy.max_attempts, bool) or policy.max_attempts < 1:
            return ModelError(ModelErrorCode.POLICY_INVALID, "model retry bound is invalid")
        return None

    @staticmethod
    def _validate_content(content: SensitiveContent) -> ModelError | None:
        if not isinstance(content, SensitiveContent):
            return ModelError(ModelErrorCode.CONTENT_REDACTED, "content was not authorized")
        if content.redaction_status is not RedactionStatus.REDACTED:
            return ModelError(ModelErrorCode.CONTENT_REDACTED, "content must be redacted before model use")
        if not isinstance(content.prompt_preview, (str, type(None))) or not isinstance(content.response_preview, (str, type(None))):
            return ModelError(ModelErrorCode.CONTENT_REDACTED, "content was malformed")
        if not content.prompt_preview and not content.response_preview:
            return ModelError(ModelErrorCode.CONTENT_REDACTED, "redacted content was unavailable")
        return None

    @staticmethod
    def _build_request(
        content: SensitiveContent,
        policy: ModelAssistancePolicy,
    ) -> _MinimizedRequest:
        values: dict[str, str] = {}
        for name, value in (("prompt", content.prompt_preview), ("response", content.response_preview)):
            if value:
                # Collapse whitespace and bound each field and the aggregate below.
                values[name] = " ".join(value.split())
        remaining = policy.max_input_length
        minimized: dict[str, str] = {}
        for name in ("prompt", "response"):
            if remaining <= 0:
                break
            value = values.get(name)
            if value:
                minimized[name] = value[:remaining]
                remaining -= len(minimized[name])
        return _MinimizedRequest(
            schema_version=policy.response_schema_version,
            model_id=policy.model_id or "",
            model_version=policy.model_version or "",
            content=minimized,
        )

    @classmethod
    def _validate_response(
        cls,
        response: Mapping[str, Any],
        policy: ModelAssistancePolicy,
        content: SensitiveContent,
    ) -> ProposedFeatures | ModelError:
        if not isinstance(response, Mapping):
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model response must be an object")
        if set(response) - _ALLOWED_RESPONSE_KEYS:
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model response contains unsupported fields")
        if response.get("schema_version") != policy.response_schema_version:
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model response schema version was unsupported")
        if response.get("model_id") != policy.model_id or response.get("model_version") != policy.model_version:
            return ModelError(ModelErrorCode.UNSUPPORTED_MODEL_VERSION, "model response version did not match policy")
        raw_features = response.get("features")
        if not isinstance(raw_features, Sequence) or isinstance(raw_features, (str, bytes)) or len(raw_features) > MAX_FEATURES_PER_RESPONSE:
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model features must be a bounded list")
        features: list[ProposedFeature] = []
        seen: set[str] = set()
        for raw_feature in raw_features:
            validated = cls._validate_feature(raw_feature, policy, content)
            if isinstance(validated, ModelError):
                return validated
            if validated.name in seen:
                return ModelError(ModelErrorCode.INVALID_RESPONSE, "model feature names must be unique")
            seen.add(validated.name)
            features.append(validated)
        return ProposedFeatures(
            model_id=policy.model_id or "",
            model_version=policy.model_version or "",
            response_schema_version=policy.response_schema_version,
            features=tuple(features),
        )

    @classmethod
    def _validate_feature(
        cls,
        raw_feature: Any,
        policy: ModelAssistancePolicy,
        content: SensitiveContent,
    ) -> ProposedFeature | ModelError:
        if not isinstance(raw_feature, Mapping) or set(raw_feature) != _ALLOWED_FEATURE_KEYS:
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model feature schema was invalid")
        name = raw_feature.get("name")
        if not isinstance(name, str) or not 0 < len(name) <= MAX_FEATURE_NAME_LENGTH or name not in policy.allowed_features:
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model feature name was not approved")
        value = raw_feature.get("value")
        if isinstance(value, bool):
            bounded_value: bool | float = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0.0 <= value <= 1.0:
            bounded_value = float(value)
        else:
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model feature value was outside its bound")
        evidence = cls._validate_evidence(raw_feature.get("evidence"), policy, content)
        if isinstance(evidence, ModelError):
            return evidence
        return ProposedFeature(name, bounded_value, evidence)

    @classmethod
    def _validate_evidence(
        cls,
        raw_evidence: Any,
        policy: ModelAssistancePolicy,
        content: SensitiveContent,
    ) -> EvidenceReference | ModelError:
        if not isinstance(raw_evidence, Mapping) or set(raw_evidence) - _ALLOWED_EVIDENCE_KEYS:
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model evidence schema was invalid")
        signal_names = raw_evidence.get("signal_names")
        field_classes = raw_evidence.get("source_field_classes")
        summaries = raw_evidence.get("summaries")
        counts = raw_evidence.get("counts", {})
        if not _bounded_strings(signal_names, MAX_EVIDENCE_ITEMS) or not _bounded_strings(field_classes, MAX_EVIDENCE_ITEMS):
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model evidence references were invalid")
        if not signal_names or not field_classes:
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model evidence must identify signals and field classes")
        if any(name not in policy.allowed_features for name in signal_names):
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model evidence referenced an unapproved feature")
        parsed_classes: list[SourceFieldClass] = []
        for value in field_classes:
            try:
                parsed = SourceFieldClass(value)
            except (ValueError, TypeError):
                return ModelError(ModelErrorCode.INVALID_RESPONSE, "model evidence field class was invalid")
            if parsed is not SourceFieldClass.PERMITTED_CONTENT_DERIVED_FEATURE:
                return ModelError(ModelErrorCode.INVALID_RESPONSE, "model evidence was not content-derived")
            parsed_classes.append(parsed)
        if not _bounded_strings(summaries, MAX_EVIDENCE_ITEMS, allow_empty=True):
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model evidence summaries were invalid")
        source_text = " ".join(value for value in (content.prompt_preview, content.response_preview) if value)
        for summary in summaries:
            if _SENSITIVE_KEY.search(summary) or (len(summary) >= 4 and summary.casefold() in source_text.casefold()):
                return ModelError(ModelErrorCode.INVALID_RESPONSE, "model evidence contained sensitive content")
        if not isinstance(counts, Mapping) or len(counts) > MAX_EVIDENCE_ITEMS:
            return ModelError(ModelErrorCode.INVALID_RESPONSE, "model evidence counts were invalid")
        parsed_counts: dict[str, int] = {}
        for name, count in counts.items():
            if name not in signal_names or not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= MAX_EVIDENCE_COUNT:
                return ModelError(ModelErrorCode.INVALID_RESPONSE, "model evidence counts were invalid")
            parsed_counts[name] = count
        return EvidenceReference(
            signal_names=tuple(signal_names),
            source_field_classes=tuple(parsed_classes),
            summaries=tuple(summaries),
            counts=parsed_counts,
        )


def _bounded_strings(value: Any, maximum: int, *, allow_empty: bool = False) -> list[str] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) > maximum:
        return None
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > MAX_EVIDENCE_SUMMARY_LENGTH or (not allow_empty and not item.strip()):
            return None
        result.append(item)
    return result


def _safe_detail(value: Any) -> str:
    return " ".join(str(value).split())[:_SAFE_ERROR_LENGTH]


__all__ = [
    "MAX_FEATURES_PER_RESPONSE",
    "ModelAssistedFeatureAdapter",
    "ModelError",
    "ModelErrorCode",
    "ModelFeatureClient",
    "ProposedFeature",
    "ProposedFeatures",
]
