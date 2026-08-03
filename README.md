# CFM Demo Account - Infrastructure

Demo account infrastructure for the AWS Cloud Financial Management TFC demo environment.

---

## Bedrock Logging Data Lake

Centralises Bedrock model invocation logs from all linked accounts into the org account for analysis via Athena.

### Architecture

```
Linked Account (466959819186)          Org Account (260990198475)
  bedrock-logging.yaml                   bedrock-data-lake.yaml
  ┌─────────────────────┐               ┌──────────────────────────┐
  │ Bedrock service      │               │ bedrock-data-lake-{id}   │
  │   ↓ logs             │  S3 replication│   /logs/{accountId}/... │
  │ bedrock-logs-{id}   │ ─────────────▶│                          │
  │   + replication role │               │ Glue: bedrock_logs DB    │
  └─────────────────────┘               │ Athena: bedrock-analytics│
                                        └──────────────────────────┘
Linked Account (904247366374)
  bedrock-logging.yaml
  (same as above)
```

### Deployment Order

**This order matters** — the org account stack must exist before the bucket policy can reference linked account role ARNs.

#### Step 1 — Deploy data lake to org account (260990198475)

The initial deploy omits `BedrockDataLakeBucketPolicy` because CFN's `ResourceExistenceCheck` hook validates that IAM principal ARNs in bucket policies exist before deploying. The linked account replication roles don't exist yet at this point.

```bash
aws cloudformation create-stack \
  --stack-name bedrock-data-lake \
  --template-body file://bedrock-data-lake.yaml \
  --capabilities CAPABILITY_IAM \
  --region us-east-1
```

#### Step 2 — Deploy logging stack to each linked account

Deploy `bedrock-logging.yaml` to both linked accounts, passing the org account ID as the `CentralDataLakeAccountId` parameter. This creates the local logging infrastructure and the replication role that ships logs to the org account.

```bash
# Account 466959819186
aws cloudformation create-stack \
  --stack-name bedrock-logging \
  --template-body file://bedrock-logging.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters ParameterKey=CentralDataLakeAccountId,ParameterValue=260990198475 \
  --region us-east-1

# Account 904247366374
aws cloudformation create-stack \
  --stack-name bedrock-logging \
  --template-body file://bedrock-logging.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters ParameterKey=CentralDataLakeAccountId,ParameterValue=260990198475 \
  --region us-east-1
```

#### Step 3 — Get replication role ARNs from linked accounts

```bash
aws cloudformation describe-stacks \
  --stack-name bedrock-logging \
  --query 'Stacks[0].Outputs[?OutputKey==`BedrockS3ReplicationRoleArn`].OutputValue' \
  --output text
```

Expected ARNs:
- `arn:aws:iam::466959819186:role/bedrock-s3-replication-role`
- `arn:aws:iam::904247366374:role/bedrock-s3-replication-role`

#### Step 4 — Add bucket policy back to data lake template

Add `BedrockDataLakeBucketPolicy` back into `bedrock-data-lake.yaml` (it's noted with a comment in the template), then update the org account stack:

```bash
aws cloudformation update-stack \
  --stack-name bedrock-data-lake \
  --template-body file://bedrock-data-lake.yaml \
  --capabilities CAPABILITY_IAM \
  --parameters \
    ParameterKey=SourceAccountReplicationRoleArns,ParameterValue="arn:aws:iam::466959819186:role/bedrock-s3-replication-role,arn:aws:iam::904247366374:role/bedrock-s3-replication-role" \
  --region us-east-1
```

The bucket policy grants the replication roles `s3:ReplicateObject`, `s3:ReplicateDelete`, `s3:ReplicateTags`, and `s3:ObjectOwnerOverrideToBucketOwner` on the `logs/` prefix. Without this, replication will fail with `AccessDenied`.

#### Step 5 — Enable Bedrock model invocation logging (manual, per linked account)

This cannot be done via CloudFormation. Run in each linked account after the logging stack is deployed:

```bash
# Get the role ARN and bucket name from stack outputs first
ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name bedrock-logging \
  --query 'Stacks[0].Outputs[?OutputKey==`BedrockLoggingRoleArn`].OutputValue' \
  --output text)

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws bedrock put-model-invocation-logging-configuration \
  --logging-config "{
    \"cloudWatchConfig\": {
      \"logGroupName\": \"/aws/bedrock/modelinvocations\",
      \"roleArn\": \"$ROLE_ARN\"
    },
    \"s3Config\": {
      \"bucketName\": \"bedrock-logs-${ACCOUNT_ID}-us-east-1\",
      \"keyPrefix\": \"${ACCOUNT_ID}/\"
    },
    \"textDataDeliveryEnabled\": true,
    \"imageDataDeliveryEnabled\": true,
    \"embeddingDataDeliveryEnabled\": true
  }" \
  --region us-east-1
```

### Known Issues & Gotchas

**CFN ResourceExistenceCheck on bucket policies**
CFN's early validation hook rejects bucket policies where the IAM principal ARNs don't yet exist. This is why the initial deploy omits the bucket policy — deploy linked account stacks first, then apply the bucket policy via the CLI (see below).

**Why the bucket policy must be applied via CLI, not CFN update-stack**
When CFN resolves an IAM role ARN in a bucket policy principal, it stores the role's unique ID rather than the ARN. If the role was created after the initial stack deploy, or deleted and recreated, the stored ID can become stale and replication fails silently with `AccessDenied`. The `put-bucket-policy` CLI call is reliable because S3 resolves ARNs to current role IDs at write time. Use this instead of `update-stack` for the bucket policy:

```bash
aws s3api put-bucket-policy \
  --bucket bedrock-data-lake-260990198475 \
  --region us-east-1 \
  --policy '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "DenyNonHTTPS",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:*",
        "Resource": [
          "arn:aws:s3:::bedrock-data-lake-260990198475",
          "arn:aws:s3:::bedrock-data-lake-260990198475/*"
        ],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}}
      },
      {
        "Sid": "AllowCrossAccountReplication",
        "Effect": "Allow",
        "Principal": {
          "AWS": [
            "arn:aws:iam::466959819186:role/bedrock-s3-replication-role",
            "arn:aws:iam::904247366374:role/bedrock-s3-replication-role"
          ]
        },
        "Action": [
          "s3:ReplicateObject",
          "s3:ReplicateDelete",
          "s3:ReplicateTags",
          "s3:ObjectOwnerOverrideToBucketOwner"
        ],
        "Resource": "arn:aws:s3:::bedrock-data-lake-260990198475/logs/*"
      }
    ]
  }'
```

**CRAWL_NEW_FOLDERS_ONLY requires LOG for both SchemaChangePolicy values**
When `RecrawlBehavior: CRAWL_NEW_FOLDERS_ONLY`, Glue requires both `UpdateBehavior` and `DeleteBehavior` to be `LOG`. Setting `UpdateBehavior: UPDATE_IN_DATABASE` will fail with HTTP 400.

**S3 replication only applies to new objects**
Objects written before the replication rule was configured are not replicated. Invoke a new model request after all stacks are deployed to generate a fresh log entry.

**Replication requires both source role permissions AND destination bucket policy**
Cross-account S3 replication needs:
1. The replication role in the source account can read the source bucket (handled by `BedrockS3ReplicationRole` in `bedrock-logging.yaml`)
2. The destination bucket policy explicitly allows the source replication role to write (handled by the `put-bucket-policy` call above)

Both are required. Removing either breaks replication — objects won't replicate and you'll see `AccessDenied` in S3 replication metrics.

### Querying Logs in Athena

After logs are flowing, use the `bedrock-analytics` workgroup in the org account:

```sql
-- Count invocations by account and model
SELECT account_id, modelid, COUNT(*) as invocations
FROM bedrock_logs.bedrock_invocations
WHERE year='2026' AND month='07'
GROUP BY account_id, modelid
ORDER BY invocations DESC;
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

