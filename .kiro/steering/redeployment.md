---
inclusion: always
---

# Re-deploying After a Stack Delete

Both templates use fixed physical names and `DeletionPolicy: Retain` on stateful resources. Deleting a stack therefore leaves resources behind, and a fresh `create-stack` fails because those names are already taken.

When a user asks you to re-deploy, redeploy, or "try again" after deleting a stack, run the pre-flight check below **before** any `create-stack` call.

## Recognising the failure

The symptom is a stack-level failure with **no resource-level events**:

```text
CREATE_FAILED  AWS::CloudFormation::Stack
Validation failed with N error(s). Call DescribeEvents to retrieve the full list
of issues with resource and property details, resolve each error, then retry.
```

`describe-stack-events` shows only the stack itself, never the offending resource. The error count is a rough signal of how many leftovers remain — it drops by one as each is removed. Do not chase this as a template bug; check for leftovers first.

## Retained resources to check

### Central account — `bedrock-firehose-data-lake.yaml`

`DeletionPolicy: Retain` (survive stack deletion):

| Resource | Physical name |
|---|---|
| S3 data lake | `bedrock-firehose-lake-<account-id>` |
| S3 Athena results | `bedrock-athena-results-<account-id>` |
| Athena workgroup | `bedrock-analytics` |
| CloudWatch log group | `/aws/kinesisfirehose/bedrock-invocations` |
| KMS key | Athena results CMK (alias `alias/bedrock-athena-results-key`) |

Fixed names that also block re-creation if a delete stalls or is partial:

```text
IAM roles       bedrock-firehose-role, bedrock-cwl-destination-role,
                bedrock-cwl-unpack-lambda-role, BedrockQuickSightDataSourceRole
Managed policies bedrock-firehose-role-policy, bedrock-firehose-lambda-invoke
KMS alias       alias/bedrock-firehose-data-lake-key
Firehose        bedrock-invocations-v2
CWL destination bedrock-firehose-destination-v2
Lambda          bedrock-cwl-unpack
Glue            database bedrock_logs, table bedrock_invocations
SNS topic       bedrock-firehose-alarms
CW alarms       bedrock-firehose-data-freshness, bedrock-firehose-delivery-success
```

### Linked account — `bedrock-logging.yaml`

`DeletionPolicy: Retain`:

| Resource | Physical name |
|---|---|
| CloudWatch log group | `/aws/bedrock/modelinvocations` |
| KMS key | Bedrock logs CMK (alias `alias/bedrock-logs-key`) |

Fixed names:

```text
IAM roles        bedrock-logging-role, bedrock-cwl-subscription-filter-role
Managed policy   bedrock-logging-policy-<region>
Subscription     bedrock-firehose-subscription
```

## Pre-flight check

Run these before re-deploying and report anything that exists. Central account:

```bash
aws s3api head-bucket --bucket bedrock-firehose-lake-$CENTRAL_ACCOUNT_ID --profile $CENTRAL_PROFILE
aws s3api head-bucket --bucket bedrock-athena-results-$CENTRAL_ACCOUNT_ID --profile $CENTRAL_PROFILE
aws athena get-work-group --work-group bedrock-analytics --region $AWS_REGION --profile $CENTRAL_PROFILE
aws logs describe-log-groups --log-group-name-prefix /aws/kinesisfirehose/bedrock-invocations --region $AWS_REGION --profile $CENTRAL_PROFILE
aws glue get-database --name bedrock_logs --region $AWS_REGION --profile $CENTRAL_PROFILE
aws kms list-aliases --region $AWS_REGION --profile $CENTRAL_PROFILE --query 'Aliases[?contains(AliasName, `bedrock`)]'
aws logs describe-destinations --destination-name-prefix bedrock-firehose-destination-v2 --region $AWS_REGION --profile $CENTRAL_PROFILE
```

Linked account:

```bash
aws logs describe-log-groups --log-group-name-prefix /aws/bedrock/modelinvocations --region $AWS_REGION --profile $LINKED_PROFILE
aws kms list-aliases --region $AWS_REGION --profile $LINKED_PROFILE --query 'Aliases[?contains(AliasName, `bedrock`)]'
```

Also delete any `ROLLBACK_COMPLETE` stack shell before retrying — it holds the stack name but no resources, and `create-stack` will fail with `AlreadyExistsException` while it exists. Deleting that shell is safe.

## Deletion rules

- **Never delete a retained resource without explicit user confirmation**, even when it looks empty.
- **S3 buckets:** `list-objects-v2` can return nothing while the bucket still holds objects or delete markers. Always confirm with `list-object-versions` before telling the user a bucket is empty, and re-confirm with them if objects are found. `delete-bucket` fails with `BucketNotEmpty` in that case.
- **Log groups and Athena workgroups** may hold data the user still wants. Report them and let the user decide.
- **KMS keys** cannot be deleted immediately — they schedule for deletion (7–30 days). Point out that a re-deploy will create a new CMK and that the old key may still be needed to read previously encrypted objects.
- Prefer letting the user delete retained resources from the console when they want to review contents first.

## If validation still fails with no leftovers

Direct `create-stack` has failed with the opaque `Validation failed with 1 error(s)` message even when every leftover was confirmed absent. Deploying the identical template and parameters through a change set succeeded in both accounts:

```bash
CS=$(aws cloudformation create-change-set \
  --stack-name <stack> \
  --change-set-name redeploy-$(date +%s) \
  --change-set-type CREATE \
  --template-body file://<template>.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters <parameters> \
  --region $AWS_REGION --profile <profile> \
  --query Id --output text)

aws cloudformation describe-change-set --change-set-name "$CS" --region $AWS_REGION --profile <profile> \
  --query '[Status,StatusReason]'

aws cloudformation execute-change-set --change-set-name "$CS" --region $AWS_REGION --profile <profile>
```

The change set also surfaces template validation errors that `create-stack` hides. Use this path rather than repeatedly retrying `create-stack`.

## Deployment order

Re-deploy in the same order as a first-time install: central `bedrock-firehose-data-lake` first, then linked `bedrock-logging` with the central `CWLDestinationArn`. The linked subscription filter cannot be created until the central destination exists.

After a central re-deploy, re-check downstream configuration that lives outside CloudFormation:

- Bedrock model invocation logging in each linked account (CloudWatch only, no `s3Config`)
- The Athena `bedrock_invocations_view` saved query, which must be run again
- QuickSight Athena datasources, which must point at `BedrockQuickSightDataSourceRole`
