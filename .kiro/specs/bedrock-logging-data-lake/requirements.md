# Requirements Document

## Introduction

This feature builds a centralized data lake that aggregates Amazon Bedrock model invocation logs from multiple source AWS accounts. Logs are replicated via S3 cross-account replication into a single central S3 bucket, partitioned for efficient querying, and exposed through an Athena query layer backed by the Glue Data Catalog. The feature involves two CloudFormation stacks: a new `bedrock-data-lake.yaml` for the central account, and updates to the existing `bedrock-logging.yaml` deployed in each source account.

## Glossary

- **Central_Account**: The AWS account that hosts the data lake, central S3 bucket, Glue catalog, and Athena workgroup.
- **Source_Account**: An AWS account that runs Bedrock workloads and has `bedrock-logging.yaml` deployed to write invocation logs.
- **BedrockDataLakeBucket**: The central S3 bucket in the Central_Account that receives replicated logs from all Source_Accounts.
- **BedrockAthenaResultsBucket**: The S3 bucket in the Central_Account that stores Athena query output.
- **Replication_Role**: The IAM role in each Source_Account that S3 assumes to replicate objects to the BedrockDataLakeBucket.
- **Glue_Catalog**: The AWS Glue Data Catalog database (`bedrock_logs`) and table (`bedrock_invocations`) that defines the schema and partition metadata.
- **Glue_Crawler**: The AWS Glue Crawler scheduled to run daily that discovers new partition prefixes and updates the Glue_Catalog.
- **Athena_WorkGroup**: The Athena workgroup (`bedrock-analytics`) used to query the data lake with enforced output location and scan limits.
- **Hive_Partition_Layout**: The S3 key structure `logs/account_id={id}/region={region}/year={year}/month={month}/day={day}/` that enables Athena partition pruning.
- **Invocation_Log**: A JSON record emitted by Amazon Bedrock describing a single model invocation, written to the source S3 bucket.
- **CloudFormation_Stack**: An AWS CloudFormation stack managing a set of AWS resources as a unit.
- **CentralDataLakeAccountId**: The CloudFormation parameter in `bedrock-logging.yaml` that holds the 12-digit AWS account ID of the Central_Account.

---

## Requirements

### Requirement 1: Central Data Lake S3 Bucket

**User Story:** As a FinOps engineer, I want a single central S3 bucket that receives replicated Bedrock logs from all source accounts, so that I have one authoritative location for cross-account log analysis.

#### Acceptance Criteria

1. THE BedrockDataLakeBucket SHALL have versioning enabled (`VersioningConfiguration.Status: Enabled`) to satisfy S3 cross-account replication prerequisites.
2. THE BedrockDataLakeBucket SHALL have server-side encryption enabled using AES-256 (`SSEAlgorithm: AES256`) applied to all objects at rest.
3. THE BedrockDataLakeBucket SHALL have all four `PublicAccessBlock` settings (`BlockPublicAcls`, `BlockPublicPolicy`, `IgnorePublicAcls`, `RestrictPublicBuckets`) set to `true`.
4. THE BedrockDataLakeBucket SHALL have a lifecycle rule with `Status: Enabled` scoped to the `logs/` prefix that transitions objects to STANDARD_IA storage after 90 days and to GLACIER after 365 days.
5. THE BedrockDataLakeBucket SHALL have a bucket policy statement that denies any request where `aws:SecureTransport` is `false`, enforcing HTTPS-only access for all principals.
6. THE BedrockDataLakeBucket SHALL have a bucket policy statement that grants each Source_Account's Replication_Role (identified by an IAM role ARN in a Source_Account) permission to perform `s3:ReplicateObject`, `s3:ReplicateDelete`, `s3:ReplicateTags`, and `s3:ObjectOwnerOverrideToBucketOwner` on objects under the `logs/` prefix.
7. THE BedrockDataLakeBucket SHALL be named using the pattern `bedrock-data-lake-{CentralAccountId}`, where `{CentralAccountId}` is the 12-digit AWS account ID of the Central_Account.
8. THE BedrockDataLakeBucket SHALL have `BucketOwnerEnforced` ownership controls so that the Central_Account is the owner of all replicated objects regardless of source account ACLs.

---

### Requirement 2: Athena Results Bucket

**User Story:** As a FinOps engineer, I want Athena query results stored in a dedicated bucket separate from the data lake, so that result data is isolated for cost tracking and access control.

#### Acceptance Criteria

1. THE BedrockAthenaResultsBucket SHALL have server-side encryption enabled using AES-256 (`SSEAlgorithm: AES256`).
2. THE BedrockAthenaResultsBucket SHALL have all four `PublicAccessBlock` settings (`BlockPublicAcls`, `BlockPublicPolicy`, `IgnorePublicAcls`, `RestrictPublicBuckets`) set to `true`.
3. THE BedrockAthenaResultsBucket SHALL have a lifecycle rule that expires (permanently deletes) all objects in the bucket after 30 days.
4. THE BedrockAthenaResultsBucket SHALL be named using the pattern `bedrock-athena-results-{CentralAccountId}`, where `{CentralAccountId}` is the 12-digit AWS account ID of the Central_Account.
5. IF the `CentralAccountId` value is absent or does not match 12 digits at stack deployment time, THE CloudFormation stack SHALL fail to deploy with a parameter validation error before any resources are created.

---

### Requirement 3: Glue Data Catalog Schema

**User Story:** As a data analyst, I want a Glue database and table definition for Bedrock invocation logs, so that Athena can resolve the schema and partition structure without manual configuration.

#### Acceptance Criteria

1. THE Glue_Catalog SHALL contain a database named `bedrock_logs` that serves as the namespace for all Bedrock log tables.
2. THE Glue_Catalog SHALL contain an external table named `bedrock_invocations` with its `StorageDescriptor.Location` pointing to `s3://bedrock-data-lake-{CentralAccountId}/logs/`.
3. THE `bedrock_invocations` table SHALL declare partition keys `account_id`, `region`, `year`, `month`, and `day`, all of type `string`, as the first-level path segments of the Hive_Partition_Layout.
4. THE `bedrock_invocations` table SHALL declare columns for all fields in the Invocation_Log schema: `schematype` (string), `schemaversionstr` (string), `timestamp` (string), `accountid` (string), `region` (string), `requestid` (string), `operation` (string), `modelid` (string), `input` (struct with subfields `inputbodyjson` string, `inputcontenttype` string, `inputtokens` int), and `output` (struct with subfields `outputbodyjson` string, `outputcontenttype` string, `outputtokens` int).
5. THE `bedrock_invocations` table SHALL use the `org.openx.data.jsonserde.JsonSerDe` serialization library so Athena can deserialize JSON log records.
6. THE `bedrock_invocations` table SHALL have partition projection enabled on all five partition keys so Athena can resolve partitions without requiring `MSCK REPAIR TABLE` or Glue Crawler runs for query execution.

---

### Requirement 4: Glue Crawler for Partition Discovery

**User Story:** As a data analyst, I want a Glue Crawler to automatically discover new partitions daily, so that I can query the previous day's logs each morning without running manual repair commands.

#### Acceptance Criteria

1. THE Glue_Crawler SHALL be configured with an S3 target path of `s3://bedrock-data-lake-{CentralAccountId}/logs/` to discover new partition prefixes.
2. THE Glue_Crawler SHALL run on a scheduled cron expression of `cron(0 6 * * ? *)` (daily at 06:00 UTC) and SHALL NOT run on any other schedule by default.
3. WHEN the Glue_Crawler run completes and new partition prefixes are found, THE Glue_Catalog `bedrock_logs` database SHALL be updated to reflect the new partitions (`UpdateBehavior: UPDATE_IN_DATABASE`).
4. WHEN the Glue_Crawler detects that a previously present table or column is absent from the crawled data, THE Glue_Catalog SHALL record the change in the crawler run log without deleting or modifying the existing table definition (`DeleteBehavior: LOG`).
5. THE Glue_Crawler SHALL use an IAM role that grants `s3:GetObject` and `s3:ListBucket` scoped to the BedrockDataLakeBucket, and `glue:UpdateDatabase`, `glue:CreateTable`, `glue:UpdateTable`, and `glue:BatchCreatePartition` scoped to the `bedrock_logs` database ARN.
6. THE Glue_Crawler SHALL have `RecrawlPolicy.RecrawlBehavior` set to `CRAWL_NEW_FOLDERS_ONLY` so that concurrent or repeated runs do not redundantly re-scan already-catalogued prefixes.

---

### Requirement 5: Athena WorkGroup Configuration

**User Story:** As a FinOps engineer, I want an Athena workgroup with enforced query output location and a per-query scan limit, so that all query results are centrally managed and runaway queries are prevented.

#### Acceptance Criteria

1. THE Athena_WorkGroup SHALL be named `bedrock-analytics`.
2. THE Athena_WorkGroup SHALL enforce that query results are written to `s3://bedrock-athena-results-{CentralAccountId}/results/` and SHALL reject any per-query output location override.
3. THE Athena_WorkGroup SHALL encrypt query results using SSE-S3 (`EncryptionOption: SSE_S3`).
4. THE Athena_WorkGroup SHALL enforce a per-query data-scanned limit of 10,737,418,240 bytes (10 GB); WHEN a query exceeds this threshold it SHALL transition to a `CANCELLED` state with an error message indicating the scan limit was exceeded.
5. THE Athena_WorkGroup SHALL publish the CloudWatch metrics `BytesScannedCutoffPerQuery`, `QueryExecutionTime`, and `QueryState` so query costs and usage can be monitored per workgroup.
6. THE Athena_WorkGroup SHALL set `EnforceWorkGroupConfiguration: true` so that all workgroup-level settings take precedence over any per-request client-side configuration overrides.
7. IF the configured output location (`s3://bedrock-athena-results-{CentralAccountId}/results/`) is inaccessible when a query is submitted, THE Athena_WorkGroup SHALL fail the query with an error indicating the output location is unavailable rather than silently writing results to an alternate location.

---

### Requirement 6: Source Account S3 Replication IAM Role

**User Story:** As a platform engineer, I want an IAM role deployed in each source account that grants S3 permission to replicate Bedrock log objects to the central data lake, so that cross-account replication works without using overly broad permissions.

#### Acceptance Criteria

1. THE Replication_Role SHALL be named `bedrock-s3-replication-role` and SHALL have a trust policy that allows only the `s3.amazonaws.com` service principal to assume it.
2. THE Replication_Role SHALL grant `s3:GetReplicationConfiguration` and `s3:ListBucket` scoped only to the Source_Account's `BedrockLogsS3Bucket` ARN (not `*` or any other bucket).
3. THE Replication_Role SHALL grant `s3:GetObjectVersionForReplication`, `s3:GetObjectVersionAcl`, and `s3:GetObjectVersionTagging` scoped only to the ARN pattern `{BedrockLogsS3Bucket.Arn}/*` (objects within the source bucket).
4. THE Replication_Role SHALL grant `s3:ReplicateObject`, `s3:ReplicateDelete`, and `s3:ReplicateTags` scoped only to the ARN pattern `arn:aws:s3:::bedrock-data-lake-{CentralDataLakeAccountId}/*`.
5. THE Replication_Role policy statements SHALL NOT include any action other than the six actions enumerated in criteria 2, 3, and 4, and SHALL NOT include any resource ARN that references AWS resources outside the two buckets named in criteria 2 and 4.
6. THE Replication_Role SHALL NOT have any AWS managed policies or inline policies attached beyond the single policy defined in criteria 2–5.

---

### Requirement 7: Source Account S3 Bucket Modifications

**User Story:** As a platform engineer, I want the existing `BedrockLogsS3Bucket` in each source account updated to enable versioning and cross-account replication, so that new Bedrock log objects are automatically replicated to the central data lake.

#### Acceptance Criteria

1. THE `BedrockLogsS3Bucket` in each Source_Account SHALL have versioning enabled (`VersioningConfiguration.Status: Enabled`) because S3 replication requires versioning on the source bucket.
2. THE `BedrockLogsS3Bucket` in each Source_Account SHALL have a `ReplicationConfiguration` with `Role` set to the Replication_Role ARN and at least one rule with `Status: Enabled` targeting the BedrockDataLakeBucket ARN (`arn:aws:s3:::bedrock-data-lake-{CentralDataLakeAccountId}`).
3. THE replication rule SHALL include a `Filter.Prefix` of `{AccountId}/` (where `{AccountId}` resolves to `!Sub '${AWS::AccountId}/'`) so that only objects under the source account's prefix are replicated.
4. THE replication rule SHALL set `Destination.AccessControlTranslation.Owner` to `Destination` so the Central_Account owns all replicated objects.
5. THE replication rule SHALL set `DeleteMarkerReplication.Status` to `Disabled` to prevent accidental deletion propagation to the data lake.

---

### Requirement 8: Central Account ID Parameter

**User Story:** As a platform engineer, I want the `CentralDataLakeAccountId` passed as a CloudFormation parameter in `bedrock-logging.yaml`, so that the source account stack can be deployed to any environment without hardcoded account IDs.

#### Acceptance Criteria

1. THE `bedrock-logging.yaml` CloudFormation template SHALL include a parameter named `CentralDataLakeAccountId` of type `String` with a non-empty `Description` field and a `ConstraintDescription` that explains the 12-digit format requirement.
2. THE `CentralDataLakeAccountId` parameter SHALL have an `AllowedPattern` of `[0-9]{12}` so that CloudFormation rejects any input that is not exactly 12 decimal digits before creating any resources.
3. THE `bedrock-logging.yaml` template SHALL reference the central account ID exclusively via `!Ref CentralDataLakeAccountId`; no literal 12-digit account ID strings SHALL appear in IAM policy resource ARNs, S3 bucket ARNs, or replication destination properties.
4. THE `CentralDataLakeAccountId` parameter SHALL have no `Default` value so that deployers are required to supply the account ID explicitly at every stack create or update operation.

---

### Requirement 9: Cross-Account Replication Observability

**User Story:** As a platform engineer, I want alerts when replication fails or is delayed, so that I can detect and remediate data gaps before they affect analytics.

#### Acceptance Criteria

1. WHEN at least one S3 object replication operation for a source bucket fails, THE Source_Account SHALL emit a `ReplicationFailed` CloudWatch metric with a value of 1 per failed object within 5 minutes of the failure event.
2. WHERE S3 Replication Time Control (RTC) is enabled on a source bucket replication rule, THE Source_Account SHALL have a CloudWatch Alarm that transitions to `ALARM` state when the `ReplicationLatency` metric exceeds 15 minutes for that bucket.
3. WHEN an S3 replication attempt for a source bucket results in a `403 AccessDenied` response from the destination, THE Source_Account SHALL emit a `ReplicationFailed` CloudWatch metric so the operator is alerted to update the central bucket policy with the correct Replication_Role ARN and redeploy.

---

### Requirement 10: Glue Crawler Schema Change Handling

**User Story:** As a data engineer, I want the Glue Crawler to handle schema evolution in Bedrock invocation logs gracefully, so that new log fields added by AWS do not break existing Athena queries.

#### Acceptance Criteria

1. WHEN the Glue_Crawler run completes and new columns are detected in the Bedrock log JSON that are not present in the current `bedrock_invocations` table definition, THE Glue_Catalog SHALL add those columns to the `bedrock_invocations` table in the same crawler run without requiring manual intervention.
2. WHEN the Glue_Crawler run completes and a column that was previously present in the `bedrock_invocations` table is absent from the crawled data, THE column SHALL be retained in the `bedrock_invocations` table definition AND the schema change event SHALL be recorded in the Glue Crawler run logs.
3. IF the `bedrock_invocations` table schema has been updated with additional columns since a query was written, THEN previously written Athena queries that do not reference the new columns SHALL continue to execute and return results without error.

---

### Requirement 11: Data Lake Infrastructure as Code

**User Story:** As a platform engineer, I want all central data lake resources defined in a single CloudFormation template, so that the stack can be deployed, updated, and version-controlled consistently.

#### Acceptance Criteria

1. THE `bedrock-data-lake.yaml` CloudFormation template SHALL define all of the following logical resources: `BedrockDataLakeBucket`, `BedrockAthenaResultsBucket`, `BedrockLogsGlueDatabase`, `BedrockLogsGlueTable`, `BedrockLogsGlueCrawler`, `GlueCrawlerRole`, `BedrockAthenaWorkGroup`, and a bucket policy resource for the BedrockDataLakeBucket.
2. THE `bedrock-data-lake.yaml` template SHALL pass `cfn-lint` (invoked with no additional flags beyond the template path) with zero findings at `error` severity; warnings are permitted.
3. THE project SHALL include a `cfn-guard` rules file named `s3-security.guard` that enforces the following on every `AWS::S3::Bucket` resource: `VersioningConfiguration.Status` equals `Enabled`, `BucketEncryption.ServerSideEncryptionConfiguration[0].ServerSideEncryptionByDefault.SSEAlgorithm` equals `AES256`, and all four `PublicAccessBlockConfiguration` properties equal `true`.
4. WHEN `bedrock-logging.yaml` is updated and deployed as a CloudFormation stack update, THE logical IDs `BedrockLogsGroup`, `BedrockLogsS3Bucket`, `BedrockLogsS3BucketPolicy`, `BedrockLoggingRole`, and `BedrockObservabilityProfile` SHALL all remain present in the updated template, AND the stack update SHALL complete without rollback.
