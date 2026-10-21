"""Contract tests for the read-only Athena source adapter."""

from datetime import date

import pytest

from bedrock_usage_insights.models import ATHENA_WORKGROUP, SOURCE_VIEW, AnalysisScope
from bedrock_usage_insights.source_access import (
    AthenaSourceReader,
    ContentAccessNotApprovedError,
    DEFAULT_SOURCE_SELECT_FIELDS,
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


class PollingAthenaClient(FakeAthenaClient):
    def __init__(self, statuses, response=None, errors=()):
        super().__init__(response=response)
        self.statuses = list(statuses)
        self.errors = list(errors)
        self.poll_requests = []

    def get_query_execution(self, **kwargs):
        self.poll_requests.append(kwargs)
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        if not self.statuses:
            raise AssertionError("poll status sequence exhausted")
        return self.statuses.pop(0)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


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


def test_query_preserves_all_dimension_filters_across_year_boundary():
    client = FakeAthenaClient()
    scope = make_scope(
        start=date(2025, 12, 31),
        end=date(2026, 1, 1),
        accounts=("111111111111", "222222222222"),
        regions=("us-east-1", "eu-west-1"),
        models=("amazon.nova-pro-v1:0", "anthropic.claude-3-haiku-20240307-v1:0"),
        operations=("InvokeModel", "Converse"),
    )
    reader = AthenaSourceReader(
        client,
        scope,
        source_window=SourceAvailabilityWindow(date(2025, 12, 1), date(2026, 1, 31)),
    )

    result = reader.read(scope, "run-1", "config-1")

    request = client.requests[0]
    query = request["QueryString"]
    assert result.query_execution_ids == ("query-1",)
    assert request["WorkGroup"] == "bedrock-analytics"
    assert "year = '2025' AND month = '12' AND day = '31'" in query
    assert "year = '2026' AND month = '01' AND day = '01'" in query
    assert "accountid IN ('111111111111', '222222222222')" in query
    assert "region IN ('us-east-1', 'eu-west-1')" in query
    assert "modelid IN ('amazon.nova-pro-v1:0', 'anthropic.claude-3-haiku-20240307-v1:0')" in query
    assert "operation IN ('InvokeModel', 'Converse')" in query
    assert query.index("year = '2025'") < query.index("accountid IN")


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


def test_default_projection_is_exact_and_never_calls_source_write_apis():
    client = FakeAthenaClient()

    result = make_reader(client).read(make_scope(), "run-1", "config-1")

    assert tuple(result.metadata["selected_fields"]) == DEFAULT_SOURCE_SELECT_FIELDS
    assert "prompt_preview" not in result.metadata["selected_fields"]
    assert "response_preview" not in result.metadata["selected_fields"]
    assert len(client.requests) == 1
    assert all(not name.startswith(("CREATE", "ALTER", "DROP", "INSERT", "UPDATE"))
               for name in client.requests[0]["QueryString"].split())


def test_disabled_reader_returns_without_submitting_or_loading():
    client = FakeAthenaClient()
    loader = FakeResultLoader(SourceReadResult(rows=({"source_request_id": "source-1"},)))
    reader = make_reader(client, result_loader=loader, enabled=False)

    result = reader.read(make_scope(), "run-disabled", "config-1")

    assert client.requests == []
    assert loader.query_ids == []
    assert result.rows == ()
    assert result.query_execution_ids == ()
    assert result.errors == ()
    assert result.metadata == {
        "enabled": False,
        "query_submitted": False,
        "source_availability": "disabled",
        "analysis_run_id": "run-disabled",
        "configuration_version": "config-1",
    }


def test_query_result_failure_is_bounded_and_content_free():
    class FailingLoader(FakeResultLoader):
        def load(self, query_execution_id):
            self.query_ids.append(query_execution_id)
            raise RuntimeError("secret prompt and response")

    client = FakeAthenaClient()
    loader = FailingLoader(SourceReadResult())
    reader = make_reader(client, result_loader=loader)

    result = reader.read(make_scope(), "run-result-failure", "config-1")

    assert result.errors == ("SOURCE_QUERY_RESULT_FAILED",)
    assert result.query_execution_ids == ("query-1",)
    assert "secret" not in str(result.metadata)
    assert result.metadata["query_submitted"] is True


def test_scan_cutoff_statistics_are_preserved_without_publishing_rows():
    client = FakeAthenaClient(
        response={
            "QueryExecutionId": "query-scan-cutoff",
            "QueryExecution": {"Statistics": {"DataScannedInBytes": 2048}},
        }
    )
    loader = FakeResultLoader(
        SourceReadResult(
            scanned_bytes=4096,
            errors=("ATHENA_SCAN_CUTOFF",),
            metadata={"scan_cutoff_bytes": 4096},
        )
    )
    reader = make_reader(client, result_loader=loader)

    result = reader.read(make_scope(), "run-scan-cutoff", "config-1")

    assert result.rows == ()
    assert result.errors == ("ATHENA_SCAN_CUTOFF",)
    assert result.scanned_bytes == 4096
    assert result.metadata["scan_cutoff_bytes"] == 4096


def test_repeated_polling_for_same_run_is_idempotent():
    client = FakeAthenaClient()
    loader = FakeResultLoader(
        SourceReadResult(rows=({"source_request_id": "request-1"},), scanned_bytes=128)
    )
    reader = make_reader(client, result_loader=loader)

    first = reader.read(make_scope(), "run-retry", "config-1")
    second = reader.read(make_scope(), "run-retry", "config-1")

    assert second == first
    assert len(client.requests) == 1
    assert loader.query_ids == ["query-1"]


def query_status(state, scanned_bytes=0):
    return {
        "QueryExecution": {
            "Status": {"State": state},
            "Statistics": {"DataScannedInBytes": scanned_bytes},
        }
    }


def test_query_is_polled_until_success_and_only_complete_results_are_publishable():
    client = PollingAthenaClient(
        [query_status("QUEUED"), query_status("RUNNING", 2048), query_status("SUCCEEDED", 4096)]
    )
    loader = FakeResultLoader(SourceReadResult(rows=({"source_request_id": "source-1"},)))
    clock = FakeClock()
    reader = AthenaSourceReader(
        client,
        make_scope(),
        source_window=WINDOW,
        result_loader=loader,
        poll_interval_seconds=1,
        timeout_seconds=5,
        sleep_fn=clock.sleep,
        clock=clock,
    )

    result = reader.read(make_scope(), "run-poll", "config-1")

    assert [request["QueryExecutionId"] for request in client.poll_requests] == [
        "query-1",
        "query-1",
        "query-1",
    ]
    assert result.query_execution_ids == ("query-1",)
    assert result.rows == ({"source_request_id": "source-1"},)
    assert result.scanned_bytes == 4096
    assert result.errors == ()
    assert result.metadata["query_state"] == "SUCCEEDED"
    assert result.metadata["source_set_determined"] is True
    assert result.metadata["publication_allowed"] is True
    assert len(client.requests[0]["ClientRequestToken"]) == 64


def test_running_query_times_out_without_rows_or_publication():
    client = PollingAthenaClient([query_status("RUNNING")] * 4)
    loader = FakeResultLoader(SourceReadResult(rows=({"source_request_id": "source-1"},)))
    clock = FakeClock()
    reader = AthenaSourceReader(
        client,
        make_scope(),
        source_window=WINDOW,
        result_loader=loader,
        poll_interval_seconds=1,
        timeout_seconds=2,
        sleep_fn=clock.sleep,
        clock=clock,
    )

    result = reader.read(make_scope(), "run-timeout", "config-1")

    assert result.errors == ("SOURCE_QUERY_TIMEOUT",)
    assert result.rows == ()
    assert result.metadata["query_state"] == "TIMEOUT"
    assert result.metadata["source_set_determined"] is False
    assert result.metadata["publication_allowed"] is False
    assert loader.query_ids == []


def test_query_failure_maps_to_content_free_diagnostic_and_preserves_scan_bytes():
    client = PollingAthenaClient([query_status("FAILED", 8192)])
    reader = AthenaSourceReader(client, make_scope(), source_window=WINDOW)

    result = reader.read(make_scope(), "run-failed", "config-1")

    assert result.errors == ("SOURCE_QUERY_FAILED",)
    assert result.query_execution_ids == ("query-1",)
    assert result.scanned_bytes == 8192
    assert result.metadata["publication_allowed"] is False
    assert "secret" not in str(result.metadata)


def test_poll_retry_is_bounded_and_diagnostics_exclude_exception_text():
    client = PollingAthenaClient(
        [query_status("SUCCEEDED")],
        errors=[RuntimeError("secret prompt"), None],
    )
    loader = FakeResultLoader(SourceReadResult(rows=({"source_request_id": "source-1"},)))
    sleeps = []
    reader = AthenaSourceReader(
        client,
        make_scope(),
        source_window=WINDOW,
        result_loader=loader,
        max_retries=1,
        retry_backoff_seconds=0.5,
        sleep_fn=sleeps.append,
    )

    result = reader.read(make_scope(), "run-retry", "config-1")

    assert result.errors == ()
    assert result.metadata["poll_retries"] == 1
    assert sleeps == [0.5]
    assert "secret" not in str(result.metadata)


def test_partial_result_loader_failure_drops_rows_and_blocks_publication():
    client = FakeAthenaClient()
    loader = FakeResultLoader(
        SourceReadResult(
            rows=({"source_request_id": "source-1"},),
            errors=("secret prompt leaked",),
            metadata={"page_count": 2, "diagnostic": "secret response"},
        )
    )
    reader = AthenaSourceReader(client, make_scope(), source_window=WINDOW, result_loader=loader)

    result = reader.read(make_scope(), "run-partial", "config-1")

    assert result.rows == ()
    assert result.errors == ("SOURCE_QUERY_RESULT_FAILED",)
    assert result.metadata["page_count"] == 2
    assert "secret" not in str(result.metadata)
    assert result.metadata["publication_allowed"] is False
