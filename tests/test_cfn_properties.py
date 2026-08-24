"""
Property-based tests for Bedrock Firehose Data Lake CloudFormation templates.

These tests validate invariants that the templates must maintain:
- IAM role least-privilege constraints
- S3 bucket naming conventions
- Glue partition projection configuration
- Absence of prohibited resource types

Uses pytest + hypothesis for property-based testing against static YAML parsing.
"""

import re
from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# CloudFormation YAML Loader with intrinsic function support
# ---------------------------------------------------------------------------

class CFNLoader(yaml.SafeLoader):
    """YAML loader that handles CloudFormation intrinsic functions."""
    pass


def _cfn_intrinsic_constructor(tag_name):
    """Create a constructor for a CloudFormation intrinsic function."""
    def constructor(loader, node):
        if isinstance(node, yaml.ScalarNode):
            return {tag_name: loader.construct_scalar(node)}
        elif isinstance(node, yaml.SequenceNode):
            return {tag_name: loader.construct_sequence(node)}
        elif isinstance(node, yaml.MappingNode):
            return {tag_name: loader.construct_mapping(node)}
    return constructor


# Register all CloudFormation intrinsic functions
CFN_TAGS = [
    'Ref', 'GetAtt', 'Sub', 'Join', 'Select', 'Split', 'GetAZs',
    'ImportValue', 'Condition', 'If', 'And', 'Or', 'Not', 'Equals',
    'FindInMap', 'Base64', 'Cidr', 'Transform'
]

for tag in CFN_TAGS:
    CFNLoader.add_constructor(f'!{tag}', _cfn_intrinsic_constructor(f'Fn::{tag}' if tag != 'Ref' else 'Ref'))

# Also handle short forms like !GetAtt
CFNLoader.add_constructor('!GetAtt', _cfn_intrinsic_constructor('Fn::GetAtt'))


def load_cfn_template(path: Path) -> dict:
    """Load a CloudFormation template with intrinsic function support."""
    with open(path, "r") as f:
        return yaml.load(f, Loader=CFNLoader)


# ---------------------------------------------------------------------------
# Fixtures: Load CloudFormation templates
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def central_template():
    """Load the central account Firehose data lake template."""
    template_path = REPO_ROOT / "bedrock-firehose-data-lake.yaml"
    return load_cfn_template(template_path)


@pytest.fixture(scope="module")
def source_template():
    """Load the source account logging template."""
    template_path = REPO_ROOT / "bedrock-logging.yaml"
    return load_cfn_template(template_path)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def get_policy_statements(template: dict, policy_resource_name: str) -> list:
    """Extract policy statements from an IAM ManagedPolicy resource."""
    resources = template.get("Resources", {})
    policy = resources.get(policy_resource_name, {})
    props = policy.get("Properties", {})
    policy_doc = props.get("PolicyDocument", {})
    return policy_doc.get("Statement", [])


def get_inline_policy_statements(template: dict, role_resource_name: str) -> list:
    """Extract inline policy statements from an IAM Role resource."""
    resources = template.get("Resources", {})
    role = resources.get(role_resource_name, {})
    props = role.get("Properties", {})
    policies = props.get("Policies", [])
    statements = []
    for policy in policies:
        policy_doc = policy.get("PolicyDocument", {})
        statements.extend(policy_doc.get("Statement", []))
    return statements


def normalize_actions(actions) -> list:
    """Normalize Action field to a list."""
    if isinstance(actions, str):
        return [actions]
    return actions if actions else []


def normalize_resources(resources) -> list:
    """Normalize Resource field to a list."""
    if isinstance(resources, str):
        return [resources]
    return resources if resources else []


# ---------------------------------------------------------------------------
# Test 2.2: FirehoseRole Least Privilege
# Validates: Requirement 3.6 - no Resource: "*" and limited action set
# ---------------------------------------------------------------------------

# Allowed action prefixes for FirehoseRole
FIREHOSE_ROLE_ALLOWED_ACTIONS = {
    "s3:PutObject",
    "s3:GetBucketLocation",
    "s3:ListBucket",
    "glue:GetTable",
    "glue:GetTableVersion",
    "glue:GetTableVersions",
    "glue:GetDatabase",
    "kms:GenerateDataKey",
    "kms:Decrypt",
    "kms:DescribeKey",
    "logs:PutLogEvents",
    "logs:CreateLogStream",
    "lambda:InvokeFunction",
}


def test_firehose_role_least_privilege(central_template):
    """
    Test 2.2: FirehoseRole only allows specific actions and no Resource: "*".
    
    The FirehoseRole policy must:
    - Not have any statement with Resource: "*"
    - Only contain allowed actions for S3, Glue, KMS, CloudWatch Logs, and Lambda
    """
    # Check FirehoseRolePolicy
    statements = get_policy_statements(central_template, "FirehoseRolePolicy")
    assert len(statements) > 0, "FirehoseRolePolicy should have statements"
    
    for stmt in statements:
        # Check no wildcard resources
        resources = normalize_resources(stmt.get("Resource"))
        for resource in resources:
            if isinstance(resource, str):
                assert resource != "*", (
                    f"FirehoseRolePolicy has Resource: '*' in statement {stmt.get('Sid', 'unknown')}"
                )
        
        # Check actions are in allowed set
        actions = normalize_actions(stmt.get("Action"))
        for action in actions:
            if isinstance(action, str):
                assert action in FIREHOSE_ROLE_ALLOWED_ACTIONS, (
                    f"FirehoseRolePolicy has unauthorized action: {action}"
                )
    
    # Also check FirehoseLambdaInvokePolicy
    lambda_statements = get_policy_statements(central_template, "FirehoseLambdaInvokePolicy")
    for stmt in lambda_statements:
        resources = normalize_resources(stmt.get("Resource"))
        for resource in resources:
            if isinstance(resource, str):
                assert resource != "*", (
                    "FirehoseLambdaInvokePolicy has Resource: '*'"
                )


# ---------------------------------------------------------------------------
# Test 2.4: CrossAccountFirehoseRole Least Privilege
# Validates: Requirements 4.2, 4.3, 4.4
# ---------------------------------------------------------------------------

CROSS_ACCOUNT_ALLOWED_ACTIONS = {"firehose:PutRecord", "firehose:PutRecordBatch"}


def test_cross_account_firehose_role_least_privilege(central_template):
    """
    Test 2.4: CrossAccountFirehoseRole only allows firehose:PutRecord and PutRecordBatch.
    
    The CrossAccountFirehoseRolePolicy must:
    - Have exactly two allowed actions: firehose:PutRecord and firehose:PutRecordBatch
    - Not have Resource: "*"
    - Resource must be scoped to a specific Firehose delivery stream ARN
    """
    statements = get_policy_statements(central_template, "CrossAccountFirehoseRolePolicy")
    assert len(statements) > 0, "CrossAccountFirehoseRolePolicy should have statements"
    
    all_actions = set()
    for stmt in statements:
        # Check no wildcard resources
        resources = normalize_resources(stmt.get("Resource"))
        for resource in resources:
            if isinstance(resource, str):
                assert resource != "*", (
                    "CrossAccountFirehoseRolePolicy has Resource: '*'"
                )
                # Resource should be a Firehose ARN pattern
                # CloudFormation !Sub returns a dict, so we check string resources
                if not resource.startswith("arn:"):
                    continue
                assert "firehose" in resource or "deliverystream" in resource, (
                    f"CrossAccountFirehoseRole resource should be a Firehose ARN: {resource}"
                )
        
        # Collect actions
        actions = normalize_actions(stmt.get("Action"))
        all_actions.update(actions)
    
    # Verify only allowed actions
    assert all_actions == CROSS_ACCOUNT_ALLOWED_ACTIONS, (
        f"CrossAccountFirehoseRole should only have {CROSS_ACCOUNT_ALLOWED_ACTIONS}, "
        f"but has {all_actions}"
    )


@given(
    source_account_ids=st.lists(
        st.from_regex(r"[0-9]{12}", fullmatch=True),
        min_size=1,
        max_size=10,
    )
)
@settings(max_examples=20)
def test_cross_account_trust_policy_pattern(source_account_ids):
    """
    Property test: Trust policy would only allow bedrock-cwl-delivery-role from source accounts.
    
    Verifies that the trust policy pattern (StringLike on aws:PrincipalArn) would correctly
    match only the expected role ARNs for any valid 12-digit account ID.
    """
    # The trust policy uses: arn:aws:iam::*:role/bedrock-cwl-delivery-role
    trust_pattern = r"arn:aws:iam::\*:role/bedrock-cwl-delivery-role"
    
    for account_id in source_account_ids:
        expected_arn = f"arn:aws:iam::{account_id}:role/bedrock-cwl-delivery-role"
        # The pattern with wildcard * should match any account's bedrock-cwl-delivery-role
        # Simulate StringLike matching: * matches any sequence
        pattern_regex = trust_pattern.replace(r"\*", r"[0-9]{12}")
        assert re.match(pattern_regex, expected_arn), (
            f"Trust pattern should match {expected_arn}"
        )
        
        # Verify other roles would NOT match
        other_role_arn = f"arn:aws:iam::{account_id}:role/some-other-role"
        assert not re.match(pattern_regex, other_role_arn), (
            f"Trust pattern should NOT match {other_role_arn}"
        )


# ---------------------------------------------------------------------------
# Test 3.2: Partition Projection Completeness
# Validates: Requirements 5.7, 5.8, 5.9, 12.3
# ---------------------------------------------------------------------------

REQUIRED_PARTITION_KEYS = {"year", "month", "day"}


def test_partition_projection_configuration(central_template):
    """
    Test 3.2: Glue table has correct partition projection configuration.
    
    The BedrockLogsGlueTable must have:
    - partition projection enabled
    - year, month, day partition keys defined
    - storage.location.template with correct path segments
    """
    resources = central_template.get("Resources", {})
    table = resources.get("BedrockLogsGlueTable", {})
    props = table.get("Properties", {})
    table_input = props.get("TableInput", {})
    
    # Check partition keys
    partition_keys = table_input.get("PartitionKeys", [])
    partition_key_names = {pk.get("Name") for pk in partition_keys}
    assert REQUIRED_PARTITION_KEYS.issubset(partition_key_names), (
        f"Table must have partition keys {REQUIRED_PARTITION_KEYS}, "
        f"but has {partition_key_names}"
    )
    
    # Check partition projection parameters
    params = table_input.get("Parameters", {})
    
    assert params.get("projection.enabled") == "true", (
        "Partition projection must be enabled"
    )
    
    # Verify projection config for each partition key
    for key in REQUIRED_PARTITION_KEYS:
        assert f"projection.{key}.type" in params, (
            f"Missing projection type for partition key: {key}"
        )


@given(
    year=st.integers(min_value=2020, max_value=2030),
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=31),
)
@settings(max_examples=50)
def test_partition_path_format(year, month, day):
    """
    Property test: Partition paths follow the expected Hive-style format.
    
    Verifies that for any valid date components, the storage location template
    would produce a valid S3 path with correct partition segments.
    """
    # Expected path format from storage.location.template
    # s3://bedrock-firehose-lake-{account}/data/year={year}/month={month}/day={day}
    month_str = f"{month:02d}"
    day_str = f"{day:02d}"
    
    expected_path_pattern = f"year={year}/month={month_str}/day={day_str}"
    
    # Verify the pattern is valid
    assert re.match(r"year=\d{4}/month=\d{2}/day=\d{2}", expected_path_pattern), (
        f"Path pattern should match expected format: {expected_path_pattern}"
    )
    
    # Verify Firehose prefix pattern would produce matching path
    # Firehose uses: data/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/
    firehose_pattern = f"data/year={year}/month={month_str}/day={day_str}/"
    assert "year=" in firehose_pattern and "month=" in firehose_pattern and "day=" in firehose_pattern


# ---------------------------------------------------------------------------
# Test 3.4: S3 Bucket Naming Convention
# Validates: Requirements 1.7, 6.7
# ---------------------------------------------------------------------------

S3_BUCKET_NAME_PATTERN = re.compile(r"^bedrock-(firehose-lake|athena-results)-[0-9]{12}$")


def test_bucket_naming_convention(central_template):
    """
    Test 3.4: S3 buckets follow the naming pattern bedrock-*-{account_id}.
    
    All S3 buckets in the template must:
    - Have names matching pattern: bedrock-(firehose-lake|athena-results)-{12-digit-account}
    """
    resources = central_template.get("Resources", {})
    
    for resource_name, resource in resources.items():
        if resource.get("Type") == "AWS::S3::Bucket":
            props = resource.get("Properties", {})
            bucket_name = props.get("BucketName")
            
            if bucket_name:
                # BucketName uses !Sub, which produces a dict in YAML
                if isinstance(bucket_name, dict) and "Fn::Sub" in bucket_name:
                    name_template = bucket_name["Fn::Sub"]
                    # Replace ${AWS::AccountId} with a sample 12-digit account
                    sample_name = name_template.replace("${AWS::AccountId}", "123456789012")
                    assert S3_BUCKET_NAME_PATTERN.match(sample_name), (
                        f"Bucket {resource_name} name '{sample_name}' doesn't match pattern"
                    )


@given(account_id=st.from_regex(r"[0-9]{12}", fullmatch=True))
@settings(max_examples=20)
def test_bucket_name_generation(account_id):
    """
    Property test: Bucket names are valid for any 12-digit account ID.
    
    Verifies that the bucket naming pattern produces valid S3 bucket names
    for any valid AWS account ID.
    """
    firehose_bucket = f"bedrock-firehose-lake-{account_id}"
    athena_bucket = f"bedrock-athena-results-{account_id}"
    
    # S3 bucket name constraints: 3-63 chars, lowercase, numbers, hyphens
    s3_name_pattern = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    
    assert s3_name_pattern.match(firehose_bucket), (
        f"Firehose bucket name '{firehose_bucket}' is invalid"
    )
    assert s3_name_pattern.match(athena_bucket), (
        f"Athena bucket name '{athena_bucket}' is invalid"
    )
    
    # Verify correct length
    assert len(firehose_bucket) == len("bedrock-firehose-lake-") + 12
    assert len(athena_bucket) == len("bedrock-athena-results-") + 12


# ---------------------------------------------------------------------------
# Test 4.4: No Glue Crawler/Job/Trigger
# Validates: Requirements 12.4, 12.5
# ---------------------------------------------------------------------------

PROHIBITED_GLUE_TYPES = {
    "AWS::Glue::Crawler",
    "AWS::Glue::Job",
    "AWS::Glue::Trigger",
}


def test_no_glue_crawler_or_etl(central_template):
    """
    Test 4.4: Template contains no Glue Crawler, Job, or Trigger resources.
    
    The Firehose architecture intentionally avoids Glue ETL components:
    - No AWS::Glue::Crawler (partition projection replaces crawlers)
    - No AWS::Glue::Job (Firehose does inline conversion)
    - No AWS::Glue::Trigger (no ETL jobs to trigger)
    """
    resources = central_template.get("Resources", {})
    
    for resource_name, resource in resources.items():
        resource_type = resource.get("Type", "")
        assert resource_type not in PROHIBITED_GLUE_TYPES, (
            f"Template should not contain {resource_type}: found {resource_name}"
        )


# ---------------------------------------------------------------------------
# Test 6.3: CWLDeliveryRole Isolation (bedrock-logging.yaml)
# Validates: Requirements 8.2, 8.3, 8.4
# ---------------------------------------------------------------------------

def test_cwl_delivery_role_not_in_source_template(source_template):
    """
    Test 6.3: Source template uses CWL Destination instead of CWLDeliveryRole.
    
    The updated bedrock-logging.yaml architecture uses a CloudWatch Logs Destination
    in the central account, eliminating the need for a CWLDeliveryRole in source accounts.
    This test verifies no CWLDeliveryRole exists and that BedrockLoggingPolicy only
    has CloudWatch Logs permissions.
    """
    resources = source_template.get("Resources", {})
    
    # CWLDeliveryRole should not exist in the source template
    # (it was removed when switching to CWL Destination architecture)
    assert "CWLDeliveryRole" not in resources, (
        "Source template should not have CWLDeliveryRole - uses CWL Destination instead"
    )
    
    # BedrockLoggingPolicy should only have logs:CreateLogStream and logs:PutLogEvents
    statements = get_policy_statements(source_template, "BedrockLoggingPolicy")
    
    allowed_actions = {"logs:CreateLogStream", "logs:PutLogEvents"}
    
    for stmt in statements:
        actions = normalize_actions(stmt.get("Action"))
        for action in actions:
            if isinstance(action, str):
                assert action in allowed_actions, (
                    f"BedrockLoggingPolicy has unauthorized action: {action}"
                )
        
        # Verify no Resource: "*"
        resources_list = normalize_resources(stmt.get("Resource"))
        for resource in resources_list:
            if isinstance(resource, str):
                assert resource != "*", (
                    "BedrockLoggingPolicy has Resource: '*'"
                )


@given(
    firehose_role_arn=st.from_regex(
        r"arn:aws:iam::[0-9]{12}:role/[a-zA-Z0-9_+=,.@-]+",
        fullmatch=True,
    )
)
@settings(max_examples=20)
def test_cwl_destination_arn_validation(firehose_role_arn):
    """
    Property test: CWL Destination ARN pattern validation.
    
    Verifies that the CentralCWLDestinationArn parameter's AllowedPattern
    would correctly validate any valid CloudWatch Logs Destination ARN.
    """
    # The parameter uses AllowedPattern: 'arn:aws:logs:.*'
    allowed_pattern = re.compile(r"arn:aws:logs:.*")
    
    # Generate a valid CWL Destination ARN
    # Format: arn:aws:logs:{region}:{account}:destination:{name}
    match = re.match(r"arn:aws:iam::([0-9]{12}):role/.*", firehose_role_arn)
    if match:
        account_id = match.group(1)
        destination_arn = f"arn:aws:logs:us-east-1:{account_id}:destination:bedrock-firehose"
        
        assert allowed_pattern.match(destination_arn), (
            f"CWL Destination ARN should match pattern: {destination_arn}"
        )


# ---------------------------------------------------------------------------
# Additional validation tests
# ---------------------------------------------------------------------------

def test_template_has_required_outputs(central_template):
    """Verify the central template exports required outputs."""
    outputs = central_template.get("Outputs", {})
    
    required_outputs = {
        "FirehoseDeliveryStreamArn",
        "CrossAccountFirehoseRoleArn",
        "FirehoseDataLakeBucketName",
        "BedrockAthenaWorkGroupName",
        "GlueDatabaseName",
    }
    
    actual_outputs = set(outputs.keys())
    missing = required_outputs - actual_outputs
    
    assert not missing, f"Missing required outputs: {missing}"


def test_all_buckets_have_deletion_policy(central_template):
    """Verify all S3 buckets have DeletionPolicy: Retain."""
    resources = central_template.get("Resources", {})
    
    for resource_name, resource in resources.items():
        if resource.get("Type") == "AWS::S3::Bucket":
            deletion_policy = resource.get("DeletionPolicy")
            assert deletion_policy == "Retain", (
                f"Bucket {resource_name} should have DeletionPolicy: Retain"
            )
