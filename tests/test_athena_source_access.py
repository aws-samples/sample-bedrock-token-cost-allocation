"""Contract tests for the read-only Athena source adapter."""

from datetime import date

import pytest

from bedrock_usage_insights.models import ATHENA_WORKGROUP, SOURCE_VIEW, AnalysisScope
from bedrock_usage_insights.source_access import (
    AthenaSourceReader,
    ContentAccessNotApprovedError,
    SourceAvailabilityWindow,
    SourceReadResult,
)


class FakeAthenaClient:
    def __init__(self, response=None, error=None):
        self.response = response or {"QueryExecutionId": "query-1"}
        self.error = error
        self.requests = []

    def start_query_execution(self, **kwargs):
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeResultLoader:
    def __init__(self, result):
        self.result = result
        self.query_ids = []

    def load(self, query_execution_id):
        self.query_ids.append(query_execution_id)
        return self.result


def make_scope(start=date(2026, 1, 1), end=date(2026, 1, 2), **changes):
    values = {
        "accounts": ("111111111111",),
        "regions": ("us-east-1",),
        "models": ("amazon.nova-pro-v1:0",),
        "operations": ("InvokeModel",),
        "start_date": start,
        "end_date": end,
    }
    values.update(changes)
    return AnalysisScope(**values)


WINDOW = SourceAvailabilityWindow(date(2026, 1, 1), date(2026, 1, 31))


def make_reader(client, **changes):
    return AthenaSourceReader(
        client,
        make_scope(),
        source_window=WINDOW,
        **changes,
    )


def test_default_read_submits_bounded_read_only_query_with_workgroup_enforcement():
    client = FakeAthenaClient(
        response={
            "QueryExecutionId": "query-1",
            "QueryExecution": {"Statistics": {"DataScannedInBytes": 2048}},
        }
    )

    result = make_reader(client).read(make_scope(), "run-1", "config-1")

    request = client.requests[0]
    query = request["QueryString"]
    assert request["WorkGroup"] == ATHENA_WORKGROUP
    assert request["QueryExecutionContext"] == {"Database": "bedrock_logs"}
    assert "ResultConfiguration" not in request
    assert f'FROM "bedrock_logs"."{SOURCE_VIEW.rsplit(".", 1)[1]}"' in query
    assert "SELECT" in query and "CREATE" not in query and "ALTER" not in query
    assert "year = '2026'" in query and "month = '01'" in query and "day = '01'" in query
    assert "accountid IN ('111111111111')" in query
    assert "prompt_preview" not in query
    assert "response_preview" not in query
    assert result.query_execution_ids == ("query-1",)
    assert result.scanned_bytes == 2048
    assert result.metadata["result_location_enforced_by_workgroup"] is True
    assert result.metadata["result_location_supplied"] is False
    assert result.metadata["source_availability_window"] == {
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
    }


def test_fully_unavailable_source_window_is_reported_without_athena_call():
    client = FakeAthenaClient()
    reader = AthenaSourceReader(
        client,
        make_scope(start=date(2025, 1, 1), end=date(2025, 1, 2)),
        source_window=WINDOW,
    )

    result = reader.read(
        make_scope(start=date(2025, 1, 1), end=date(2025, 1, 2)),
        "run-1",
        "config-1",
    )

    assert client.requests == []
    assert result.query_execution_ids == ()
    assert result.unavailable_intervals == ("2025-01-01..2025-01-02",)
    assert result.metadata["source_availability"] == "unavailable"
    assert result.metadata["query_submitted"] is False


def test_content_fields_require_explicit_authorization_and_are_minimal_when_allowed():
    client = FakeAthenaClient()
    with pytest.raises(ContentAccessNotApprovedError):
        make_reader(client).read(make_scope(), "run-1", "config-1", include_content=True)
    assert client.requests == []

    authorized = make_reader(client, content_access_approved=True)
    result = authorized.read(make_scope(), "run-1", "config-1", include_content=True)
    query = client.requests[0]["QueryString"]
    assert "prompt_preview" in query
    assert "response_preview" in query
    assert result.metadata["include_content"] is True
    assert result.metadata["selected_fields"][-2:] == ["prompt_preview", "response_preview"]


def test_optional_result_loader_preserves_rows_and_execution_tracking():
    client = FakeAthenaClient()
    loader = FakeResultLoader(
        SourceReadResult(
            rows=({"source_request_id": "request-1"},),
            scanned_bytes=4096,
            metadata={"page_count": 1},
        )
    )
    reader = AthenaSourceReader(
        client,
        make_scope(),
        source_window=WINDOW,
        result_loader=loader,
    )

    result = reader.read(make_scope(), "run-1", "config-1")

    assert loader.query_ids == ["query-1"]
    assert result.rows == ({"source_request_id": "request-1"},)
    assert result.query_execution_ids == ("query-1",)
    assert result.scanned_bytes == 4096
    assert result.metadata["page_count"] == 1


def test_submission_failure_returns_bounded_content_free_error():
    client = FakeAthenaClient(error=RuntimeError("secret request body"))

    result = make_reader(client).read(make_scope(), "run-1", "config-1")

    assert result.errors == ("SOURCE_QUERY_SUBMISSION_FAILED",)
    assert "secret" not in str(result.metadata)
