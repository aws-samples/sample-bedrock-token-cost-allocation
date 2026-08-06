---
inclusion: always
---

# Deploying This Project

When a user asks you to deploy, run, or set up this project, follow this workflow before executing any AWS CLI commands.

## Step 1 — Ask how they want to authenticate

Ask the user exactly one question:

> "How do you want to authenticate with AWS? Options:
> - **A) Named profiles** — you have entries in `~/.aws/config` like `[profile my-account]`
> - **B) AWS SSO** — you log in with `aws sso login`
> - **C) Environment variables / instance role** — credentials are already active in your terminal"

Wait for their answer before proceeding.

## Step 2 — Collect account details

Based on their choice:

**If A or B (profiles):**
Ask: "What are your AWS CLI profile names for the central account and the linked account?"
Then set:
```bash
export CENTRAL_PROFILE=<answer>
export LINKED_PROFILE=<answer>
export CENTRAL_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --profile $CENTRAL_PROFILE)
export LINKED_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --profile $LINKED_PROFILE)
export AWS_REGION=us-east-1
```

**If C (env vars / instance role):**
Ask: "What are your central and linked AWS account IDs?"
Remind them they will need to switch credentials between commands for the two accounts.

## Step 3 — Verify credentials before any deployment

Always run these two checks and confirm both return the expected account IDs before proceeding:

```bash
aws sts get-caller-identity --profile $CENTRAL_PROFILE
aws sts get-caller-identity --profile $LINKED_PROFILE
```

If either fails, stop and help the user fix their credentials before continuing.

## Step 4 — Follow the deployment guide

All deployment commands are in [deploy.md](../deploy.md). Follow the steps in order. Do not skip steps or run them out of order — the cross-account replication depends on both stacks existing before the bucket policy is applied.

## Key constraints to enforce

- **Region is always `us-east-1`** — do not change this without warning the user (the Bedrock foundation model ARN is region-specific)
- **Always wait for stack completion** before the next step — use `aws cloudformation wait stack-create-complete`
- **Never delete S3 buckets automatically** — they have `DeletionPolicy: Retain`; emptying them requires explicit user confirmation
- **The bucket policy (Step 4) must be applied via CLI**, not CloudFormation — explain why if the user asks

## Personal deployment config

If the user already has account IDs and profiles configured, they may have a personal `aws-deployment.md` file in `.kiro/specs/bedrock-logging-data-lake/` with their actual values. Check if it exists and offer to use those values directly.
