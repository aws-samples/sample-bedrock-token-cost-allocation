"""
Behavioural tests for the bedrock-cwl-unpack Lambda processor.

The handler source is extracted from the CloudFormation template rather than
duplicated here, so these tests always exercise the code that actually deploys.

The handler sits between the CloudWatch Logs destination and Firehose's
JSON-to-Parquet conversion. Anything it emits must be a JSON object matching the
Glue schema; emitting plain text causes Firehose to fail the whole batch with
DataFormatConversion.ParseError and write it to the errors/ prefix instead.
"""

import base64
import gzip
import json
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).parent.parent


class _CFNLoader(yaml.SafeLoader):
    """Minimal loader that tolerates CloudFormation intrinsic tags."""


def _intrinsic(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


for _tag in (
    "Ref", "GetAtt", "Sub", "Join", "Select", "Split", "GetAZs",
    "ImportValue", "Condition", "If", "And", "Or", "Not", "Equals",
    "FindInMap", "Base64", "Cidr", "Transform",
):
    _CFNLoader.add_constructor(f"!{_tag}", _intrinsic)


@pytest.fixture(scope="module")
def handler():
    """Extract and compile the handler from the template's inline ZipFile."""
    template = yaml.load(
        (REPO_ROOT / "bedrock-firehose-data-lake.yaml").read_text(),
        Loader=_CFNLoader,
    )
    source = template["Resources"]["CWLUnpackFunction"]["Properties"]["Code"]["ZipFile"]
    namespace: dict = {}
    exec(compile(source, "<CWLUnpackFunction>", "exec"), namespace)
    return namespace["handler"]


# ---------------------------------------------------------------------------
# Helpers to build realistic CloudWatch Logs subscription payloads
# ---------------------------------------------------------------------------

def make_record(messages, record_id="r1", message_type="DATA_MESSAGE"):
    """Wrap log messages in a gzip+base64 CloudWatch Logs envelope."""
    envelope = {
        "messageType": message_type,
        "owner": "123456789012",
        "logGroup": "/aws/bedrock/modelinvocations",
        "logStream": "aws/bedrock/modelinvocations",
        "logEvents": [
            {"id": str(i), "timestamp": 1787754906000, "message": m}
            for i, m in enumerate(messages)
        ],
    }
    packed = gzip.compress(json.dumps(envelope).encode())
    return {"recordId": record_id, "data": base64.b64encode(packed).decode()}


def decode_output(record):
    """Decode an emitted record back into a list of JSON objects."""
    raw = base64.b64decode(record["data"]).decode()
    return [json.loads(line) for line in raw.splitlines() if line]


BEDROCK_LOG = json.dumps({
    "timestamp": "2026-08-26T14:35:06Z",
    "accountId": "123456789012",
    "region": "us-east-1",
    "requestId": "9e7919c8-d0f3-4c8d-971a-51ec26b490a5",
    "operation": "InvokeModel",
    "modelId": "amazon.nova-pro-v1:0",
    "identity": {"arn": "arn:aws:sts::123456789012:assumed-role/ExampleRole/example-session"},
    "input": {"inputTokenCount": 4},
    "output": {"outputTokenCount": 10},
})

# CloudWatch Logs writes this plain-text notice when Bedrock logging is enabled.
PERMISSIONS_NOTICE = "Permissions are correctly set for Amazon Bedrock logs."


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_valid_bedrock_log_is_emitted_with_flattened_identity(handler):
    """A well-formed Bedrock log passes through and gains identity_arn."""
    result = handler({"records": [make_record([BEDROCK_LOG])]}, None)

    assert len(result["records"]) == 1
    record = result["records"][0]
    assert record["result"] == "Ok"

    events = decode_output(record)
    assert len(events) == 1
    assert events[0]["identity_arn"] == (
        "arn:aws:sts::123456789012:assumed-role/ExampleRole/example-session"
    )
    assert events[0]["modelId"] == "amazon.nova-pro-v1:0"


def test_plain_text_notice_is_dropped_not_forwarded(handler):
    """
    Regression: the CloudWatch permissions notice must never reach Firehose.

    Forwarding it produced DataFormatConversion.ParseError:
    "Unrecognized token 'Permissions'".
    """
    result = handler({"records": [make_record([PERMISSIONS_NOTICE])]}, None)

    assert result["records"][0]["result"] == "Dropped", (
        "plain-text log messages must be dropped, not forwarded to Parquet conversion"
    )


def test_mixed_batch_keeps_json_and_discards_plain_text(handler):
    """A batch containing both kinds emits only the JSON events."""
    result = handler(
        {"records": [make_record([PERMISSIONS_NOTICE, BEDROCK_LOG, "another plain line"])]},
        None,
    )

    record = result["records"][0]
    assert record["result"] == "Ok"

    events = decode_output(record)
    assert len(events) == 1
    assert events[0]["requestId"] == "9e7919c8-d0f3-4c8d-971a-51ec26b490a5"


def test_control_message_is_dropped(handler):
    """CloudWatch Logs subscription heartbeats carry no data."""
    result = handler(
        {"records": [make_record(["CWL CONTROL MESSAGE"], message_type="CONTROL_MESSAGE")]},
        None,
    )
    assert result["records"][0]["result"] == "Dropped"


def test_non_object_json_is_dropped(handler):
    """
    A bare JSON scalar parses successfully but has no schema fields.

    Previously this raised TypeError on item assignment and fell through to the
    plain-text path, forwarding an unusable record.
    """
    result = handler({"records": [make_record(['"just a string"', "42"])]}, None)
    assert result["records"][0]["result"] == "Dropped"


def test_missing_or_null_identity_yields_empty_identity_arn(handler):
    """identity may be absent or null; neither should raise."""
    without_identity = json.dumps({"requestId": "a", "modelId": "m"})
    null_identity = json.dumps({"requestId": "b", "modelId": "m", "identity": None})

    result = handler({"records": [make_record([without_identity, null_identity])]}, None)

    events = decode_output(result["records"][0])
    assert len(events) == 2
    assert all(e["identity_arn"] == "" for e in events)


def test_every_emitted_line_is_a_json_object(handler):
    """
    The core invariant: Firehose's Parquet conversion only accepts JSON objects.

    Any emitted line that is not a JSON object fails the entire batch.
    """
    messages = [
        BEDROCK_LOG,
        PERMISSIONS_NOTICE,
        "",
        "not json at all",
        "{unclosed",
        '["an", "array"]',
        json.dumps({"requestId": "c", "identity": {"arn": "arn:aws:iam::1:role/r"}}),
    ]
    result = handler({"records": [make_record(messages)]}, None)

    record = result["records"][0]
    if record["result"] == "Dropped":
        pytest.fail("valid JSON objects in the batch should still be emitted")

    for event in decode_output(record):
        assert isinstance(event, dict), f"emitted non-object payload: {event!r}"
        assert "identity_arn" in event


def test_record_ids_are_preserved(handler):
    """Firehose matches responses to inputs by recordId."""
    records = [
        make_record([BEDROCK_LOG], record_id="id-a"),
        make_record([PERMISSIONS_NOTICE], record_id="id-b"),
    ]
    result = handler({"records": records}, None)

    assert [r["recordId"] for r in result["records"]] == ["id-a", "id-b"]
