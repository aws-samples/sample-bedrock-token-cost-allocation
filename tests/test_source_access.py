"""Tests for bounded source-scope resolution and Athena partition planning."""

from datetime import date, datetime, timedelta, timezone

import pytest

from bedrock_usage_insights.models import ATHENA_WORKGROUP, SOURCE_VIEW, AnalysisScope
from bedrock_usage_insights.source_access import (
    ScopeNotApprovedError,
    SourceAvailabilityWindow,
    UnboundedSourceScopeError,
    resolve_source_scope,
)

UTC = timezone.utc


def make_scope(start=date(2025, 1, 1), end=date(2026, 12, 31), **changes):
    values = {
        "accounts": ("111111111111", "222222222222"),
        "regions": ("us-east-1", "eu-west-1"),
        "models": ("amazon.nova-pro-v1:0",),
        "operations": ("InvokeModel",),
        "start_date": start,
        "end_date": end,
    }
    values.update(changes)
    return AnalysisScope(**values)


def test_scope_intersection_emits_unavailable_intervals_and_ordered_partition_predicates():
    requested = make_scope(start=date(2025, 7, 1), end=date(2026, 2, 1))
    approved = make_scope(start=date(2025, 1, 1), end=date(2026, 12, 31))
    window = SourceAvailabilityWindow(date(2025, 7, 15), date(2026, 1, 15))

    plan = resolve_source_scope(requested, approved, source_window=window)

    assert plan.effective_scope is not None
    assert plan.effective_scope.start_date == date(2025, 7, 15)
    assert plan.effective_scope.end_date == date(2026, 1, 15)
    assert plan.unavailable_intervals == (
        "2025-07-01..2025-07-14",
        "2026-01-16..2026-02-01",
    )
    assert plan.where_clause.index("year = '2025'") < plan.where_clause.index("accountid IN")
    assert plan.where_clause.index("accountid IN") < plan.where_clause.index("region IN")
    assert plan.where_clause.index("region IN") < plan.where_clause.index("modelid IN")
    assert plan.where_clause.index("modelid IN") < plan.where_clause.index("operation IN")
    assert plan.workgroup == ATHENA_WORKGROUP
    assert plan.source_view == SOURCE_VIEW
    assert plan.run_metadata["requested_scope"] == requested.to_dict()
    assert plan.run_metadata["effective_scope"] == plan.effective_scope.to_dict()


def test_month_and_year_boundary_partition_predicates_are_complete_and_first():
    scope = make_scope(
        start=date(2025, 12, 31),
        end=date(2026, 1, 2),
        accounts=("111111111111",),
        regions=("us-east-1",),
        models=("amazon.nova-pro-v1:0",),
        operations=("InvokeModel",),
    )

    plan = resolve_source_scope(scope, scope, source_window=SourceAvailabilityWindow(
        date(2025, 12, 1), date(2026, 1, 31)
    ))

    assert plan.partition_predicate == (
        "(year = '2025' AND month = '12' AND day = '31') OR "
        "(year = '2026' AND month = '01' AND day = '01') OR "
        "(year = '2026' AND month = '01' AND day = '02')"
    )
    assert plan.where_clause.startswith(f"({plan.partition_predicate}) AND ")
    assert plan.where_clause.endswith(
        "accountid IN ('111111111111') AND "
        "region IN ('us-east-1') AND "
        "modelid IN ('amazon.nova-pro-v1:0') AND "
        "operation IN ('InvokeModel')"
    )


def test_default_window_matches_view_six_calendar_month_semantics():
    plan = resolve_source_scope(
        make_scope(start=date(2025, 12, 30), end=date(2026, 6, 30)),
        make_scope(start=date(2025, 1, 1), end=date(2026, 12, 31)),
        as_of=datetime(2026, 6, 30, 23, 59, tzinfo=UTC),
    )

    assert plan.source_window == SourceAvailabilityWindow(date(2025, 12, 30), date(2026, 6, 30))
    assert plan.unavailable_intervals == ()


def test_six_month_source_window_intersects_request_without_widening():
    plan = resolve_source_scope(
        make_scope(start=date(2025, 11, 1), end=date(2026, 7, 31)),
        make_scope(start=date(2025, 1, 1), end=date(2026, 12, 31)),
        as_of=date(2026, 6, 30),
    )

    assert plan.source_window == SourceAvailabilityWindow(date(2025, 12, 30), date(2026, 6, 30))
    assert plan.effective_scope is not None
    assert plan.effective_scope.start_date == date(2025, 12, 30)
    assert plan.effective_scope.end_date == date(2026, 6, 30)
    assert plan.unavailable_intervals == (
        "2025-11-01..2025-12-29",
        "2026-07-01..2026-07-31",
    )


def test_dimension_filters_are_exact_and_follow_partition_predicates():
    requested = make_scope(
        start=date(2026, 1, 1),
        end=date(2026, 1, 1),
        accounts=("111111111111", "222222222222"),
        regions=("us-east-1", "eu-west-1"),
        models=("amazon.nova-pro-v1:0", "anthropic.claude-3-haiku-20240307-v1:0"),
        operations=("InvokeModel", "Converse"),
    )

    plan = resolve_source_scope(
        requested,
        requested,
        source_window=(date(2026, 1, 1), date(2026, 1, 31)),
    )

    assert plan.dimension_predicates == (
        "accountid IN ('111111111111', '222222222222')",
        "region IN ('us-east-1', 'eu-west-1')",
        "modelid IN ('amazon.nova-pro-v1:0', 'anthropic.claude-3-haiku-20240307-v1:0')",
        "operation IN ('InvokeModel', 'Converse')",
    )
    assert plan.where_clause.startswith(
        "((year = '2026' AND month = '01' AND day = '01')) AND "
    )
    assert plan.where_clause.count("accountid IN") == 1
    assert plan.where_clause.count("region IN") == 1
    assert plan.where_clause.count("modelid IN") == 1
    assert plan.where_clause.count("operation IN") == 1


def test_unbounded_requests_are_rejected_before_predicate_generation():
    with pytest.raises(UnboundedSourceScopeError) as error:
        resolve_source_scope(
            make_scope(accounts=(), start=None),
            make_scope(),
            source_window=(date(2025, 1, 1), date(2026, 1, 1)),
        )

    assert "requested_scope.accounts" in error.value.fields
    assert "requested_scope.start_date" in error.value.fields


def test_request_outside_approved_dimensions_or_dates_is_rejected():
    approved = make_scope(
        start=date(2025, 3, 1),
        end=date(2025, 9, 30),
        accounts=("111111111111",),
        regions=("us-east-1",),
    )
    requested = make_scope(
        start=date(2025, 3, 1),
        end=date(2025, 9, 30),
        accounts=("222222222222",),
        regions=("us-east-1",),
    )

    with pytest.raises(ScopeNotApprovedError) as error:
        resolve_source_scope(requested, approved)

    assert "requested_scope.accounts" in error.value.fields


def test_fully_out_of_window_request_is_not_queryable_or_widened():
    requested = make_scope(start=date(2024, 1, 1), end=date(2024, 1, 31))
    approved = make_scope(start=date(2024, 1, 1), end=date(2026, 12, 31))

    plan = resolve_source_scope(
        requested,
        approved,
        source_window=SourceAvailabilityWindow(date(2025, 1, 1), date(2025, 6, 30)),
    )

    assert not plan.queryable
    assert plan.effective_scope is None
    assert plan.partition_predicate == ""
    assert plan.where_clause == ""
    assert plan.unavailable_intervals == ("2024-01-01..2024-01-31",)


from hypothesis import given, settings, strategies as st


@st.composite
def source_scope_window_cases(draw):
    """Generate approved scopes, bounded requests, windows, and candidate rows."""

    account_values = ("111111111111", "222222222222", "333333333333")
    region_values = ("us-east-1", "eu-west-1", "ap-southeast-1")
    model_values = (
        "amazon.nova-pro-v1:0",
        "anthropic.claude-3-haiku-20240307-v1:0",
        "amazon.titan-text-express-v1",
    )
    operation_values = ("InvokeModel", "Converse", "InvokeModelWithResponseStream")

    def selected(values):
        return tuple(
            draw(st.lists(st.sampled_from(values), min_size=1, max_size=len(values), unique=True))
        )

    approved_start = date(2025, 1, 1)
    approved_end = date(2025, 12, 31)
    request_start = draw(st.dates(min_value=approved_start, max_value=date(2025, 12, 20)))
    request_end = min(
        request_start + timedelta(days=draw(st.integers(min_value=0, max_value=10))),
        approved_end,
    )
    window_start = draw(st.dates(min_value=approved_start, max_value=date(2025, 12, 20)))
    window_end = min(
        window_start + timedelta(days=draw(st.integers(min_value=0, max_value=10))),
        approved_end,
    )
    requested = AnalysisScope(
        accounts=selected(account_values),
        regions=selected(region_values),
        models=selected(model_values),
        operations=selected(operation_values),
        start_date=request_start,
        end_date=request_end,
    )
    approved = AnalysisScope(
        accounts=account_values,
        regions=region_values,
        models=model_values,
        operations=operation_values,
        start_date=approved_start,
        end_date=approved_end,
    )
    candidate = {
        "account": draw(st.sampled_from(account_values)),
        "region": draw(st.sampled_from(region_values)),
        "model": draw(st.sampled_from(model_values)),
        "operation": draw(st.sampled_from(operation_values)),
        "date": draw(st.dates(min_value=approved_start, max_value=approved_end)),
    }
    return requested, approved, SourceAvailabilityWindow(window_start, window_end), candidate


# Feature: bedrock-usage-insights, Property 2: For all normalized invocation records and approved analysis scopes, the source adapter SHALL include a record if and only if its account, Region, model, operation, and UTC date match the configured scope and its date is within the available `bedrock_invocations_view` window; every requested date outside that window SHALL be represented as unavailable rather than queried or silently widened.
@given(source_scope_window_cases())
@settings(max_examples=100, deadline=None)
def test_property_2_source_scope_inclusion_requires_dimensions_and_available_date(
    case,
):
    requested, approved, source_window, candidate = case

    plan = resolve_source_scope(requested, approved, source_window=source_window)
    candidate_matches_requested = (
        candidate["account"] in requested.accounts
        and candidate["region"] in requested.regions
        and candidate["model"] in requested.models
        and candidate["operation"] in requested.operations
        and requested.start_date <= candidate["date"] <= requested.end_date
    )
    candidate_matches_source = source_window.start_date <= candidate["date"] <= source_window.end_date
    expected_inclusion = candidate_matches_requested and candidate_matches_source

    actual_inclusion = bool(
        plan.effective_scope
        and candidate["account"] in plan.effective_scope.accounts
        and candidate["region"] in plan.effective_scope.regions
        and candidate["model"] in plan.effective_scope.models
        and candidate["operation"] in plan.effective_scope.operations
        and plan.effective_scope.start_date <= candidate["date"] <= plan.effective_scope.end_date
    )
    assert actual_inclusion is expected_inclusion

    overlap_start = max(requested.start_date, source_window.start_date)
    overlap_end = min(requested.end_date, source_window.end_date)
    if expected_inclusion or overlap_start <= overlap_end:
        assert plan.effective_scope is not None
        assert plan.effective_scope.accounts == requested.accounts
        assert plan.effective_scope.regions == requested.regions
        assert plan.effective_scope.models == requested.models
        assert plan.effective_scope.operations == requested.operations
        assert plan.effective_scope.start_date == overlap_start
        assert plan.effective_scope.end_date == overlap_end
        current = overlap_start
        while current <= overlap_end:
            assert (
                f"year = '{current.year:04d}'"
                f" AND month = '{current.month:02d}'"
                f" AND day = '{current.day:02d}'"
            ) in plan.partition_predicate
            current += timedelta(days=1)
        assert plan.where_clause.startswith(f"({plan.partition_predicate}) AND ")
    else:
        assert not plan.queryable
        assert plan.effective_scope is None
        assert plan.partition_predicate == ""
        assert plan.dimension_predicates == ()
        assert plan.where_clause == ""

    assert plan.run_metadata["requested_scope"] == requested.to_dict()
    assert plan.run_metadata["source_availability_window"] == {
        "start_date": source_window.start_date.isoformat(),
        "end_date": source_window.end_date.isoformat(),
    }

    expected_unavailable = []
    if requested.start_date < source_window.start_date:
        expected_unavailable.append(
            f"{requested.start_date.isoformat()}.."
            f"{min(requested.end_date, source_window.start_date - timedelta(days=1)).isoformat()}"
        )
    if requested.end_date > source_window.end_date:
        expected_unavailable.append(
            f"{max(requested.start_date, source_window.end_date + timedelta(days=1)).isoformat()}.."
            f"{requested.end_date.isoformat()}"
        )
    assert plan.unavailable_intervals == tuple(expected_unavailable)


# Feature: bedrock-usage-insights, Property 2: For all normalized invocation records and approved analysis scopes, the source adapter SHALL include a record if and only if its account, Region, model, operation, and UTC date match the configured scope and its date is within the available `bedrock_invocations_view` window; every requested date outside that window SHALL be represented as unavailable rather than queried or silently widened.
@given(source_scope_window_cases())
@settings(max_examples=100, deadline=None)
def test_property_2_out_of_window_dates_are_unavailable_and_never_widened(case):
    requested, approved, source_window, _ = case
    plan = resolve_source_scope(requested, approved, source_window=source_window)

    overlap_start = max(requested.start_date, source_window.start_date)
    overlap_end = min(requested.end_date, source_window.end_date)
    if overlap_start > overlap_end:
        assert plan.queryable is False
        assert plan.effective_scope is None
        assert plan.unavailable_intervals
        assert plan.partition_predicate == ""
        assert plan.dimension_predicates == ()
        assert plan.where_clause == ""
        assert plan.run_metadata["queryable"] is False
        assert plan.run_metadata["effective_scope"] is None
    else:
        assert plan.effective_scope is not None
        assert plan.effective_scope.start_date == overlap_start
        assert plan.effective_scope.end_date == overlap_end
        assert plan.effective_scope.start_date >= source_window.start_date
        assert plan.effective_scope.end_date <= source_window.end_date
        assert plan.queryable is True
