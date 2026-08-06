# Deployment Guide

This guide covers deploying the Bedrock Token Cost Allocation solution. It works alongside the [README](./README.md) and is designed to be used with an AI assistant like Kiro or Claude Code.

---

## Before you start

You need two AWS accounts:

| Account | Role | Description |
|---------|------|-------------|
| Central (org) | Receives logs | Runs Glue ETL, Athena, and hosts the data lake S3 bucket |
| Linked | Runs workloads | Runs Bedrock, replicates logs to central |

Both accounts must be accessible from your local machine. See **Authentication** below to configure access.

**Region:** Deploy everything to `us-east-1`. The Application Inference Profile references an Amazon Nova Pro foundation model ARN that is only available in that region.

---

## Authentication

Choose the method that matches how you access AWS:

### Option A — Named AWS CLI profiles (recommended)

If you use `~/.aws/config` named profiles:

```bash
# Set these once before running any deployment commands
export CENTRAL_PROFILE=<your-central-account-profile>
export LINKED_PROFILE=<your-linked-account-profile>

# Resolve account IDs from live credentials
export CENTRAL_ACCOUNT_ID=$(aws sts get-caller-identity \
  --query Account --output text --profile $CENTRAL_PROFILE)
export LINKED_ACCOUNT_ID=$(aws sts get-caller-identity \
  --query Account --output text --profile $LINKED_PROFILE)
export AWS_REGION=us-east-1

echo "Central: $CENTRAL_ACCOUNT_ID  Linked: $LINKED_ACCOUNT_ID"
```

Verify both work:

```bash
aws sts get-caller-identity --profile $CENTRAL_PROFILE
aws sts get-caller-identity --profile $LINKED_PROFILE
```

### Option B — AWS SSO (Identity Center)

If you log in via `aws sso login`:

```bash
aws sso login --profile <your-sso-profile>

export CENTRAL_PROFILE=<central-sso-profile>
export LINKED_PROFILE=<linked-sso-profile>

export CENTRAL_ACCOUNT_ID=$(aws sts get-caller-identity \
  --query Account --output text --profile $CENTRAL_PROFILE)
export LINKED_ACCOUNT_ID=$(aws sts get-caller-identity \
  --query Account --output text --profile $LINKED_PROFILE)
export AWS_REGION=us-east-1
```

### Option C — Environment variables / instance role

If credentials are already in environment variables or attached to the machine:

```bash
# You still need two separate credential sets for the two accounts.
# Use AWS STS assume-role to get temporary credentials for the second account:
export CENTRAL_ACCOUNT_ID=<central-account-id>
export LINKED_ACCOUNT_ID=<linked-account-id>
export AWS_REGION=us-east-1

# Unset CENTRAL_PROFILE and LINKED_PROFILE — commands will use default credentials
unset CENTRAL_PROFILE
unset LINKED_PROFILE
```

> For Option C, append `--profile` flags to the CLI commands below only when switching between accounts, or run them in separate terminal sessions with different credentials active.

---

## Deployment steps

Once your environment variables are set, run these in order.

### Step 1 — Deploy the data lake to the central account

```bash
aws cloudformation create-stack \
  --stack-name bedrock-data-lake \
  --template-body file://bedrock-data-lake.yaml \
  --capabilities CAPABILITY_IAM \
  --region $AWS_REGION \
  --profile $CENTRAL_PROFILE

aws cloudformation wait stack-create-complete \
  --stack-name bedrock-data-lake \
  --region $AWS_REGION \
  --profile $CENTRAL_PROFILE
```

### Step 2 — Deploy the logging stack to each linked account

```bash
aws cloudformation create-stack \
  --stack-name bedrock-logging \
  --template-body file://bedrock-logging.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters ParameterKey=CentralDataLakeAccountId,ParameterValue=$CENTRAL_ACCOUNT_ID \
  --region $AWS_REGION \
  --profile $LINKED_PROFILE

aws cloudformation wait stack-create-complete \
  --stack-name bedrock-logging \
  --region $AWS_REGION \
  --profile $LINKED_PROFILE
```

### Step 3 — Retrieve the replication role ARN

```bash
REPLICATION_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name bedrock-logging \
  --query 'Stacks[0].Outputs[?OutputKey==`BedrockS3ReplicationRoleArn`].OutputValue' \
  --output text \
  --region $AWS_REGION \
  --profile $LINKED_PROFILE)

echo "Replication role: $REPLICATION_ROLE_ARN"
```

### Step 4 — Apply the bucket policy to the central account

```bash
aws s3api put-bucket-policy \
  --bucket bedrock-data-lake-${CENTRAL_ACCOUNT_ID} \
  --region $AWS_REGION \
  --profile $CENTRAL_PROFILE \
  --policy "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {
        \"Sid\": \"DenyNonHTTPS\",
        \"Effect\": \"Deny\",
        \"Principal\": \"*\",
        \"Action\": \"s3:*\",
        \"Resource\": [
          \"arn:aws:s3:::bedrock-data-lake-${CENTRAL_ACCOUNT_ID}\",
          \"arn:aws:s3:::bedrock-data-lake-${CENTRAL_ACCOUNT_ID}/*\"
        ],
        \"Condition\": {\"Bool\": {\"aws:SecureTransport\": \"false\"}}
      },
      {
        \"Sid\": \"AllowCrossAccountReplication\",
        \"Effect\": \"Allow\",
        \"Principal\": {
          \"AWS\": [\"${REPLICATION_ROLE_ARN}\"]
        },
        \"Action\": [
          \"s3:ReplicateObject\",
          \"s3:ReplicateDelete\",
          \"s3:ReplicateTags\",
          \"s3:ObjectOwnerOverrideToBucketOwner\"
        ],
        \"Resource\": \"arn:aws:s3:::bedrock-data-lake-${CENTRAL_ACCOUNT_ID}/logs/*\"
      }
    ]
  }"
```

For multiple linked accounts, add each replication role ARN to the `AWS` array above and re-run.

### Step 5 — Enable Bedrock model invocation logging (linked account)

```bash
LOGGING_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name bedrock-logging \
  --query 'Stacks[0].Outputs[?OutputKey==`BedrockLoggingRoleArn`].OutputValue' \
  --output text \
  --region $AWS_REGION \
  --profile $LINKED_PROFILE)

aws bedrock put-model-invocation-logging-configuration \
  --logging-config "{
    \"cloudWatchConfig\": {
      \"logGroupName\": \"/aws/bedrock/modelinvocations\",
      \"roleArn\": \"$LOGGING_ROLE_ARN\"
    },
    \"s3Config\": {
      \"bucketName\": \"bedrock-logs-${LINKED_ACCOUNT_ID}-${AWS_REGION}\",
      \"keyPrefix\": \"${LINKED_ACCOUNT_ID}/\"
    },
    \"textDataDeliveryEnabled\": true,
    \"imageDataDeliveryEnabled\": true,
    \"embeddingDataDeliveryEnabled\": true
  }" \
  --region $AWS_REGION \
  --profile $LINKED_PROFILE
```

### Step 6 — Verify end-to-end

```bash
# Get inference profile ARN
INFERENCE_PROFILE_ARN=$(aws cloudformation describe-stacks \
  --stack-name bedrock-logging \
  --query 'Stacks[0].Outputs[?OutputKey==`BedrockInferenceProfileArn`].OutputValue' \
  --output text \
  --region $AWS_REGION \
  --profile $LINKED_PROFILE)

# Run test invocation
python scripts/test_bedrock_invocation.py \
  --profile $LINKED_PROFILE \
  --region $AWS_REGION \
  --stack-name bedrock-logging

# Check logs in linked account source bucket (allow a few minutes)
aws s3 ls s3://bedrock-logs-${LINKED_ACCOUNT_ID}-${AWS_REGION}/ \
  --recursive --profile $LINKED_PROFILE

# Check replication arrived in central account
aws s3 ls s3://bedrock-data-lake-${CENTRAL_ACCOUNT_ID}/ \
  --recursive --profile $CENTRAL_PROFILE

# Trigger Glue ETL immediately (normally runs at 05:30 UTC)
aws glue start-job-run \
  --job-name bedrock-logs-etl \
  --region $AWS_REGION \
  --profile $CENTRAL_PROFILE
```

---

## Teardown

```bash
# Empty S3 buckets first (they have DeletionPolicy: Retain)
aws s3 rm s3://bedrock-logs-${LINKED_ACCOUNT_ID}-${AWS_REGION} --recursive --profile $LINKED_PROFILE
aws s3 rm s3://bedrock-data-lake-${CENTRAL_ACCOUNT_ID} --recursive --profile $CENTRAL_PROFILE
aws s3 rm s3://bedrock-athena-results-${CENTRAL_ACCOUNT_ID} --recursive --profile $CENTRAL_PROFILE

# Delete stacks
aws cloudformation delete-stack \
  --stack-name bedrock-logging \
  --region $AWS_REGION \
  --profile $LINKED_PROFILE

aws cloudformation delete-stack \
  --stack-name bedrock-data-lake \
  --region $AWS_REGION \
  --profile $CENTRAL_PROFILE
```
