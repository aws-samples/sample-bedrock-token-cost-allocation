"""Static safety checks for the optional Bedrock Usage Insights stack."""

from pathlib import Path

from test_cfn_properties import load_cfn_template


REPO_ROOT = Path(__file__).parent.parent


FEATURE_RESOURCE_TYPES = {
    "AWS::CloudWatch::Alarm",
    "AWS::DynamoDB::Table",
    "AWS::IAM::Role",
    "AWS::KMS::Alias",
    "AWS::KMS::Key",
    "AWS::Logs::LogGroup",
    "AWS::Logs::MetricFilter",
    "AWS::SNS::Topic",
    "AWS::S3::Bucket",
    "AWS::S3::BucketPolicy",
    "AWS::SSM::Parameter",
}


def feature_template():
    return load_cfn_template(REPO_ROOT / "bedrock-usage-insights.yaml")


def _all_policy_statements(template, resource_name):
    resource = template["Resources"][resource_name]
    statements = []
    for policy in resource.get("Properties", {}).get("Policies", []):
        statements.extend(policy.get("PolicyDocument", {}).get("Statement", []))
    return statements


def test_feature_defaults_are_disabled_and_privacy_conservative():
    template = feature_template()
    parameters = template["Parameters"]

    assert parameters["FeatureEnabled"]["Default"] == "false"
    assert parameters["ContentAnalysisEnabled"]["Default"] == "false"
    assert parameters["DashboardPublicationMode"]["Default"] == "aggregate_only"
    assert parameters["MinimumGroupSize"]["Default"] == 10
    assert parameters["MinimumGroupSize"]["MinValue"] == 5
    assert parameters["DerivedDataRetentionDays"]["Default"] == 30
    assert parameters["DerivedDataRetentionDays"]["MaxValue"] == 30


def test_feature_owns_only_additive_resources():
    template = feature_template()
    resources = template["Resources"]

    assert all(resource["Type"] in FEATURE_RESOURCE_TYPES for resource in resources.values())
    for prohibited in (
        "BedrockLogsGlueDatabase",
        "BedrockLogsGlueTable",
        "BedrockInvocationsView",
        "FirehoseDeliveryStream",
        "CWLFirehoseDestination",
        "BedrockQuickSightDataSourceRole",
        "BedrockAthenaWorkGroup",
    ):
        assert prohibited not in resources

    assert resources["InsightsContentRole"]["Condition"] == "ContentAnalysisEnabledCondition"
    assert resources["InsightsDashboardRole"]["Properties"]["Tags"][-1] == {
        "Key": "Exposure",
        "Value": "aggregate-only",
    }


def test_feature_storage_is_encrypted_versioned_private_and_retained():
    template = feature_template()
    resources = template["Resources"]

    assert resources["InsightsKmsKey"]["Properties"]["EnableKeyRotation"] is True
    for name in ("InsightsDataBucket", "InsightsAccessLogBucket"):
        bucket = resources[name]
        properties = bucket["Properties"]
        assert bucket["DeletionPolicy"] == "Retain"
        assert bucket["UpdateReplacePolicy"] == "Retain"
        assert properties["VersioningConfiguration"]["Status"] == "Enabled"
        assert properties["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        assert "LifecycleConfiguration" in properties

    encryption = resources["InsightsDataBucket"]["Properties"]["BucketEncryption"]
    default_encryption = encryption["ServerSideEncryptionConfiguration"][0][
        "ServerSideEncryptionByDefault"
    ]
    assert default_encryption["SSEAlgorithm"] == "aws:kms"
    assert resources["InsightsDataBucket"]["Properties"]["LoggingConfiguration"][
        "DestinationBucketName"
    ] == {"Ref": "InsightsAccessLogBucket"}

    for policy_name in ("InsightsDataBucketPolicy", "InsightsAccessLogBucketPolicy"):
        statements = resources[policy_name]["Properties"]["PolicyDocument"]["Statement"]
        deny_https = next(statement for statement in statements if statement["Sid"] == "DenyNonHTTPS")
        assert deny_https["Condition"]["Bool"]["aws:SecureTransport"] is False


def test_configuration_and_run_stores_are_feature_owned_and_kms_encrypted():
    template = feature_template()
    resources = template["Resources"]

    for name, store_key in (
        ("InsightConfigurationTable", "configuration"),
        ("AnalysisRunTable", "run-control"),
    ):
        table = resources[name]
        assert table["DeletionPolicy"] == "Retain"
        properties = table["Properties"]
        assert properties["BillingMode"] == "PAY_PER_REQUEST"
        assert properties["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
        assert properties["SSESpecification"]["SSEType"] == "KMS"
        assert properties["SSESpecification"]["KMSMasterKeyId"] == {"Fn::GetAtt": "InsightsKmsKey.Arn"}
        assert {tag["Value"] for tag in properties["Tags"] if tag["Key"] == "Store"} == {store_key}

    parameter_value = resources["InsightsDisabledConfiguration"]["Properties"]["Value"]
    assert "bootstrap-disabled-v1" in parameter_value["Fn::Sub"]
    assert "content_analysis_enabled" in parameter_value["Fn::Sub"]
    assert "minimum_group_size" in parameter_value["Fn::Sub"]


def test_roles_are_separated_and_source_access_is_read_only():
    template = feature_template()
    resources = template["Resources"]

    assert resources["InsightsAnalysisRole"]["Properties"]["RoleName"]["Fn::Sub"].startswith(
        "bedrock-usage-insights-analysis-"
    )
    assert resources["InsightsDashboardRole"]["Properties"]["RoleName"]["Fn::Sub"].startswith(
        "bedrock-usage-insights-dashboard-"
    )

    analysis_actions = {
        action
        for statement in _all_policy_statements(template, "InsightsAnalysisRole")
        for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    }
    assert "glue:GetTable" in analysis_actions
    assert "s3:GetObject" in analysis_actions
    assert "s3:PutObject" in analysis_actions
    assert "s3:DeleteObject" not in analysis_actions

    dashboard_actions = {
        action
        for statement in _all_policy_statements(template, "InsightsDashboardRole")
        for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    }
    assert "s3:GetObject" in dashboard_actions
    assert "s3:PutObject" not in dashboard_actions
    assert "s3:DeleteObject" not in dashboard_actions

    source_read_statement = next(
        statement
        for statement in _all_policy_statements(template, "InsightsAnalysisRole")
        if statement.get("Sid") == "ReadExistingDataLake"
    )
    assert "s3:PutObject" not in source_read_statement["Action"]
    assert "s3:DeleteObject" not in source_read_statement["Action"]


def test_operational_observability_is_feature_owned():
    template = feature_template()
    resources = template["Resources"]

    assert resources["InsightsLogGroup"]["Properties"]["RetentionInDays"] == {"Ref": "DerivedDataRetentionDays"}
    metric_filters = [
        name for name, resource in resources.items() if resource["Type"] == "AWS::Logs::MetricFilter"
    ]
    assert {"InsightsErrorMetricFilter", "InsightsUnknownMetricFilter", "InsightsPublicationFailureMetricFilter"}.issubset(
        metric_filters
    )

    alarms = [name for name, resource in resources.items() if resource["Type"] == "AWS::CloudWatch::Alarm"]
    assert {"InsightsErrorAlarm", "InsightsUnknownRateAlarm", "InsightsPublicationFailureAlarm"}.issubset(alarms)
    for alarm_name in alarms:
        assert resources[alarm_name]["Properties"]["AlarmActions"] == [{"Ref": "InsightsAlarmTopic"}]
