"""Read-only source access boundary for the existing Athena view.

No AWS SDK is imported here. An Athena client and any execution policy are
owned by an adapter supplied by the application layer, which keeps disabled
mode and pure tests free of AWS side effects.  Scope planning is also pure:
it only resolves an approved request and builds predicates for a later reader.
"""

from __future__ import annotations

import calendar
import hashlib
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from .models import ATHENA_WORKGROUP, SOURCE_VIEW, AnalysisScope

SOURCE_AVAILABILITY_MONTHS = 6
UNAVAILABLE_INTERVAL_SEPARATOR = ".."
ATHENA_DATABASE = "bedrock_logs"

# These expressions are aliases to the stable logical fields exposed by the
# existing view.  Raw prompt and response columns are deliberately kept out of
# the default projection and may only be added by an authorized reader.
DEFAULT_SOURCE_SELECT_FIELDS = (
    "accountid AS source_account",
    "CAST(timestamp AS timestamp) AS source_timestamp_utc",
    "region AS source_region",
    "requestid AS source_request_id",
    "operation",
    "modelid AS model_id",
    "identity_arn",
    "identity",
    "requestmetadata AS request_metadata",
    "year",
    "month",
    "day",
    "input_token_count",
    "input_content_type",
    "output_token_count",
    "output_content_type",
    "usage_input_tokens",
    "usage_output_tokens",
    "usage_total_tokens",
    "usage_cache_read_tokens",
    "usage_cache_write_tokens",
    "stop_reason",
)
CONTENT_SOURCE_SELECT_FIELDS = (
    "prompt_preview",
    "response_preview",
)


class SourceScopeError(ValueError):
    """Raised when a source request is unbounded or outside its approval."""

    def __init__(self, message: str, *, fields: tuple[str, ...] = ()):
        super().__init__(message)
        self.fields = fields


class UnboundedSourceScopeError(SourceScopeError):
    """Raised when a request does not have finite filters and UTC dates."""


class ScopeNotApprovedError(SourceScopeError):
    """Raised when a request asks for values outside the approved scope."""


@dataclass(frozen=True)
class SourceAvailabilityWindow:
    """Inclusive UTC date window exposed by the existing source view."""

    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        if (
            not isinstance(self.start_date, date)
            or isinstance(self.start_date, datetime)
            or not isinstance(self.end_date, date)
            or isinstance(self.end_date, datetime)
        ):
            raise TypeError("source availability bounds must be date values")
        if self.start_date > self.end_date:
            raise ValueError("source availability start_date must not be after end_date")

    @classmethod
    def six_months_through(
        cls, as_of: date | datetime | None = None
    ) -> "SourceAvailabilityWindow":
        """Return the date projection of the view's six-calendar-month window.

        The view uses ``current_timestamp`` and an inclusive ``>=`` lower
        bound.  A date-level planner therefore retains the date containing the
        lower bound and the current UTC date as the inclusive upper bound.
        """

        if as_of is None:
            as_of_date = datetime.now(timezone.utc).date()
        elif isinstance(as_of, datetime):
            if as_of.tzinfo is None or as_of.utcoffset() != timezone.utc.utcoffset(as_of):
                raise ValueError("as_of must be a timezone-aware UTC datetime")
            as_of_date = as_of.date()
        else:
            as_of_date = as_of
        return cls(_subtract_calendar_months(as_of_date, SOURCE_AVAILABILITY_MONTHS), as_of_date)


@dataclass(frozen=True)
class SourceScopePlan:
    """Pure plan for one approved, date-bounded source read."""

    requested_scope: AnalysisScope
    approved_scope: AnalysisScope
    source_window: SourceAvailabilityWindow
    effective_scope: AnalysisScope | None
    unavailable_intervals: tuple[str, ...] = ()
    partition_predicate: str = ""
    dimension_predicates: tuple[str, ...] = ()
    where_clause: str = ""
    workgroup: str = ATHENA_WORKGROUP
    source_view: str = SOURCE_VIEW

    @property
    def queryable(self) -> bool:
        """Whether the request has at least one source date to query."""

        return self.effective_scope is not None

    @property
    def run_metadata(self) -> Mapping[str, Any]:
        """Metadata to retain with a run without changing source resources."""

        return {
            "requested_scope": self.requested_scope.to_dict(),
            "approved_scope": self.approved_scope.to_dict(),
            "effective_scope": self.effective_scope.to_dict() if self.effective_scope else None,
            "source_availability_window": {
                "start_date": self.source_window.start_date.isoformat(),
                "end_date": self.source_window.end_date.isoformat(),
            },
            "unavailable_intervals": list(self.unavailable_intervals),
            "workgroup": self.workgroup,
            "source_view": self.source_view,
            "queryable": self.queryable,
        }

    def as_run_metadata(self) -> Mapping[str, Any]:
        """Alias used by run-control adapters when persisting plan metadata."""

        return self.run_metadata


@dataclass(frozen=True)
class SourceReadResult:
    """Rows and bounded source metadata returned by an injected reader."""

    rows: tuple[Mapping[str, Any], ...] = ()
    query_execution_ids: tuple[str, ...] = ()
    unavailable_intervals: tuple[str, ...] = ()
    scanned_bytes: int = 0
    errors: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SourceReader(Protocol):
    """Port for bounded, partition-filtered reads from the curated view."""

    def read(
        self,
        scope: AnalysisScope,
        analysis_run_id: str,
        configuration_version: str,
        *,
        include_content: bool = False,
    ) -> SourceReadResult:
        """Read eligible source rows without modifying source tables or views."""
        ...


class AthenaQueryClient(Protocol):
    """Minimal injected port for submitting and polling Athena queries."""

    def start_query_execution(self, **kwargs: Any) -> Mapping[str, Any]:
        """Submit one query and return the Athena response."""
        ...

    def get_query_execution(self, **kwargs: Any) -> Mapping[str, Any]:
        """Return the status and statistics for one query execution."""
        ...


class AthenaResultLoader(Protocol):
    """Optional port for loading completed rows after query polling."""

    def load(self, query_execution_id: str) -> SourceReadResult:
        """Load rows and bounded statistics for a completed execution."""
        ...


class ContentAccessNotApprovedError(SourceScopeError):
    """Raised when a caller requests previews without explicit authorization."""


class AthenaSourceReader:
    """Read the existing invocation view with bounded, read-only execution."""

    def __init__(
        self,
        client: AthenaQueryClient,
        approved_scope: AnalysisScope,
        *,
        source_window: SourceAvailabilityWindow | tuple[date, date] | None = None,
        as_of: date | datetime | None = None,
        content_access_approved: bool = False,
        result_loader: AthenaResultLoader | None = None,
        enabled: bool = True,
        poll_interval_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.25,
        sleep_fn: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._approved_scope = approved_scope
        self._source_window = source_window
        self._as_of = as_of
        self._content_access_approved = content_access_approved
        self._result_loader = result_loader
        self._enabled = enabled
        self._poll_interval_seconds = _positive_finite(
            poll_interval_seconds, "poll_interval_seconds"
        )
        self._timeout_seconds = _positive_finite(timeout_seconds, "timeout_seconds")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        self._max_retries = max_retries
        self._retry_backoff_seconds = _nonnegative_finite(
            retry_backoff_seconds, "retry_backoff_seconds"
        )
        self._sleep_fn = sleep_fn or time.sleep
        self._clock = clock or time.monotonic
        self._results_by_run: dict[str, SourceReadResult] = {}

    def read(
        self,
        scope: AnalysisScope,
        analysis_run_id: str,
        configuration_version: str,
        *,
        include_content: bool = False,
    ) -> SourceReadResult:
        """Submit, poll, and load one bounded read of the existing view."""

        if include_content and not self._content_access_approved:
            raise ContentAccessNotApprovedError(
                "content source fields require an explicitly approved content path",
                fields=("include_content",),
            )
        if not isinstance(analysis_run_id, str) or not analysis_run_id.strip():
            raise ValueError("analysis_run_id must be non-empty text")
        if not isinstance(configuration_version, str) or not configuration_version.strip():
            raise ValueError("configuration_version must be non-empty text")

        cached = self._results_by_run.get(analysis_run_id)
        if cached is not None:
            return cached

        if not self._enabled:
            result = SourceReadResult(
                metadata={
                    "enabled": False,
                    "query_submitted": False,
                    "source_availability": "disabled",
                    "analysis_run_id": analysis_run_id,
                    "configuration_version": configuration_version,
                }
            )
            self._results_by_run[analysis_run_id] = result
            return result

        plan = resolve_source_scope(
            scope,
            self._approved_scope,
            source_window=self._source_window,
            as_of=self._as_of,
        )
        metadata: dict[str, Any] = {
            **dict(plan.run_metadata),
            "database": ATHENA_DATABASE,
            "result_location_enforced_by_workgroup": True,
            "result_location_supplied": False,
            "selected_fields": list(source_select_fields(include_content=include_content)),
            "include_content": include_content,
            "analysis_run_id": analysis_run_id,
            "configuration_version": configuration_version,
            "source_set_determined": False,
            "publication_allowed": False,
        }
        if not plan.queryable:
            metadata["query_submitted"] = False
            metadata["source_availability"] = "unavailable"
            result = SourceReadResult(
                unavailable_intervals=plan.unavailable_intervals,
                errors=("SOURCE_UNAVAILABLE",),
                metadata=metadata,
            )
            self._results_by_run[analysis_run_id] = result
            return result

        query_string = build_source_query(plan, include_content=include_content)
        request = {
            "QueryString": query_string,
            "QueryExecutionContext": {"Database": ATHENA_DATABASE},
            "WorkGroup": ATHENA_WORKGROUP,
            "ClientRequestToken": _client_request_token(analysis_run_id),
        }
        metadata["query_string"] = query_string
        response, retries, failed = self._call_with_retries(
            self._client.start_query_execution, request
        )
        metadata["submission_retries"] = retries
        if failed or response is None:
            metadata["query_submitted"] = False
            result = SourceReadResult(
                unavailable_intervals=plan.unavailable_intervals,
                errors=("SOURCE_QUERY_SUBMISSION_FAILED",),
                metadata=metadata,
            )
            self._results_by_run[analysis_run_id] = result
            return result

        query_execution_id = _query_execution_id(response)
        if query_execution_id is None:
            metadata["query_submitted"] = False
            result = SourceReadResult(
                unavailable_intervals=plan.unavailable_intervals,
                errors=("SOURCE_QUERY_ID_MISSING",),
                metadata=metadata,
            )
            self._results_by_run[analysis_run_id] = result
            return result

        scanned_bytes = _scanned_bytes(response)
        metadata.update(
            {
                "query_submitted": True,
                "source_availability": "available",
                "query_execution_ids": [query_execution_id],
                "scanned_bytes": scanned_bytes,
                "query_request": {
                    "QueryExecutionContext": request["QueryExecutionContext"],
                    "WorkGroup": request["WorkGroup"],
                    "result_location_supplied": False,
                },
            }
        )
        poll = self._poll_query(query_execution_id)
        scanned_bytes = max(scanned_bytes, poll.scanned_bytes)
        metadata.update(poll.metadata)
        metadata["scanned_bytes"] = scanned_bytes
        if poll.error:
            result = SourceReadResult(
                query_execution_ids=(query_execution_id,),
                unavailable_intervals=plan.unavailable_intervals,
                scanned_bytes=scanned_bytes,
                errors=(poll.error,),
                metadata=metadata,
            )
            self._results_by_run[analysis_run_id] = result
            return result

        if self._result_loader is None:
            result = SourceReadResult(
                query_execution_ids=(query_execution_id,),
                unavailable_intervals=plan.unavailable_intervals,
                scanned_bytes=scanned_bytes,
                errors=("SOURCE_RESULTS_NOT_LOADED",),
                metadata=metadata,
            )
            self._results_by_run[analysis_run_id] = result
            return result

        loaded, retries, failed = self._call_with_retries(
            self._result_loader.load, query_execution_id
        )
        metadata["result_load_retries"] = retries
        if failed or loaded is None or not isinstance(loaded, SourceReadResult):
            result = SourceReadResult(
                query_execution_ids=(query_execution_id,),
                unavailable_intervals=plan.unavailable_intervals,
                scanned_bytes=scanned_bytes,
                errors=("SOURCE_QUERY_RESULT_FAILED",),
                metadata=metadata,
            )
            self._results_by_run[analysis_run_id] = result
            return result

        loaded_errors = _bounded_source_errors(loaded.errors)
        metadata.update(_content_free_metadata(loaded.metadata))
        scanned_bytes = max(scanned_bytes, loaded.scanned_bytes)
        metadata["scanned_bytes"] = scanned_bytes
        if loaded_errors:
            result = SourceReadResult(
                rows=(),
                query_execution_ids=(query_execution_id,),
                unavailable_intervals=plan.unavailable_intervals,
                scanned_bytes=scanned_bytes,
                errors=loaded_errors,
                metadata=metadata,
            )
        else:
            metadata["source_set_determined"] = True
            metadata["publication_allowed"] = True
            result = SourceReadResult(
                rows=loaded.rows,
                query_execution_ids=(query_execution_id,),
                unavailable_intervals=plan.unavailable_intervals,
                scanned_bytes=scanned_bytes,
                metadata=metadata,
            )
        self._results_by_run[analysis_run_id] = result
        return result

    def _call_with_retries(
        self, operation: Callable[..., Any], argument: Any
    ) -> tuple[Any | None, int, bool]:
        retries = 0
        while True:
            try:
                value = (
                    operation(**argument)
                    if isinstance(argument, dict)
                    else operation(argument)
                )
                return value, retries, False
            except Exception:
                if retries >= self._max_retries:
                    return None, retries, True
                self._sleep(self._retry_backoff_seconds * (2**retries))
                retries += 1

    def _poll_query(self, query_execution_id: str) -> "_PollResult":
        get_query_execution = getattr(self._client, "get_query_execution", None)
        if not callable(get_query_execution):
            return _PollResult(metadata={"query_state": "NOT_POLLED"})

        started = self._clock()
        poll_attempts = 0
        poll_retries = 0
        scanned_bytes = 0
        max_attempts = max(1, math.ceil(self._timeout_seconds / self._poll_interval_seconds))
        while poll_attempts < max_attempts:
            response, retries, failed = self._call_with_retries(
                get_query_execution, {"QueryExecutionId": query_execution_id}
            )
            poll_attempts += 1
            poll_retries += retries
            if failed or response is None:
                return _PollResult(
                    scanned_bytes=scanned_bytes,
                    error="SOURCE_QUERY_STATUS_FAILED",
                    metadata={
                        "query_state": "UNKNOWN",
                        "poll_attempts": poll_attempts,
                        "poll_retries": poll_retries,
                    },
                )
            scanned_bytes = max(scanned_bytes, _scanned_bytes(response))
            state = _query_state(response)
            if state == "SUCCEEDED":
                return _PollResult(
                    scanned_bytes=scanned_bytes,
                    metadata={
                        "query_state": state,
                        "poll_attempts": poll_attempts,
                        "poll_retries": poll_retries,
                    },
                )
            if state in {"FAILED", "CANCELLED"}:
                return _PollResult(
                    scanned_bytes=scanned_bytes,
                    error="SOURCE_QUERY_FAILED" if state == "FAILED" else "SOURCE_QUERY_CANCELLED",
                    metadata={
                        "query_state": state,
                        "poll_attempts": poll_attempts,
                        "poll_retries": poll_retries,
                    },
                )
            if state not in {"QUEUED", "RUNNING"}:
                return _PollResult(
                    scanned_bytes=scanned_bytes,
                    error="SOURCE_QUERY_STATUS_INVALID",
                    metadata={
                        "query_state": state or "UNKNOWN",
                        "poll_attempts": poll_attempts,
                        "poll_retries": poll_retries,
                    },
                )
            if self._clock() - started >= self._timeout_seconds:
                break
            self._sleep(min(self._poll_interval_seconds, self._timeout_seconds))

        return _PollResult(
            scanned_bytes=scanned_bytes,
            error="SOURCE_QUERY_TIMEOUT",
            metadata={
                "query_state": "TIMEOUT",
                "poll_attempts": poll_attempts,
                "poll_retries": poll_retries,
            },
        )

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep_fn(seconds)


@dataclass(frozen=True)
class _PollResult:
    scanned_bytes: int = 0
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _client_request_token(analysis_run_id: str) -> str:
    return hashlib.sha256(analysis_run_id.encode("utf-8")).hexdigest()


def _query_state(response: Mapping[str, Any]) -> str | None:
    if not isinstance(response, Mapping):
        return None
    execution = response.get("QueryExecution", response)
    if not isinstance(execution, Mapping):
        return None
    status = execution.get("Status", {})
    if not isinstance(status, Mapping):
        return None
    state = status.get("State")
    return state.strip().upper() if isinstance(state, str) and state.strip() else None


_CONTENT_FREE_METADATA_KEYS = frozenset({
    "page_count", "row_count", "scan_cutoff_bytes", "result_count", "next_page_available",
    "source_set_determined", "publication_allowed",
})


def _bounded_source_errors(errors: tuple[str, ...]) -> tuple[str, ...]:
    allowed = frozenset({
        "ATHENA_SCAN_CUTOFF",
        "SOURCE_QUERY_FAILED",
        "SOURCE_QUERY_CANCELLED",
        "SOURCE_QUERY_STATUS_FAILED",
        "SOURCE_QUERY_TIMEOUT",
        "SOURCE_QUERY_RESULT_FAILED",
    })
    bounded = tuple(error for error in errors if isinstance(error, str) and error in allowed)
    return bounded or (("SOURCE_QUERY_RESULT_FAILED",) if errors else ())


def _content_free_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    return {
        key: value for key, value in metadata.items()
        if key in _CONTENT_FREE_METADATA_KEYS
        and isinstance(value, (bool, int, float, type(None)))
        and not isinstance(value, complex)
    }


# Descriptive alias for callers that use the component name from the design.
AthenaSourceAdapter = AthenaSourceReader


def source_select_fields(*, include_content: bool = False) -> tuple[str, ...]:
    """Return the allowlisted source projection for a read mode."""

    if include_content:
        return DEFAULT_SOURCE_SELECT_FIELDS + CONTENT_SOURCE_SELECT_FIELDS
    return DEFAULT_SOURCE_SELECT_FIELDS


def build_source_query(plan: SourceScopePlan, *, include_content: bool = False) -> str:
    """Build a SELECT against the existing view without any DDL."""

    if not plan.queryable or not plan.where_clause:
        raise ValueError("a query requires a queryable source scope")
    selected_fields = ",\n          ".join(source_select_fields(include_content=include_content))
    return (
        "SELECT\n"
        f"          {selected_fields}\n"
        f"        FROM \"{ATHENA_DATABASE}\".\"{SOURCE_VIEW.split('.', 1)[1]}\"\n"
        f"        WHERE {plan.where_clause}"
    )


def _positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def _nonnegative_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return float(value)


def _query_execution_id(response: Mapping[str, Any]) -> str | None:
    if not isinstance(response, Mapping):
        return None
    value = response.get("QueryExecutionId")
    if isinstance(value, str) and value.strip():
        return value
    return None


def _scanned_bytes(response: Mapping[str, Any]) -> int:
    if not isinstance(response, Mapping):
        return 0
    statistics = response.get("QueryExecution", {})
    if isinstance(statistics, Mapping):
        statistics = statistics.get("Statistics", {})
    if not isinstance(statistics, Mapping):
        statistics = {}
    value = statistics.get("DataScannedInBytes", response.get("DataScannedInBytes", 0))
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def resolve_source_scope(
    requested_scope: AnalysisScope,
    approved_scope: AnalysisScope,
    *,
    source_window: SourceAvailabilityWindow | tuple[date, date] | None = None,
    as_of: date | datetime | None = None,
) -> SourceScopePlan:
    """Resolve a finite request against approved dimensions and source dates.

    Non-date dimensions must be subsets of the approved values.  Requested
    dates outside the six-month source window are returned as explicit
    unavailable intervals; only the date intersection is queryable.  A fully
    unavailable request returns a plan with no query predicates, preventing a
    caller from accidentally widening the read.
    """

    _validate_bounded_scope(requested_scope, "requested_scope")
    _validate_bounded_scope(approved_scope, "approved_scope")
    _ensure_dimension_subset(requested_scope, approved_scope)

    resolved_window = _coerce_source_window(source_window, as_of)
    unavailable = _unavailable_intervals(
        requested_scope.start_date, requested_scope.end_date, resolved_window
    )
    effective_start = max(requested_scope.start_date, resolved_window.start_date)
    effective_end = min(requested_scope.end_date, resolved_window.end_date)
    effective_scope: AnalysisScope | None
    if effective_start <= effective_end:
        effective_scope = AnalysisScope(
            accounts=requested_scope.accounts,
            regions=requested_scope.regions,
            models=requested_scope.models,
            operations=requested_scope.operations,
            start_date=effective_start,
            end_date=effective_end,
        )
        partition_predicate = _build_partition_predicate(effective_start, effective_end)
        dimension_predicates = _build_dimension_predicates(effective_scope)
        where_clause = " AND ".join((f"({partition_predicate})", *dimension_predicates))
    else:
        effective_scope = None
        partition_predicate = ""
        dimension_predicates = ()
        where_clause = ""

    return SourceScopePlan(
        requested_scope=requested_scope,
        approved_scope=approved_scope,
        source_window=resolved_window,
        effective_scope=effective_scope,
        unavailable_intervals=unavailable,
        partition_predicate=partition_predicate,
        dimension_predicates=dimension_predicates,
        where_clause=where_clause,
    )


# Short alias for adapters and callers that use the design document's name.
plan_source_scope = resolve_source_scope


def _validate_bounded_scope(scope: AnalysisScope, label: str) -> None:
    if not isinstance(scope, AnalysisScope):
        raise UnboundedSourceScopeError(
            "source scope must be an AnalysisScope with finite filters and UTC dates",
            fields=(label,),
        )

    missing: list[str] = []
    for dimension in ("accounts", "regions", "models", "operations"):
        values = getattr(scope, dimension)
        if not values or any(not isinstance(value, str) or not value.strip() for value in values):
            missing.append(f"{label}.{dimension}")
    for date_field in ("start_date", "end_date"):
        value = getattr(scope, date_field)
        if value is None or isinstance(value, datetime) or not isinstance(value, date):
            missing.append(f"{label}.{date_field}")
    if missing:
        raise UnboundedSourceScopeError(
            "source requests require non-empty account, Region, model, and operation filters "
            "and finite UTC start/end dates",
            fields=tuple(missing),
        )
    if scope.start_date > scope.end_date:
        raise UnboundedSourceScopeError(
            f"{label}.start_date must not be after {label}.end_date",
            fields=(f"{label}.start_date", f"{label}.end_date"),
        )


def _ensure_dimension_subset(requested: AnalysisScope, approved: AnalysisScope) -> None:
    fields: list[str] = []
    for dimension in ("accounts", "regions", "models", "operations"):
        requested_values = set(getattr(requested, dimension))
        approved_values = set(getattr(approved, dimension))
        if not requested_values.issubset(approved_values):
            fields.append(f"requested_scope.{dimension}")
    if requested.start_date < approved.start_date or requested.end_date > approved.end_date:
        fields.append("requested_scope.date_range")
    if fields:
        raise ScopeNotApprovedError(
            "requested source scope exceeds the approved scope",
            fields=tuple(fields),
        )


def _coerce_source_window(
    source_window: SourceAvailabilityWindow | tuple[date, date] | None,
    as_of: date | datetime | None,
) -> SourceAvailabilityWindow:
    if source_window is None:
        return SourceAvailabilityWindow.six_months_through(as_of)
    if isinstance(source_window, SourceAvailabilityWindow):
        return source_window
    if (
        isinstance(source_window, tuple)
        and len(source_window) == 2
        and isinstance(source_window[0], date)
        and isinstance(source_window[1], date)
    ):
        return SourceAvailabilityWindow(source_window[0], source_window[1])
    raise TypeError("source_window must be SourceAvailabilityWindow or (start_date, end_date)")


def _unavailable_intervals(
    requested_start: date,
    requested_end: date,
    source_window: SourceAvailabilityWindow,
) -> tuple[str, ...]:
    intervals: list[str] = []
    if requested_start < source_window.start_date:
        intervals.append(
            _format_interval(requested_start, min(requested_end, source_window.start_date - timedelta(days=1)))
        )
    if requested_end > source_window.end_date:
        intervals.append(
            _format_interval(max(requested_start, source_window.end_date + timedelta(days=1)), requested_end)
        )
    return tuple(intervals)


def _format_interval(start: date, end: date) -> str:
    return f"{start.isoformat()}{UNAVAILABLE_INTERVAL_SEPARATOR}{end.isoformat()}"


def _build_partition_predicate(start: date, end: date) -> str:
    predicates = [
        " AND ".join(
            (
                f"year = '{current.year:04d}'",
                f"month = '{current.month:02d}'",
                f"day = '{current.day:02d}'",
            )
        )
        for current in _dates_between(start, end)
    ]
    return "(" + ") OR (".join(predicates) + ")"


def _build_dimension_predicates(scope: AnalysisScope) -> tuple[str, ...]:
    return (
        _in_predicate("accountid", scope.accounts),
        _in_predicate("region", scope.regions),
        _in_predicate("modelid", scope.models),
        _in_predicate("operation", scope.operations),
    )


def _in_predicate(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(_sql_literal(value) for value in values)
    return f"{column} IN ({rendered})"


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _dates_between(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _subtract_calendar_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


__all__ = [
    "ATHENA_DATABASE",
    "AthenaQueryClient",
    "AthenaResultLoader",
    "AthenaSourceAdapter",
    "AthenaSourceReader",
    "CONTENT_SOURCE_SELECT_FIELDS",
    "ContentAccessNotApprovedError",
    "DEFAULT_SOURCE_SELECT_FIELDS",
    "ScopeNotApprovedError",
    "ScopeResolver",
    "SourceAvailabilityWindow",
    "SourceReadResult",
    "SourceReader",
    "SourceScopeError",
    "SourceScopePlan",
    "UnboundedSourceScopeError",
    "build_source_query",
    "plan_source_scope",
    "resolve_source_scope",
    "source_select_fields",
]
