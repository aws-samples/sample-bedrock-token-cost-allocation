"""Tests for native CloudWatch Logs processing in Amazon Data Firehose."""

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
def central_template():
    return yaml.load(
        (REPO_ROOT / "bedrock-firehose-data-lake.yaml").read_text(),
        Loader=_CFNLoader,
    )


@pytest.fixture(scope="module")
def source_template():
    return yaml.load(
        (REPO_ROOT / "bedrock-logging.yaml").read_text(),
        Loader=_CFNLoader,
    )


def test_firehose_uses_native_cloudwatch_logs_processors(central_template):
    destination = central_template["Resources"]["FirehoseDeliveryStream"]["Properties"][
        "ExtendedS3DestinationConfiguration"
    ]
    processors = destination["ProcessingConfiguration"]["Processors"]

    assert processors == [
        {
            "Type": "Decompression",
            "Parameters": [
                {"ParameterName": "CompressionFormat", "ParameterValue": "GZIP"}
            ],
        },
        {
            "Type": "CloudWatchLogProcessing",
            "Parameters": [
                {"ParameterName": "DataMessageExtraction", "ParameterValue": "true"}
            ],
        },
    ]


def test_ingestion_path_has_no_lambda_dependency(central_template):
    resource_types = {
        resource["Type"] for resource in central_template["Resources"].values()
    }

    assert "AWS::Lambda::Function" not in resource_types
    assert "AWS::Lambda::Permission" not in resource_types


def test_source_filter_forwards_only_bedrock_invocations(source_template):
    subscription = source_template["Resources"]["BedrockCWLSubscriptionFilter"]

    assert subscription["Properties"]["FilterPattern"] == (
        '{ $.schemaType = "ModelInvocationLog" }'
    )


def test_glue_schema_supports_old_and_native_identity_shapes(central_template):
    table = central_template["Resources"]["BedrockLogsGlueTable"]
    columns = {
        column["Name"]: column["Type"]
        for column in table["Properties"]["TableInput"]["StorageDescriptor"]["Columns"]
    }

    assert columns["identity_arn"] == "string"
    assert columns["identity"] == "struct<arn:string>"

    view = central_template["Resources"]["BedrockInvocationsView"]
    query = view["Properties"]["QueryString"]
    assert "COALESCE(identity_arn, identity.arn) AS identity_arn" in query
