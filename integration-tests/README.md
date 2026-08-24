# Bedrock Logging Data Lake — End-to-End Integration Test Procedure

This document describes the manual integration test steps to validate cross-account S3 replication and the Athena query layer for the Bedrock logging data lake.

---

## Prerequisites

Before starting, confirm all of the following are in place:

- **`cfn-lint` installed** — used to lint templates before deployment
  ```bash
  cfn-lint --version
  ```
- **Both source accounts are deployed** — `bedrock-logging.yaml` must have been deployed previously (without `CentralDataLakeAccountId`) to accounts `466959819186` and `904247366374`
- **Central account is accessible** — you have CLI credentials that can deploy CloudFormation stacks and invoke Bedrock in the central account
- **AWS CLI configured** with profiles or environment variables for all three accounts:
  - Source account 1: `466959819186`
  - Source account 2: `904247366374`
  - Central account: the account where the data lake will be deployed
- **Bedrock model access** — `amazon.nova-pro-v1:0` is enabled in each source account's region

---

## Step 1: Deploy `bedrock-data-lake.yaml` to the Central Account

Deploy the central data lake stack, passing the replication role ARNs from both source accounts.

```bash
aws cloudformation deploy \
  --template-file bedrock-data-lake.yaml \
  --stack-name bedrock-data-lake \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    SourceAccountReplicationRoleArns=arn:aws:iam::466959819186:role/bedrock-s3-replication-role,arn:aws:iam::904247366374:role/bedrock-s3-replication-role
```

After the stack reaches `CREATE_COMPLETE` or `UPDATE_COMPLETE`, note the central account ID:

```bash
aws sts get-caller-identity --query Account --output text
# Example output: 123456789012  <-- this is your <central-account-id>
```

---

## Step 2: Deploy Updated `bedrock-logging.yaml` to Each Source Account

Run this command once per source account, substituting the actual central account ID.

**Source account `466959819186`:**

```bash
aws cloudformation deploy \
  --template-file bedrock-logging.yaml \
  --stack-name bedrock-observability \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides CentralDataLakeAccountId=<central-account-id>
```

**Source account `904247366374`** (repeat with credentials for this account):

```bash
aws cloudformation deploy \
  --template-file bedrock-logging.yaml \
  --stack-name bedrock-observability \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides CentralDataLakeAccountId=<central-account-id>
```

Both stacks must reach `CREATE_COMPLETE` or `UPDATE_COMPLETE` before proceeding.

---

## Step 3: Invoke a Bedrock Model to Generate a Log Entry

Make a Bedrock model invocation to produce an invocation log. Run this from each source account to generate log entries in both.

```bash
aws bedrock-runtime invoke-model \
  --model-id "amazon.nova-pro-v1:0" \
  --body '{"messages":[{"role":"user","content":[{"type":"text","text":"Say hello"}]}],"max_tokens":10,"anthropic_version":"bedrock-2023-05-31"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/bedrock-response.json

cat /tmp/bedrock-response.json
```

> **Note**: Model invocation logging must be enabled in Bedrock console settings for the account and region before logs are written to S3. Confirm under **Amazon Bedrock → Settings → Model invocation logging**.

---

## Step 4: Assert the Log Object Appears in the Source Bucket (within 60 seconds)

Bedrock writes the log object to the source bucket within approximately 60 seconds of the invocation.

Replace `<accountId>` and `<region>` with the source account values (e.g., `466959819186` and `us-east-1`):

```bash
aws s3 ls s3://bedrock-logs-<accountId>-<region>/<accountId>/ --recursive
```

**Example for account `466959819186`:**

```bash
aws s3 ls s3://bedrock-logs-466959819186-us-east-1/466959819186/ --recursive
```

**Expected**: At least one `.json.gz` object listed under the account prefix.

If no object appears after 60 seconds, check that model invocation logging is enabled and the `BedrockLoggingRole` has `s3:PutObject` permission on the bucket.

---

## Step 5: Assert the Same Object Appears in the Central Data Lake Bucket (within 15 minutes)

S3 cross-account replication is asynchronous. The object should appear in the central bucket within 15 minutes under normal conditions (15 seconds with S3 Replication Time Control enabled).

Replace `<centralAccountId>` with the central account ID:

```bash
aws s3 ls s3://bedrock-data-lake-<centralAccountId>/logs/ --recursive
```

**Expected**: The same key that appeared in the source bucket is now present under the `logs/` prefix in the central bucket.

Poll every 30 seconds if needed:

```bash
watch -n 30 "aws s3 ls s3://bedrock-data-lake-<centralAccountId>/logs/ --recursive"
```

If the object does not appear after 15 minutes, see [Troubleshooting: Cross-Account Permission Denial](#error-scenario-4-cross-account-permission-denial) and [Troubleshooting: Replication Lag](#error-scenario-1-replication-lag).

---

## Step 6: Trigger the Glue Crawler Manually

The Glue Crawler runs automatically at 06:00 UTC daily, but trigger it now to register the new partitions immediately.

```bash
aws glue start-crawler --name bedrock-logs-crawler
```

---

## Step 7: Poll for Crawler Completion

The crawler typically finishes within 1–3 minutes for small data sets. Poll until the state returns `READY`:

```bash
aws glue get-crawler \
  --name bedrock-logs-crawler \
  --query 'Crawler.State'
```

Repeat until the output is `"READY"`. A state of `"RUNNING"` means the crawl is still in progress. A state of `"STOPPING"` means it is finishing up.

---

## Step 8: Run an Athena Query Against the Data Lake

Query the `bedrock_logs.bedrock_invocations` table in the `bedrock-analytics` workgroup. Replace the placeholder values with the actual source account ID, year, and month.

```sql
SELECT COUNT(*) FROM bedrock_logs.bedrock_invocations
WHERE account_id='<sourceAccountId>'
AND year='<year>'
AND month='<month>';
```

**Example for account `466959819186`, June 2025:**

```sql
SELECT COUNT(*) FROM bedrock_logs.bedrock_invocations
WHERE account_id='466959819186'
AND year='2025'
AND month='06';
```

Run it via AWS CLI:

```bash
QUERY_ID=$(aws athena start-query-execution \
  --query-string "SELECT COUNT(*) FROM bedrock_logs.bedrock_invocations WHERE account_id='466959819186' AND year='2025' AND month='06';" \
  --work-group bedrock-analytics \
  --query 'QueryExecutionId' \
  --output text)

# Poll for completion
aws athena get-query-execution \
  --query-execution-id "$QUERY_ID" \
  --query 'QueryExecution.Status.State'

# Retrieve results once state is SUCCEEDED
aws athena get-query-results \
  --query-execution-id "$QUERY_ID"
```

**Expected**: `COUNT(*)` returns a value greater than zero, confirming the invocation log was ingested, replicated, crawled, and is queryable.

---

## Troubleshooting

### Error Scenario 1: Replication Lag

**Symptom**: The log object is present in the source bucket but has not appeared in the central bucket after 15 minutes.

**Cause**: S3 cross-account replication is asynchronous. Under high load, replication can exceed the typical 15-minute window.

**Resolution**:
1. Check the source bucket's replication metrics in CloudWatch — look for `ReplicationLatency` on the `bedrock-logs-<accountId>-<region>` bucket.
2. Verify the replication rule is enabled:
   ```bash
   aws s3api get-bucket-replication --bucket bedrock-logs-<accountId>-<region>
   ```
3. To enforce a 15-minute SLA, enable S3 Replication Time Control (RTC) by updating `bedrock-logging.yaml` with `ReplicationTime` and `Metrics` blocks on the replication destination.
4. If objects are permanently stuck, re-trigger replication:
   ```bash
   aws s3api put-bucket-replication \
     --bucket bedrock-logs-<accountId>-<region> \
     --replication-configuration file://replication.json
   ```

---

### Error Scenario 2: Schema Evolution

**Symptom**: The Glue Crawler run history shows a schema change warning, or Athena queries return unexpected `null` values for new fields.

**Cause**: AWS added new fields to the Bedrock invocation log schema that are not present in the Glue table definition.

**Resolution**:
1. Review the Glue Crawler run log in CloudWatch Logs (log group `/aws-glue/crawlers`).
2. The crawler's `SchemaChangePolicy` is set to `UPDATE_IN_DATABASE`, so new columns are added automatically on the next crawl — re-trigger the crawler:
   ```bash
   aws glue start-crawler --name bedrock-logs-crawler
   ```
3. After the crawl completes, verify the updated table schema:
   ```bash
   aws glue get-table \
     --database-name bedrock_logs \
     --name bedrock_invocations \
     --query 'Table.StorageDescriptor.Columns[*].Name'
   ```
4. Existing Athena queries continue to work because the JSON SerDe tolerates additional columns. Update the `BedrockLogsGlueTable` resource in `bedrock-data-lake.yaml` to persist the schema change in source control.

---

### Error Scenario 3: Partition Mismatch

**Symptom**: The Glue Crawler completes but Athena queries return zero rows, or the crawler adds partitions with unexpected key paths.

**Cause**: The source bucket writes log objects under `{accountId}/{key}` but the Hive partition layout expected by the Glue table is `account_id={accountId}/region={region}/year={year}/month={month}/day={day}/{key}`. If keys do not match the `storage.location.template` in the table definition, partitions will not align.

**Resolution (Option A — Glue ETL, chosen approach)**:
1. Inspect the raw key structure in the central bucket:
   ```bash
   aws s3 ls s3://bedrock-data-lake-<centralAccountId>/logs/ --recursive | head -20
   ```
2. Compare the actual key path to the partition template in the Glue table definition.
3. Run the daily Glue ETL job to reorganise objects into Hive-compatible prefixes, then re-trigger the crawler.
4. If the Glue table's `storage.location.template` needs updating, redeploy `bedrock-data-lake.yaml` after modifying the template string.

**Resolution (Option B — Lambda rename, upgrade path)**:
- Configure an S3 Event Notification on the central bucket to trigger a Lambda function that copies each arriving object to the correctly partitioned prefix and deletes the original. This provides near-real-time partitioning without waiting for the daily ETL job.

---

### Error Scenario 4: Cross-Account Permission Denial

**Symptom**: Objects are not appearing in the central bucket, and the source bucket's CloudWatch metrics show `ReplicationFailed` events. Alternatively, `aws s3api head-object` on the central bucket returns `403 AccessDenied`.

**Cause**: The central bucket policy does not include the source account's replication role ARN in the `AllowCrossAccountReplication` statement, or the source account's replication role lacks `s3:ReplicateObject` permission on the central bucket path.

**Resolution**:
1. Confirm the source account's replication role ARN is listed in the central stack parameter:
   ```bash
   aws cloudformation describe-stacks \
     --stack-name bedrock-data-lake \
     --query "Stacks[0].Parameters[?ParameterKey=='SourceAccountReplicationRoleArns'].ParameterValue"
   ```
2. If a source account ARN is missing, redeploy `bedrock-data-lake.yaml` with the updated parameter (comma-separated):
   ```bash
   aws cloudformation deploy \
     --template-file bedrock-data-lake.yaml \
     --stack-name bedrock-data-lake \
     --capabilities CAPABILITY_IAM \
     --parameter-overrides \
       SourceAccountReplicationRoleArns=arn:aws:iam::466959819186:role/bedrock-s3-replication-role,arn:aws:iam::904247366374:role/bedrock-s3-replication-role
   ```
3. After the central bucket policy is updated, verify the replication rule in the source account and re-apply it if needed:
   ```bash
   aws s3api get-bucket-replication --bucket bedrock-logs-<accountId>-<region>
   ```
4. Monitor the `ReplicationFailed` CloudWatch metric to confirm the error clears after the policy update.

---

## Reference

| Resource | Name pattern |
|---|---|
| Source log bucket (account 1) | `bedrock-logs-466959819186-us-east-1` |
| Source log bucket (account 2) | `bedrock-logs-904247366374-us-east-1` |
| Central data lake bucket | `bedrock-data-lake-<centralAccountId>` |
| Athena results bucket | `bedrock-athena-results-<centralAccountId>` |
| Glue database | `bedrock_logs` |
| Glue table | `bedrock_invocations` |
| Glue Crawler | `bedrock-logs-crawler` |
| Athena WorkGroup | `bedrock-analytics` |
| Source CloudFormation stack | `bedrock-observability` |
| Central CloudFormation stack | `bedrock-data-lake` |
