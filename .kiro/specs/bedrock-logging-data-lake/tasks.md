# Implementation Plan: Bedrock Logging Data Lake

## Overview

Implement the centralized Bedrock invocation log data lake using two CloudFormation templates. The work splits into three streams: (1) the new `bedrock-data-lake.yaml` template for the central account, (2) targeted modifications to the existing `bedrock-logging.yaml` template deployed in source accounts, and (3) a `cfn-guard` security rules file. All resources are defined as Infrastructure-as-Code with no runtime code outside CloudFormation and cfn-guard.

---

## Tasks

- [x] 1. Create the `bedrock-data-lake.yaml` central account template — S3 buckets
  - [x] 1.1 Define `BedrockDataLakeBucket` with full security configuration
    - Add `AWS::S3::Bucket` resource with logical ID `BedrockDataLakeBucket`
    - Set `BucketName: !Sub 'bedrock-data-lake-${AWS::AccountId}'`
    - Enable versioning: `VersioningConfiguration.Status: Enabled`
    - Enable AES-256 SSE: `BucketEncryption.ServerSideEncryptionConfiguration[0].ServerSideEncryptionByDefault.SSEAlgorithm: AES256`
    - Set all four `PublicAccessBlockConfiguration` properties to `true`
    - Add lifecycle rule scoped to `logs/` prefix: transition to STANDARD_IA at 90 days, GLACIER at 365 days
    - Set `OwnershipControls.Rules[0].ObjectOwnership: BucketOwnerEnforced`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.7, 1.8_

  - [x] 1.2 Define `BedrockAthenaResultsBucket` with full security configuration
    - Add `AWS::S3::Bucket` resource with logical ID `BedrockAthenaResultsBucket`
    - Set `BucketName: !Sub 'bedrock-athena-results-${AWS::AccountId}'`
    - Enable AES-256 SSE
    - Set all four `PublicAccessBlockConfiguration` properties to `true`
    - Add lifecycle rule that expires all objects after 30 days (`ExpirationInDays: 30`)
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 1.3 Add `CentralAccountId` parameter and `BedrockDataLakeBucketPolicy`
    - Add a `CentralAccountId` parameter of type `String` with `AllowedPattern: '[0-9]{12}'` and a `ConstraintDescription` explaining the 12-digit format; set no `Default` value
    - Add `AWS::S3::BucketPolicy` resource for `BedrockDataLakeBucket` with two statements:
      1. Deny statement: `Effect: Deny`, all principals (`"*"`), `Action: s3:*`, `Condition: {Bool: {"aws:SecureTransport": false}}`
      2. Allow statement granting `s3:ReplicateObject`, `s3:ReplicateDelete`, `s3:ReplicateTags`, `s3:ObjectOwnerOverrideToBucketOwner` to source account replication role ARNs scoped to the `logs/` prefix — use a parameter or condition for the source account role ARNs
    - _Requirements: 1.5, 1.6, 2.5_

- [x] 2. Create the `bedrock-data-lake.yaml` template — Glue Data Catalog resources
  - [x] 2.1 Define `BedrockLogsGlueDatabase`
    - Add `AWS::Glue::Database` resource with logical ID `BedrockLogsGlueDatabase`
    - Set `CatalogId: !Ref AWS::AccountId`
    - Set `DatabaseInput.Name: bedrock_logs` and a `Description` field
    - _Requirements: 3.1_

  - [x] 2.2 Define `BedrockLogsGlueTable` with full schema and partition projection
    - Add `AWS::Glue::Table` resource with logical ID `BedrockLogsGlueTable`
    - Set `DatabaseName: !Ref BedrockLogsGlueDatabase`, `CatalogId: !Ref AWS::AccountId`
    - Set `TableInput.Name: bedrock_invocations`, `TableType: EXTERNAL_TABLE`
    - Declare partition keys: `account_id`, `region`, `year`, `month`, `day` (all `string`)
    - Set `StorageDescriptor.Location: !Sub 's3://bedrock-data-lake-${AWS::AccountId}/logs/'`
    - Set `InputFormat: org.apache.hadoop.mapred.TextInputFormat`
    - Set `OutputFormat: org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat`
    - Set `SerdeInfo.SerializationLibrary: org.openx.data.jsonserde.JsonSerDe`
    - Declare all columns from the Invocation_Log schema: `schematype`, `schemaversionstr`, `timestamp`, `accountid`, `region`, `requestid`, `operation`, `modelid`, `input` (struct), `output` (struct)
    - Enable partition projection on all five partition keys via `Parameters` table properties (`projection.enabled: true` and per-key projection settings)
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 3. Create the `bedrock-data-lake.yaml` template — Glue Crawler and IAM role
  - [x] 3.1 Define `GlueCrawlerRole`
    - Add `AWS::IAM::Role` with logical ID `GlueCrawlerRole`
    - Trust policy: allow `glue.amazonaws.com` to assume the role
    - Attach `AWSGlueServiceRole` AWS managed policy
    - Add inline policy granting:
      - `s3:GetObject`, `s3:ListBucket` scoped to `BedrockDataLakeBucket` ARN and `BedrockDataLakeBucket.Arn/*`
      - `glue:UpdateDatabase`, `glue:CreateTable`, `glue:UpdateTable`, `glue:BatchCreatePartition` scoped to the `bedrock_logs` database ARN (`arn:aws:glue:${AWS::Region}:${AWS::AccountId}:database/bedrock_logs`)
    - _Requirements: 4.5_

  - [x] 3.2 Define `BedrockLogsGlueCrawler`
    - Add `AWS::Glue::Crawler` with logical ID `BedrockLogsGlueCrawler`
    - Set `Role: !GetAtt GlueCrawlerRole.Arn`
    - Set `DatabaseName: !Ref BedrockLogsGlueDatabase`
    - Set `Targets.S3Targets[0].Path: !Sub 's3://bedrock-data-lake-${AWS::AccountId}/logs/'`
    - Set `Schedule.ScheduleExpression: 'cron(0 6 * * ? *)'`
    - Set `SchemaChangePolicy.UpdateBehavior: UPDATE_IN_DATABASE`
    - Set `SchemaChangePolicy.DeleteBehavior: LOG`
    - Set `RecrawlPolicy.RecrawlBehavior: CRAWL_NEW_FOLDERS_ONLY`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6_

- [x] 4. Create the `bedrock-data-lake.yaml` template — Athena WorkGroup and outputs
  - [x] 4.1 Define `BedrockAthenaWorkGroup`
    - Add `AWS::Athena::WorkGroup` with logical ID `BedrockAthenaWorkGroup`
    - Set `Name: bedrock-analytics`
    - Set `WorkGroupConfiguration.ResultConfiguration.OutputLocation: !Sub 's3://bedrock-athena-results-${AWS::AccountId}/results/'`
    - Set `ResultConfiguration.EncryptionConfiguration.EncryptionOption: SSE_S3`
    - Set `EnforceWorkGroupConfiguration: true`
    - Set `PublishCloudWatchMetricsEnabled: true`
    - Set `BytesScannedCutoffPerQuery: 10737418240`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x] 4.2 Add CloudFormation Outputs section to `bedrock-data-lake.yaml`
    - Export `BedrockDataLakeBucketName`, `BedrockDataLakeBucketArn`, `BedrockAthenaResultsBucketName`, `BedrockLogsGlueDatabaseName`, `BedrockAthenaWorkGroupName`
    - _Requirements: 11.1_

- [x] 5. Checkpoint — validate `bedrock-data-lake.yaml` with cfn-lint
  - Run `cfn-lint bedrock-data-lake.yaml` and resolve any `error`-severity findings before proceeding.
  - Confirm all eight required logical resource IDs are present: `BedrockDataLakeBucket`, `BedrockAthenaResultsBucket`, `BedrockLogsGlueDatabase`, `BedrockLogsGlueTable`, `BedrockLogsGlueCrawler`, `GlueCrawlerRole`, `BedrockAthenaWorkGroup`, and the bucket policy resource.
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Modify `bedrock-logging.yaml` — parameter and source bucket updates
  - [x] 6.1 Add `CentralDataLakeAccountId` parameter to `bedrock-logging.yaml`
    - Insert a `Parameters` block (or add to the existing one) with:
      ```yaml
      CentralDataLakeAccountId:
        Type: String
        Description: 'AWS Account ID of the central Bedrock data lake account (12-digit numeric)'
        AllowedPattern: '[0-9]{12}'
        ConstraintDescription: 'Must be exactly 12 decimal digits'
      ```
    - Confirm no `Default` value is set
    - _Requirements: 8.1, 8.2, 8.4_

  - [x] 6.2 Enable versioning and add `ReplicationConfiguration` to `BedrockLogsS3Bucket`
    - Under `BedrockLogsS3Bucket.Properties`, add:
      ```yaml
      VersioningConfiguration:
        Status: Enabled
      ```
    - Add `ReplicationConfiguration` block with `Role: !GetAtt BedrockS3ReplicationRole.Arn` and one rule:
      - `Id: ReplicateAllToDataLake`, `Status: Enabled`
      - `Filter.Prefix: !Sub '${AWS::AccountId}/'`
      - `Destination.Bucket: !Sub 'arn:aws:s3:::bedrock-data-lake-${CentralDataLakeAccountId}'`
      - `Destination.StorageClass: STANDARD`
      - `Destination.Account: !Ref CentralDataLakeAccountId`
      - `Destination.AccessControlTranslation.Owner: Destination`
      - `DeleteMarkerReplication.Status: Disabled`
    - Verify that all existing logical IDs (`BedrockLogsGroup`, `BedrockLogsS3Bucket`, `BedrockLogsS3BucketPolicy`, `BedrockLoggingRole`, `BedrockObservabilityProfile`) remain unchanged
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 11.4_

- [x] 7. Modify `bedrock-logging.yaml` — add `BedrockS3ReplicationRole`
  - [x] 7.1 Add `BedrockS3ReplicationRole` IAM role resource
    - Add `AWS::IAM::Role` with logical ID `BedrockS3ReplicationRole`
    - Set `RoleName: bedrock-s3-replication-role`
    - Trust policy: allow only `s3.amazonaws.com` to assume the role
    - Inline policy `BedrockS3ReplicationPolicy` with exactly three statements and no additional actions:
      1. `Sid: SourceBucketRead` — `s3:GetReplicationConfiguration`, `s3:ListBucket` on `!GetAtt BedrockLogsS3Bucket.Arn`
      2. `Sid: SourceObjectRead` — `s3:GetObjectVersionForReplication`, `s3:GetObjectVersionAcl`, `s3:GetObjectVersionTagging` on `!Sub '${BedrockLogsS3Bucket.Arn}/*'`
      3. `Sid: DestinationWrite` — `s3:ReplicateObject`, `s3:ReplicateDelete`, `s3:ReplicateTags` on `!Sub 'arn:aws:s3:::bedrock-data-lake-${CentralDataLakeAccountId}/*'`
    - No AWS managed policies attached
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 8.3_

  - [x] 7.2 Add `BedrockS3ReplicationRole` ARN to Outputs
    - Add output `BedrockS3ReplicationRoleArn` exporting `!GetAtt BedrockS3ReplicationRole.Arn`
    - This ARN is required for central account operators to configure the `BedrockDataLakeBucketPolicy`
    - _Requirements: 6.1_

- [x] 8. Create the `s3-security.guard` cfn-guard rules file
  - [x] 8.1 Write cfn-guard rules enforcing versioning, encryption, and public access blocks
    - Create file `s3-security.guard` in the workspace root
    - Add a rule that selects every `AWS::S3::Bucket` resource and asserts `VersioningConfiguration.Status == "Enabled"`
    - Add a rule asserting `BucketEncryption.ServerSideEncryptionConfiguration[0].ServerSideEncryptionByDefault.SSEAlgorithm == "AES256"`
    - Add rules asserting all four `PublicAccessBlockConfiguration` properties (`BlockPublicAcls`, `BlockPublicPolicy`, `IgnorePublicAcls`, `RestrictPublicBuckets`) equal `true`
    - Add human-readable `<<` error messages to each rule clause
    - _Requirements: 11.3_

  - [ ]* 8.2 Validate `s3-security.guard` passes against both templates
    - Run `cfn-guard validate -d bedrock-data-lake.yaml -r s3-security.guard` and confirm all rules PASS
    - Run `cfn-guard validate -d bedrock-logging.yaml -r s3-security.guard` and confirm all rules PASS (source bucket now has versioning and encryption)
    - _Requirements: 11.3, 4.4 (Property 4)_

- [x] 9. Checkpoint — validate `bedrock-logging.yaml` with cfn-lint
  - Run `cfn-lint bedrock-logging.yaml` and resolve any `error`-severity findings.
  - Confirm all five original logical IDs are still present.
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Write integration test steps
  - [x] 10.1 Document end-to-end replication validation steps
    - Create `integration-tests/README.md` (or add a section to an existing doc) that describes the manual integration test procedure:
      1. Deploy `bedrock-data-lake.yaml` to the central account with `aws cloudformation deploy`
      2. Deploy updated `bedrock-logging.yaml` to one source account providing `CentralDataLakeAccountId`
      3. Invoke a Bedrock model via the Application Inference Profile to generate a log entry
      4. Assert the log object appears in `bedrock-logs-{accountId}-{region}` within 60 seconds
      5. Assert the same object appears in `bedrock-data-lake-{centralAccountId}` within 15 minutes
      6. Trigger the Glue Crawler manually: `aws glue start-crawler --name bedrock-logs-crawler`
      7. Wait for the crawler to reach `READY` state and assert the partition appears in the Glue catalog
      8. Run an Athena query in the `bedrock-analytics` workgroup: `SELECT COUNT(*) FROM bedrock_logs.bedrock_invocations WHERE account_id='<sourceAccountId>' AND year='<year>';` — assert the row count matches the number of log objects written
    - _Requirements: (integration test steps from design.md Testing Strategy)_

  - [ ]* 10.2 Write property-based tests for CloudFormation template correctness
    - Use `pytest` + `hypothesis` (Python) to write property tests for:
      - **Property 1: S3 Bucket Naming Convention** — for any valid 12-digit account ID string, assert the rendered `BucketName` values in both templates match `bedrock-data-lake-{accountId}` and `bedrock-athena-results-{accountId}` respectively
      - **Property 3: CentralDataLakeAccountId Parameter Validation** — for any input string, assert `re.fullmatch(r'[0-9]{12}', value)` matches iff the AllowedPattern regex accepts it; test with 11-digit, 13-digit, alpha, and empty strings as counterexamples
      - **Property 4: All S3 Buckets Have Required Security Settings** — parse `bedrock-data-lake.yaml` with PyYAML and iterate all `AWS::S3::Bucket` resources; assert each has `VersioningConfiguration.Status == "Enabled"`, `SSEAlgorithm == "AES256"`, and all four public access block flags are `true`
    - Create `integration-tests/test_cfn_properties.py`
    - _Requirements: 1.1, 1.2, 1.3, 1.7, 2.4, 8.2, 11.3_

  - [ ]* 10.3 Write property-based test for Replication Role least privilege (Property 2)
    - Parse `bedrock-logging.yaml` with PyYAML and locate `BedrockS3ReplicationRole`
    - Assert that for every statement in the inline policy, no action other than the six enumerated in Requirements 6.2–6.4 is present
    - Assert that every resource ARN in the policy references either `BedrockLogsS3Bucket` or `bedrock-data-lake-{CentralDataLakeAccountId}` — no wildcards covering the entire account
    - **Property 2: Replication Role Least Privilege**
    - **Validates: Requirements 6.2, 6.5**
    - _Requirements: 6.2, 6.5_

- [x] 11. Final checkpoint — ensure all linting and guard rules pass
  - Run `cfn-lint bedrock-data-lake.yaml` — zero errors
  - Run `cfn-lint bedrock-logging.yaml` — zero errors
  - Run `cfn-guard validate -d bedrock-data-lake.yaml -r s3-security.guard` — all rules PASS
  - Run `cfn-guard validate -d bedrock-logging.yaml -r s3-security.guard` — all rules PASS
  - Ensure all tests pass, ask the user if questions arise.

---

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP
- All `!Sub`, `!Ref`, and `!GetAtt` references in CloudFormation YAML should be treated as literal CloudFormation intrinsic functions — cfn-lint validates these
- The central bucket policy's source account replication role ARNs (Requirement 1.6) should be passed as a `CommaDelimitedList` parameter in `bedrock-data-lake.yaml` to support multiple source accounts without template duplication
- `BucketOwnerEnforced` on the central bucket disables ACLs entirely; the source bucket's `AccessControlTranslation: Owner: Destination` in the replication rule is still required to tell S3 to hand ownership to the destination account
- Do not hardcode account IDs anywhere; use `!Ref CentralDataLakeAccountId` or `!Sub '${AWS::AccountId}'`
- Inclusive language: this document uses "allowlist" and "denylist" instead of whitelist/blacklist where relevant

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "2.1", "6.1"] },
    { "id": 1, "tasks": ["1.3", "2.2", "3.1", "6.2"] },
    { "id": 2, "tasks": ["3.2", "4.1", "7.1"] },
    { "id": 3, "tasks": ["4.2", "7.2"] },
    { "id": 4, "tasks": ["8.1"] },
    { "id": 5, "tasks": ["8.2", "10.1"] },
    { "id": 6, "tasks": ["10.2", "10.3"] }
  ]
}
```
