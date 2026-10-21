"""Normalization boundary between Athena rows and canonical records.

The source view has a stable logical contract but contains model-specific and
nullable fields.  This module keeps the normalization boundary dependency-free:
source rows are converted into :class:`InvocationRecord` values, while format
adapters validate only formats they explicitly support.  No adapter falls back
to another model's JSON shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping, Protocol, Sequence

from .models import IdentityMetadata, InvocationRecord, SensitiveContent

MAX_DIAGNOSTIC_DETAIL_LENGTH = 160

_MISSING = object()


@dataclass(frozen=True)
class NormalizationError:
    """Content-free diagnostic for a row that cannot be fully normalized."""

    code: str
    detail: str
    source_request_reference: str | None = None


@dataclass(frozen=True)
class NormalizationResult:
    """A canonical record plus bounded diagnostics, when a record is possible.

    A record may be returned together with an error for malformed optional
    fields or an unsupported response format.  Downstream policy can preserve
    its valid reconciliation metadata while converting the diagnostic into an
    unknown or low-confidence outcome.  Traceability failures return no record.
    """

    record: InvocationRecord | None = None
    error: NormalizationError | None = None
    errors: tuple[NormalizationError, ...] = ()

    def __post_init__(self) -> None:
        if self.error is not None and not self.errors:
            object.__setattr__(self, "errors", (self.error,))
        elif self.error is None and self.errors:
            object.__setattr__(self, "error", self.errors[0])


@dataclass(frozen=True)
class AdapterResult:
    """Result from an explicit model/format adapter."""

    model_format: str | None = None
    errors: tuple[NormalizationError, ...] = ()


class ModelFormatAdapter(Protocol):
    """Port for a model-specific source/body format adapter."""

    format_name: str

    def supports(
        self,
        model_id: str | None,
        declared_format: str | None,
        source_row: Mapping[str, Any],
    ) -> bool:
        """Return whether this adapter explicitly owns the source format."""
        ...

    def normalize(
        self,
        source_row: Mapping[str, Any],
        *,
        source_request_reference: str | None,
    ) -> AdapterResult:
        """Validate only this adapter's known model/body representation."""
        ...


class _JsonModelFormatAdapter:
    """Base for explicit adapters that validate model-specific JSON bodies."""

    format_name = ""
    aliases: frozenset[str] = frozenset()
    model_prefixes: tuple[str, ...] = ()

    def supports(
        self,
        model_id: str | None,
        declared_format: str | None,
        source_row: Mapping[str, Any],
    ) -> bool:
        del source_row
        normalized_format = _normalize_format(declared_format)
        if normalized_format is not None:
            return normalized_format in self.aliases
        normalized_model = (model_id or "").strip().lower()
        return any(normalized_model.startswith(prefix) for prefix in self.model_prefixes)

    def normalize(
        self,
        source_row: Mapping[str, Any],
        *,
        source_request_reference: str | None,
    ) -> AdapterResult:
        errors: list[NormalizationError] = []
        for field_name in ("input_body_json", "output_body_json"):
            value = _first_present(source_row, _BODY_FIELD_ALIASES[field_name])
            if value is _MISSING or value is None:
                continue
            if isinstance(value, Mapping):
                continue
            if not isinstance(value, str):
                errors.append(
                    _diagnostic(
                        "MALFORMED_MODEL_JSON",
                        f"{field_name} must be a JSON object",
                        source_request_reference,
                    )
                )
                continue
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                errors.append(
                    _diagnostic(
                        "MALFORMED_MODEL_JSON",
                        f"{field_name} is not valid JSON",
                        source_request_reference,
                    )
                )
                continue
            if not isinstance(parsed, Mapping):
                errors.append(
                    _diagnostic(
                        "MALFORMED_MODEL_JSON",
                        f"{field_name} must contain a JSON object",
                        source_request_reference,
                    )
                )
        return AdapterResult(model_format=self.format_name, errors=tuple(errors))


class NovaModelFormatAdapter(_JsonModelFormatAdapter):
    """Adapter for Amazon Nova message/request and response formats."""

    format_name = "nova"
    aliases = frozenset({"nova", "amazon_nova", "amazon.nova"})
    model_prefixes = ("amazon.nova",)


class AnthropicModelFormatAdapter(_JsonModelFormatAdapter):
    """Adapter for Anthropic Claude request and response formats."""

    format_name = "anthropic"
    aliases = frozenset({"anthropic", "claude", "anthropic_claude"})
    model_prefixes = ("anthropic.",)


class TitanModelFormatAdapter(_JsonModelFormatAdapter):
    """Adapter for Amazon Titan request and response formats."""

    format_name = "titan"
    aliases = frozenset({"titan", "amazon_titan", "amazon.titan"})
    model_prefixes = ("amazon.titan",)


class CohereModelFormatAdapter(_JsonModelFormatAdapter):
    """Adapter for Cohere Command request and response formats."""

    format_name = "cohere"
    aliases = frozenset({"cohere", "command", "cohere_command"})
    model_prefixes = ("cohere.",)


class LlamaModelFormatAdapter(_JsonModelFormatAdapter):
    """Adapter for Meta Llama request and response formats."""

    format_name = "llama"
    aliases = frozenset({"llama", "meta_llama", "meta.llama"})
    model_prefixes = ("meta.llama",)


class MistralModelFormatAdapter(_JsonModelFormatAdapter):
    """Adapter for Mistral request and response formats."""

    format_name = "mistral"
    aliases = frozenset({"mistral"})
    model_prefixes = ("mistral.",)


class FlattenedViewFormatAdapter:
    """Adapter for rows with direct flattened fields and no body to interpret.

    This adapter intentionally does not claim rows containing an explicit format
    or raw model body.  Such rows must be handled by a matching model adapter or
    classified as unsupported rather than interpreted using a different schema.
    """

    format_name = "flattened_view"

    def supports(
        self,
        model_id: str | None,
        declared_format: str | None,
        source_row: Mapping[str, Any],
    ) -> bool:
        del model_id
        return (
            declared_format is None
            and _first_present(source_row, _BODY_FIELD_ALIASES["input_body_json"]) is _MISSING
            and _first_present(source_row, _BODY_FIELD_ALIASES["output_body_json"]) is _MISSING
        )

    def normalize(
        self,
        source_row: Mapping[str, Any],
        *,
        source_request_reference: str | None,
    ) -> AdapterResult:
        del source_row, source_request_reference
        return AdapterResult(model_format=None)


def default_model_format_adapters() -> tuple[ModelFormatAdapter, ...]:
    """Return the explicit adapters used by :class:`InvocationRecordNormalizer`."""

    return (
        NovaModelFormatAdapter(),
        AnthropicModelFormatAdapter(),
        TitanModelFormatAdapter(),
        CohereModelFormatAdapter(),
        LlamaModelFormatAdapter(),
        MistralModelFormatAdapter(),
        FlattenedViewFormatAdapter(),
    )


class RecordNormalizer(Protocol):
    """Port for explicit model/format adapters and traceability preservation."""

    def normalize(
        self,
        source_row: Mapping[str, Any],
        *,
        analysis_run_id: str,
        configuration_version: str,
        analysis_logic_version: str,
        include_sensitive: bool = False,
    ) -> NormalizationResult:
        """Normalize one row without inventing missing metadata or formats."""
        ...


class InvocationRecordNormalizer:
    """Normalize an Athena view row into an :class:`InvocationRecord`."""

    def __init__(
        self,
        adapters: Sequence[ModelFormatAdapter] | None = None,
    ) -> None:
        self._adapters = tuple(adapters or default_model_format_adapters())

    def normalize(
        self,
        source_row: Mapping[str, Any],
        *,
        analysis_run_id: str,
        configuration_version: str,
        analysis_logic_version: str,
        include_sensitive: bool = False,
    ) -> NormalizationResult:
        errors: list[NormalizationError] = []
        if not isinstance(source_row, Mapping):
            return NormalizationResult(
                error=_diagnostic("MALFORMED_SOURCE_ROW", "source row must be a mapping", None)
            )

        run_error = _required_context_error("analysis_run_id", analysis_run_id)
        config_error = _required_context_error("configuration_version", configuration_version)
        logic_error = _required_context_error("analysis_logic_version", analysis_logic_version)
        context_errors = tuple(error for error in (run_error, config_error, logic_error) if error)
        if context_errors:
            return NormalizationResult(errors=context_errors)

        raw_request_id = _first_present(source_row, _FIELD_ALIASES["source_request_id"])
        request_id, error = _required_text(raw_request_id, "source_request_id", None)
        if error:
            errors.append(error)

        source_reference = request_id
        raw_timestamp = _first_present(source_row, _FIELD_ALIASES["source_timestamp_utc"])
        source_timestamp, error = _parse_timestamp(raw_timestamp, source_reference)
        if error:
            errors.append(error)

        required_text_fields: dict[str, str | None] = {}
        for field_name in (
            "source_account",
            "source_region",
            "model_id",
            "operation",
        ):
            value, field_error = _required_text(
                _first_present(source_row, _FIELD_ALIASES[field_name]),
                field_name,
                source_reference,
            )
            required_text_fields[field_name] = value
            if field_error:
                errors.append(field_error)

        if errors:
            return NormalizationResult(errors=tuple(errors))

        request_metadata, metadata_error = _parse_request_metadata(
            _first_present(source_row, _FIELD_ALIASES["request_metadata"]),
            source_reference,
        )
        if metadata_error:
            errors.append(metadata_error)

        token_values: dict[str, int | None] = {}
        for field_name in (
            "input_token_count",
            "output_token_count",
            "cache_read_token_count",
            "cache_write_token_count",
        ):
            value, token_error = _parse_optional_nonnegative_int(
                _first_present(source_row, _FIELD_ALIASES[field_name]),
                field_name,
                source_reference,
            )
            token_values[field_name] = value
            if token_error:
                errors.append(token_error)

        declared_format, format_error = _declared_format(source_row, source_reference)
        if format_error:
            errors.append(format_error)

        adapter = self._select_adapter(
            required_text_fields["model_id"], declared_format, source_row
        )
        if adapter is None:
            errors.append(
                _diagnostic(
                    "UNSUPPORTED_MODEL_FORMAT",
                    "no explicit adapter supports the model or declared format",
                    source_reference,
                )
            )
            model_format = declared_format
        else:
            adapter_result = adapter.normalize(
                source_row,
                source_request_reference=source_reference,
            )
            errors.extend(adapter_result.errors)
            model_format = adapter_result.model_format

        stop_reason, stop_reason_error = _parse_optional_text(
            _first_present(source_row, _FIELD_ALIASES["stop_reason"]),
            "stop_reason",
            source_reference,
        )
        if stop_reason_error:
            errors.append(stop_reason_error)
        content_type, content_type_error = _parse_optional_text(
            _first_present(source_row, _FIELD_ALIASES["content_type"]),
            "content_type",
            source_reference,
        )
        if content_type_error:
            errors.append(content_type_error)

        record = InvocationRecord(
            source_request_id=request_id,
            source_timestamp_utc=source_timestamp,
            source_account=required_text_fields["source_account"],
            source_region=required_text_fields["source_region"],
            model_id=required_text_fields["model_id"],
            operation=required_text_fields["operation"],
            analysis_run_id=analysis_run_id,
            configuration_version=configuration_version,
            analysis_logic_version=analysis_logic_version,
            input_token_count=token_values["input_token_count"],
            output_token_count=token_values["output_token_count"],
            cache_read_token_count=token_values["cache_read_token_count"],
            cache_write_token_count=token_values["cache_write_token_count"],
            stop_reason=stop_reason,
            content_type=content_type,
            request_metadata=request_metadata,
            model_format=model_format,
            identity_metadata=_identity_metadata(source_row),
            sensitive_content=(
                _sensitive_content(source_row) if include_sensitive else None
            ),
        )
        return NormalizationResult(record=record, errors=tuple(errors))

    def _select_adapter(
        self,
        model_id: str | None,
        declared_format: str | None,
        source_row: Mapping[str, Any],
    ) -> ModelFormatAdapter | None:
        for adapter in self._adapters:
            if adapter.supports(model_id, declared_format, source_row):
                return adapter
        return None


SourceRowNormalizer = InvocationRecordNormalizer

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "source_request_id": ("source_request_id", "request_id", "requestid"),
    "source_timestamp_utc": (
        "source_timestamp_utc",
        "timestamp",
        "event_timestamp",
        "source_timestamp",
    ),
    "source_account": ("source_account", "account", "account_id", "accountid"),
    "source_region": ("source_region", "region"),
    "model_id": ("model_id", "model", "modelid"),
    "operation": ("operation",),
    "input_token_count": (
        "input_token_count",
        "usage_input_tokens",
        "input_tokens",
        "input.inputtokencount",
        "input.input_token_count",
        "inputTokenCount",
    ),
    "output_token_count": (
        "output_token_count",
        "usage_output_tokens",
        "output_tokens",
        "output.outputtokencount",
        "output.output_token_count",
        "outputTokenCount",
    ),
    "cache_read_token_count": (
        "cache_read_token_count",
        "usage_cache_read_tokens",
        "cache_read_tokens",
        "cache.readtokencount",
        "cache_read_token_count",
    ),
    "cache_write_token_count": (
        "cache_write_token_count",
        "usage_cache_write_tokens",
        "cache_write_tokens",
        "cache.writetokencount",
        "cache_write_token_count",
    ),
    "stop_reason": ("stop_reason", "stopReason", "output.stopreason"),
    "content_type": ("content_type", "contentType"),
    "request_metadata": ("request_metadata", "requestmetadata", "requestMetadata"),
}

_BODY_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "input_body_json": (
        "input_body_json",
        "inputbodyjson",
        "input_body",
        "input.body_json",
    ),
    "output_body_json": (
        "output_body_json",
        "outputbodyjson",
        "output_body",
        "output.body_json",
    ),
}


def _first_present(source_row: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    for alias in aliases:
        if alias in source_row:
            return source_row[alias]
        if "." in alias:
            current: Any = source_row
            for part in alias.split("."):
                if not isinstance(current, Mapping) or part not in current:
                    current = _MISSING
                    break
                current = current[part]
            if current is not _MISSING:
                return current
    return _MISSING


def _required_context_error(field_name: str, value: Any) -> NormalizationError | None:
    if not isinstance(value, str) or not value.strip():
        return _diagnostic("MISSING_TRACEABILITY", f"{field_name} is required", None)
    return None


def _required_text(
    value: Any,
    field_name: str,
    source_reference: str | None,
) -> tuple[str | None, NormalizationError | None]:
    if value is _MISSING or value is None:
        return None, _diagnostic("MISSING_TRACEABILITY", f"{field_name} is required", source_reference)
    if not isinstance(value, str) or not value.strip():
        return None, _diagnostic("INVALID_TRACEABILITY", f"{field_name} must be non-empty text", source_reference)
    return value, None


def _optional_text(value: Any) -> str | None:
    if value is _MISSING or value is None:
        return None
    return value if isinstance(value, str) else None


def _parse_optional_text(
    value: Any,
    field_name: str,
    source_reference: str | None,
) -> tuple[str | None, NormalizationError | None]:
    if value is _MISSING or value is None:
        return None, None
    if not isinstance(value, str):
        return None, _diagnostic(
            "MALFORMED_SOURCE_FIELD",
            f"{field_name} must be text when present",
            source_reference,
        )
    return value, None


def _parse_timestamp(
    value: Any,
    source_reference: str | None,
) -> tuple[datetime | None, NormalizationError | None]:
    if value is _MISSING or value is None:
        return None, _diagnostic("MISSING_TRACEABILITY", "source_timestamp_utc is required", source_reference)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    else:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, _diagnostic("MALFORMED_TIMESTAMP", "source_timestamp_utc must be timezone-aware ISO text", source_reference)
    return parsed.astimezone(timezone.utc), None


def _parse_optional_nonnegative_int(
    value: Any,
    field_name: str,
    source_reference: str | None,
) -> tuple[int | None, NormalizationError | None]:
    if value is _MISSING or value is None:
        return None, None
    if isinstance(value, bool) or isinstance(value, float) and (not isfinite(value) or not value.is_integer()):
        return None, _diagnostic("MALFORMED_TOKEN_COUNT", f"{field_name} must be a nonnegative integer", source_reference)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        return None, _diagnostic("MALFORMED_TOKEN_COUNT", f"{field_name} must be a nonnegative integer", source_reference)
    if parsed < 0:
        return None, _diagnostic("MALFORMED_TOKEN_COUNT", f"{field_name} must be a nonnegative integer", source_reference)
    return parsed, None


def _parse_request_metadata(
    value: Any,
    source_reference: str | None,
) -> tuple[Mapping[str, str], NormalizationError | None]:
    if value is _MISSING or value is None:
        return {}, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, _diagnostic("MALFORMED_REQUEST_METADATA", "request_metadata is not valid JSON", source_reference)
    if not isinstance(value, Mapping):
        return {}, _diagnostic("MALFORMED_REQUEST_METADATA", "request_metadata must be an object", source_reference)
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        return {}, _diagnostic("MALFORMED_REQUEST_METADATA", "request_metadata keys and values must be text", source_reference)
    return dict(value), None


def _declared_format(
    source_row: Mapping[str, Any],
    source_reference: str | None,
) -> tuple[str | None, NormalizationError | None]:
    value = _first_present(source_row, ("model_format", "format"))
    if value is _MISSING or value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, _diagnostic("MALFORMED_MODEL_FORMAT", "model_format must be non-empty text", source_reference)
    return _normalize_format(value), None


def _normalize_format(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _identity_metadata(source_row: Mapping[str, Any]) -> IdentityMetadata | None:
    values = {
        "identity_arn": _optional_text(
            _first_present(source_row, ("identity_arn", "identityArn"))
        ),
        "session_name": _optional_text(
            _first_present(source_row, ("identity_session_name", "session_name", "sessionName"))
        ),
        "name": _optional_text(
            _first_present(source_row, ("identity_name", "name"))
        ),
        "account_owner": _optional_text(
            _first_present(source_row, ("account_owner", "accountOwner"))
        ),
    }
    if not any(values.values()):
        return None
    return IdentityMetadata(**values)


def _sensitive_content(source_row: Mapping[str, Any]) -> SensitiveContent | None:
    prompt = _optional_text(_first_present(source_row, ("prompt_preview", "prompt")))
    response = _optional_text(_first_present(source_row, ("response_preview", "response")))
    if prompt is None and response is None:
        return None
    return SensitiveContent(prompt_preview=prompt, response_preview=response)


def _diagnostic(
    code: str,
    detail: str,
    source_request_reference: str | None,
) -> NormalizationError:
    bounded_detail = " ".join(detail.split())[:MAX_DIAGNOSTIC_DETAIL_LENGTH]
    bounded_reference = (
        source_request_reference[:MAX_DIAGNOSTIC_DETAIL_LENGTH]
        if isinstance(source_request_reference, str)
        else None
    )
    return NormalizationError(
        code=code,
        detail=bounded_detail,
        source_request_reference=bounded_reference,
    )


__all__ = [
    "AdapterResult",
    "AnthropicModelFormatAdapter",
    "CohereModelFormatAdapter",
    "FlattenedViewFormatAdapter",
    "InvocationRecordNormalizer",
    "LlamaModelFormatAdapter",
    "MAX_DIAGNOSTIC_DETAIL_LENGTH",
    "MistralModelFormatAdapter",
    "ModelFormatAdapter",
    "NormalizationError",
    "NormalizationResult",
    "NovaModelFormatAdapter",
    "RecordNormalizer",
    "SourceRowNormalizer",
    "TitanModelFormatAdapter",
    "default_model_format_adapters",
]
