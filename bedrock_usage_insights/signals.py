"""Configured, privacy-aware extraction of canonical usage signals.

The extractor is deliberately pure. Privacy authorization and redaction happen
before it is called; this module still fails closed for content that does not
carry the required redaction state and never turns restricted identity metadata
into a feature implicitly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import (
    CONTENT_SIGNAL_NAMES,
    ContentAnalysisState,
    InsightConfiguration,
    InvocationRecord,
    RedactionStatus,
    Signal,
    SignalAvailability,
    SignalDefinition,
    SignalProvenance,
)

# These limits apply to values emitted by the extractor, not to source rows.
# They keep a Signal bounded even when a source adapter receives malformed data.
MAX_SIGNAL_STRING_LENGTH = 256
MAX_METADATA_ITEMS = 32
MAX_METADATA_KEY_LENGTH = 64
MAX_METADATA_VALUE_LENGTH = 256
MAX_TOKEN_COUNT = 1_000_000_000

_METADATA_KEY_BLOCKLIST = re.compile(
    r"(?:prompt|response|secret|password|credential|authorization|body|content)",
    re.IGNORECASE,
)
_TASK_MARKER = re.compile(
    r"\b(?:task|todo|objective|goal|please|create|write|analy[sz]e|summari[sz]e|"
    r"explain|generate|fix|implement|compare|review)\b",
    re.IGNORECASE,
)
_OUTPUT_CONSTRAINT_MARKER = re.compile(
    r"(?:return\s+only|format\s+as|output\s+(?:as|in)|json|yaml|xml|"
    r"table|bullet(?:s)?|must\s+include|maximum|limit)",
    re.IGNORECASE,
)
_CONTEXT_MARKER = re.compile(
    r"(?:context|background|given|assume|requirements?|constraints?|input|here\s+is)",
    re.IGNORECASE,
)
_SPECIFICITY_MARKER = re.compile(
    r"(?:specifically|using|with|include|exclude|must|should|when|where|if)",
    re.IGNORECASE,
)

_DIRECT_VALUE_TYPES = {
    "operation": "string",
    "model_id": "string",
    "input_token_count": "integer",
    "output_token_count": "integer",
    "cache_read_token_count": "integer",
    "cache_write_token_count": "integer",
    "stop_reason": "string",
    "content_type": "string",
    "request_metadata": "mapping",
    "model_format": "string",
    "identity_arn": "string",
    "identity_session_name": "string",
    "identity_name": "string",
    "account_owner": "string",
}
_CONTENT_VALUE_TYPES = {name: "boolean" for name in CONTENT_SIGNAL_NAMES}
_VALUE_TYPES = {**_DIRECT_VALUE_TYPES, **_CONTENT_VALUE_TYPES}


@dataclass(frozen=True)
class SignalSet:
    """Stable ordered signal collection returned by :class:`SignalExtractor`."""

    signals: tuple[Signal, ...]

    def __iter__(self):
        return iter(self.signals)

    def __len__(self) -> int:
        return len(self.signals)

    def __getitem__(self, index: int) -> Signal:
        return self.signals[index]

    def as_tuple(self) -> tuple[Signal, ...]:
        return self.signals


class SignalExtractor:
    """Extract only enabled, category-allowlisted Signals from one record."""

    def extract(
        self,
        record: InvocationRecord,
        configuration: InsightConfiguration,
    ) -> tuple[Signal, ...]:
        if not configuration.enabled:
            return ()

        definitions = self._active_definitions(configuration)
        return tuple(
            self._extract_one(record, configuration, definition)
            for definition in definitions
        )

    def extract_set(
        self,
        record: InvocationRecord,
        configuration: InsightConfiguration,
    ) -> SignalSet:
        return SignalSet(self.extract(record, configuration))

    @staticmethod
    def _active_definitions(
        configuration: InsightConfiguration,
    ) -> tuple[SignalDefinition, ...]:
        """Resolve the active Signal allowlist without adding implicit fields."""

        allowed_names: set[str] = set()
        for category in configuration.categories:
            if category.enabled:
                allowed_names.update(category.signals)

        # PromptDimensionConfiguration is intentionally small in the canonical
        # model.  Supporting an optional ``signals`` attribute keeps the
        # extractor compatible with a richer configuration without making
        # content signals active by default.
        for dimension in configuration.prompt_dimensions.values():
            dimension_signals = getattr(dimension, "signals", ())
            if isinstance(dimension_signals, Sequence) and not isinstance(
                dimension_signals, (str, bytes)
            ):
                allowed_names.update(
                    name for name in dimension_signals if isinstance(name, str)
                )

        definitions_by_name: dict[str, SignalDefinition] = {}
        for definition in configuration.signal_definitions:
            if (
                definition.enabled
                and definition.name in allowed_names
                and definition.name in _VALUE_TYPES
                and definition.name not in definitions_by_name
            ):
                definitions_by_name[definition.name] = definition
        return tuple(definitions_by_name[name] for name in sorted(definitions_by_name))

    def _extract_one(
        self,
        record: InvocationRecord,
        configuration: InsightConfiguration,
        definition: SignalDefinition,
    ) -> Signal:
        name = definition.name
        if name in CONTENT_SIGNAL_NAMES:
            value, availability, summary = self._content_value(
                record, configuration, name
            )
        else:
            value, availability, summary = self._direct_value(record, name)

        return Signal(
            name=name,
            value_type=_VALUE_TYPES[name],
            value=value,
            source_field_class=definition.source_field_class,
            provenance=definition.provenance,
            availability=availability,
            summary=summary,
        )

    @staticmethod
    def _direct_value(
        record: InvocationRecord,
        name: str,
    ) -> tuple[Any, SignalAvailability, str]:
        if name == "request_metadata":
            value = SignalExtractor._bounded_metadata(record.request_metadata)
            if value is None:
                return None, SignalAvailability.MALFORMED, "Request metadata was malformed."
            if not value:
                return None, SignalAvailability.UNAVAILABLE, "Request metadata was unavailable."
            return value, SignalAvailability.AVAILABLE, "Request metadata was observed."

        if name.startswith("identity_") or name == "account_owner":
            # IdentityMetadata is restricted and is reached only when the
            # active category explicitly allowlists this exact Signal name.
            identity = record.identity_metadata
            if identity is None:
                return None, SignalAvailability.UNAVAILABLE, "Identity metadata was unavailable."
            attribute = {
                "identity_arn": "identity_arn",
                "identity_session_name": "session_name",
                "identity_name": "name",
                "account_owner": "account_owner",
            }[name]
            value = getattr(identity, attribute, None)
        else:
            value = getattr(record, name, None)

        if value is None:
            return None, SignalAvailability.UNAVAILABLE, f"{name} was unavailable."
        if name.endswith("token_count"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None, SignalAvailability.MALFORMED, f"{name} was malformed."
            return min(value, MAX_TOKEN_COUNT), SignalAvailability.AVAILABLE, f"{name} was observed."
        if isinstance(value, str):
            return (
                _bounded_string(value),
                SignalAvailability.AVAILABLE,
                f"{name} was observed.",
            )
        return None, SignalAvailability.MALFORMED, f"{name} was malformed."

    @staticmethod
    def _bounded_metadata(value: Any) -> dict[str, str] | None:
        if not isinstance(value, Mapping):
            return None
        bounded: dict[str, str] = {}
        for key in sorted(value, key=lambda item: str(item)):
            if len(bounded) >= MAX_METADATA_ITEMS:
                break
            if not isinstance(key, str):
                return None
            if not key.strip() or _METADATA_KEY_BLOCKLIST.search(key):
                continue
            item = value[key]
            if not isinstance(item, str):
                return None
            bounded[_bounded_string(key, MAX_METADATA_KEY_LENGTH)] = _bounded_string(
                item, MAX_METADATA_VALUE_LENGTH
            )
        return bounded

    @staticmethod
    def _content_value(
        record: InvocationRecord,
        configuration: InsightConfiguration,
        name: str,
    ) -> tuple[Any, SignalAvailability, str]:
        if configuration.content_analysis.state is not ContentAnalysisState.ENABLED:
            return None, SignalAvailability.UNAVAILABLE, "Content analysis was disabled."

        content = record.sensitive_content
        if content is None or content.prompt_preview is None:
            return None, SignalAvailability.UNAVAILABLE, "Prompt content was unavailable."

        if configuration.privacy.redaction_required:
            if content.redaction_status is RedactionStatus.FAILED:
                return None, SignalAvailability.REDACTED, "Prompt content was blocked after redaction failure."
            if content.redaction_status is not RedactionStatus.REDACTED:
                return None, SignalAvailability.REDACTED, "Prompt content was not approved after redaction."

        if not isinstance(content.prompt_preview, str):
            return None, SignalAvailability.MALFORMED, "Prompt content was malformed."
        max_length = configuration.content_analysis.max_preview_length
        if max_length is not None and len(content.prompt_preview) > max_length:
            return None, SignalAvailability.MALFORMED, "Prompt content exceeded the configured bound."

        prompt = content.prompt_preview
        marker = {
            "prompt_task_marker": _TASK_MARKER.search(prompt),
            "prompt_output_constraint_marker": _OUTPUT_CONSTRAINT_MARKER.search(prompt),
            "prompt_context_marker": _CONTEXT_MARKER.search(prompt),
            "prompt_specificity_marker": (
                len(prompt.strip()) >= 20 and _SPECIFICITY_MARKER.search(prompt)
            ),
        }[name]
        return bool(marker), SignalAvailability.AVAILABLE, f"{name} was derived from permitted prompt content."


def _bounded_string(value: str, limit: int = MAX_SIGNAL_STRING_LENGTH) -> str:
    value = value.strip()
    return value[:limit]


def extract_signals(
    record: InvocationRecord,
    configuration: InsightConfiguration,
) -> tuple[Signal, ...]:
    """Functional entry point for configured Signal extraction."""

    return SignalExtractor().extract(record, configuration)


__all__ = [
    "MAX_METADATA_ITEMS",
    "MAX_METADATA_KEY_LENGTH",
    "MAX_METADATA_VALUE_LENGTH",
    "MAX_SIGNAL_STRING_LENGTH",
    "MAX_TOKEN_COUNT",
    "SignalExtractor",
    "SignalSet",
    "extract_signals",
]
