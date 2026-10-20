# Bedrock Token Cost Allocation — Project Overview

Amazon Bedrock bills at the account level. Your Cost and Usage Report (CUR) tells you that one
account spent $40,000 on Claude Sonnet last month — it doesn't tell you which team, which
application, or which agent drove that spend. For most organizations running Bedrock at scale,
that's the gap between having a cloud bill and being able to act on it.

This solution closes it. It captures every Bedrock model invocation across your organization,
lands it in a central Parquet data lake, and joins it to CUR on the calling IAM principal and the
hourly usage window. The result is per-team, per-application, per-principal token and cost
attribution you can query in Athena or chart in QuickSight.

Two CloudFormation stacks, no application changes, no agents to install.

---

## Why deploy it

**You can finally charge back.** Invocation logs join to CUR on the normalized IAM role ARN, so
spend maps to the identity that caused it. Add your activated cost allocation tags and you have
showback or chargeback by team, project, or cost center — without asking every application team to
re-instrument their code.

**It scales across the organization without per-account toil.** The central CloudWatch Logs
destination is gated by `aws:PrincipalOrgID` rather than an enumerated account list. Onboarding a
new account is one small stack, not a change to the central template.

**Query costs stay predictable.** Firehose converts JSON to Snappy Parquet inline, so Athena scans
columnar data rather than raw gzipped JSON. Athena partition projection means no crawler to run, no
partition metadata to repair, and no Glue ETL job billed by the DPU-hour. The Athena workgroup
enforces a 10 GiB per-query scan cap, so a careless `SELECT *` can't produce a surprise line item of
its own.

**Token-level detail, not just dollars.** Input, output, cache-read, and cache-write token counts
are extracted per invocation, alongside stop reason and prompt/response previews. That's what you
need to answer the questions that follow the cost question: is this workload growing or just
inefficient? Would prompt caching pay off? Is a cheaper model viable for this traffic pattern?

**Security posture is built in.** Customer-managed KMS keys on both the log group and the data lake,
HTTPS-only bucket policies, full public access blocking, `BucketOwnerEnforced` ownership, and
least-privilege scoped roles for Firehose, CloudWatch Logs, and QuickSight. Stateful resources carry
`DeletionPolicy: Retain` so a stack teardown can't take historical data with it. The repo ships
cfn-guard rules and a pytest suite that assert these properties — including that no role is
assumable by a wildcard principal and that the cross-account destination stays org-scoped.

**It's operable.** CloudWatch alarms on Firehose data freshness and delivery success publish to SNS,
so you learn about a broken pipeline from a notification rather than from a gap in a dashboard three
weeks later. Delivery errors land in a dedicated log group and a separate `errors/` S3 prefix.

### Caveat worth stating plainly

This captures invocation metadata **including prompt and response text**. Review it against your
data classification and retention policy before deploying into an environment where models handle
sensitive input.

---

## How it works

```
Source account                          Central account
──────────────                          ───────────────
Bedrock invocation
      │
      ▼
/aws/bedrock/modelinvocations  ──▶  CWL Destination (org-scoped)
  (CWL, CMK-encrypted)                    │
      subscription filter                 ▼
                                    Firehose (DirectPut)
                                          │
                                    Native CWL decompression
                                    + message extraction
                                          │
                                    Inline JSON ▶ Parquet (Glue schema)
                                          │
                                          ▼
                                    S3 data lake (CMK, date-partitioned)
                                          │
                                    Athena ─▶ QuickSight
                                          │
                                    JOIN CUR ─▶ cost attribution
```

Bedrock writes model invocation logs to CloudWatch Logs in each source account. A subscription
filter forwards that log group to a cross-account CloudWatch Logs destination in the central
account, which feeds a Firehose DirectPut stream. Firehose natively decompresses each CloudWatch
Logs envelope, extracts its message records, converts the JSON to Snappy Parquet using a Glue table
schema, and writes to an encrypted S3 data lake partitioned by date. The subscription filter only
forwards records whose documented `schemaType` is `ModelInvocationLog`, which removes setup notices
before they reach Parquet conversion.

---

## Repository layout

| Path | Purpose |
|---|---|
| `bedrock-logging.yaml` | **Source account stack.** Log group, KMS key, Bedrock logging role, subscription filter. |
| `bedrock-firehose-data-lake.yaml` | **Central account stack.** Buckets, KMS, Firehose, Glue catalog, Athena workgroup, alarms, QuickSight role. |
| `Bedrock Data Lake SQL.md` | Athena query reference, including the CUR join patterns. |
| `Bedrock-Invocation-Usage-Dashboard.yaml` | CID-format QuickSight dashboard definition. |
| `deploy.md` | Step-by-step deployment commands. |
| `s3-security.guard` | cfn-guard rules enforcing bucket and IAM posture. |
| `tests/test_cfn_properties.py` | Template security and configuration assertions (pytest + Hypothesis). |
| `tests/test_firehose_processing.py` | Tests for native CloudWatch Logs decompression, message extraction, filtering, and schema compatibility. |
| `integration-tests/README.md` | End-to-end validation procedure. |
| `.github/workflows/main.yml` | CI: cfn-lint, cfn_nag, cfn-guard, pytest. |

---

## The source account stack — `bedrock-logging.yaml`

Small and self-contained. Takes one parameter, `CentralCWLDestinationArn`.

- **`BedrockLogsKmsKey`** — CMK encrypting the log group, scoped by an
  `kms:EncryptionContext:aws:logs:arn` condition so it can only be used for this log group.
- **`BedrockLogsGroup`** — `/aws/bedrock/modelinvocations`, 7-day retention. Retention is short on
  purpose: CloudWatch is a transit hop, and S3 is the durable store.
- **`BedrockLoggingRole`** — assumed by `bedrock.amazonaws.com` with an `aws:SourceAccount`
  condition. This is the role you supply when enabling Bedrock model invocation logging.
- **`CWLSubscriptionFilterRole`** — required specifically because the central destination policy is
  org-scoped rather than account-scoped. CloudWatch Logs assumes it to validate the subscription.
- **`BedrockCWLSubscriptionFilter`** — forwards only JSON records with
  `schemaType=ModelInvocationLog`, excluding setup notices before Parquet conversion.

---

## The central account stack — `bedrock-firehose-data-lake.yaml`

Takes one parameter, `OrganizationId` (validated against `^o-[a-z0-9]{10,32}$`).

**Storage.** Two CMKs and two buckets:

- `bedrock-firehose-lake-<account>` — the data lake. Lifecycle transitions the `data/` prefix to
  STANDARD_IA at 90 days and Glacier at 365.
- `bedrock-athena-results-<account>` — query results, expiring at 30 days.

Both deny non-HTTPS via bucket policy, block all public access, and retain on stack deletion.
Versioning is deliberately off on the data lake — Firehose only ever writes new objects.

**Catalog.** Glue database `bedrock_logs`, table `bedrock_invocations`, with **Athena partition
projection** on `year`/`month`/`day` only. The Glue table supplies the schema for Firehose's Parquet
conversion and Athena queries; records do not pass through a Glue job or crawler. `accountid` and
`region` are regular Parquet columns rather than partition keys. That's a deliberate tradeoff: far
fewer partitions to manage, at the cost of not being able to partition-prune by account.

**Ingestion.** Firehose stream `bedrock-invocations-v2`, DirectPut, 128 MB / 60s buffering. Its
native `Decompression` and `CloudWatchLogProcessing` processors unpack the gzip CloudWatch Logs
envelope and emit each message. `DataFormatConversionConfiguration` reads the Glue schema to drive
the Parquet conversion. Dynamic partitioning is off — the date prefix comes from Firehose timestamp
expressions instead, which is cheaper and sufficient given date-only partitioning.

**Cross-account entry point.** `CWLFirehoseDestination` carries a `Principal: "*"` policy gated by
`aws:PrincipalOrgID`. This is what lets any account in the org subscribe without the central
template enumerating account IDs.

**Access.** Three separate roles, each scoped to one job: `bedrock-firehose-role` (S3 write, Glue
schema read, KMS, error logging), `bedrock-cwl-destination-role` (Firehose PutRecord only), and
`BedrockQuickSightDataSourceRole` (Athena, Glue, S3 read, KMS decrypt).

**Observability.** Alarms on `DeliveryToS3.DataFreshness` (>5 min) and `DeliveryToS3.Success` (<95%
for two periods), both publishing to the `bedrock-firehose-alarms` SNS topic.

---

## Native Firehose preprocessing

The stream uses Firehose's native
[`Decompression`](https://docs.aws.amazon.com/firehose/latest/dev/writing-with-cloudwatch-logs-decompression.html)
processor for CloudWatch Logs gzip envelopes, followed by `CloudWatchLogProcessing` with
[`DataMessageExtraction=true`](https://docs.aws.amazon.com/firehose/latest/dev/Message_extraction.html).
This emits the content of each `logEvents[].message` record before JSON-to-Parquet conversion.

The source subscription filter matches `{ $.schemaType = "ModelInvocationLog" }`. Amazon Bedrock
[documents this value for every invocation record](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html),
so the filter excludes plain-text setup notices that would otherwise fail Parquet parsing. The
Glue schema stores the native nested `identity.arn` field and retains the former `identity_arn`
column for existing Parquet objects. The Athena view
uses `COALESCE(identity_arn, identity.arn)`, keeping historical and new records query-compatible.

This removes Lambda concurrency and retry limits from ingestion. SQS is neither required nor a
supported direct CloudWatch Logs subscription destination. If sustained traffic approaches the
Firehose quota, increase that quota; if the design later needs explicit stream capacity or more
than Firehose's 24-hour DirectPut retention, evaluate Kinesis Data Streams rather than SQS.

---

## Querying: the flattened view

`bedrock_invocations_view` is created by a saved Athena query in the `bedrock-analytics` workgroup.
It flattens the nested structs and pulls token counts out of the stringified response body.

**Two implementation details worth knowing before you edit it:**

1. **JSON paths are lowercase on purpose.** Firehose's OpenX JSON SerDe is case-insensitive and
   lowercases every key, so the stringified bodies contain `inputtokens` and `stopreason`, not
   `inputTokens` and `stopReason`. CamelCase paths return `NULL` silently — no error, just missing
   data.
2. **Every numeric extraction uses `TRY_CAST`.** Model response shapes differ; a path that doesn't
   exist yields `NULL` rather than failing the whole query.

The view also derives a readable `identity` column by stripping the STS session suffix from
assumed-role ARNs, and filters to the trailing 6 months.

## Querying: the CUR join

The join that produces cost attribution matches on four things:

| Bedrock field | CUR field | Match |
|---|---|---|
| `account_id` | `line_item_usage_account_id` | Exact |
| `modelid` | `line_item_resource_id` | Exact (both inference profile ARNs) |
| `regexp_replace(identity_arn, '/[^/]+$', '')` | `regexp_replace(line_item_iam_principal, '/[^/]+$', '')` | Normalized role ARN |
| `date_trunc('hour', timestamp)` | `line_item_usage_start_date` → `end_date` | Range |

The ARN normalization is the non-obvious part. Bedrock logs the STS session ARN
(`arn:aws:sts::…:assumed-role/my-role/session-name`) while CUR logs the IAM role ARN
(`arn:aws:iam::…:role/my-role`). Stripping the last path segment from both sides aligns them at the
role level.

**CUR prerequisite:** `line_item_iam_principal` requires CUR v2 with that column enabled under
**Cost & Usage Report → Report content**. Activated cost allocation tags surface as
`resource_tags_user_<tag_key>` columns, with hyphens converted to underscores.

See `Bedrock Data Lake SQL.md` for the full query set.

---

## Testing

`tests/test_cfn_properties.py` parses both templates with a custom YAML loader that tolerates
CloudFormation intrinsic tags, then asserts:

- No role is assumable by a wildcard principal without an org scope.
- The CWL destination policy is organization-scoped.
- Partition projection is configured consistently with the S3 prefix (Hypothesis-generated dates).
- Bucket names follow the `<purpose>-<account-id>` convention (Hypothesis-generated account IDs).
- All buckets carry `DeletionPolicy: Retain`.
- No Glue crawler, job, or trigger exists — the design deliberately avoids them.
- Required stack outputs are present.
- Native Firehose `Decompression` and `CloudWatchLogProcessing` processors are configured in order.
- The source subscription forwards only `ModelInvocationLog` records.
- The Glue schema and Athena view remain compatible with both the former flattened identity field
  and the native nested identity object.

`s3-security.guard` encodes the same posture as cfn-guard rules for CI enforcement.

---

## Known documentation drift

The Firehose architecture replaced an earlier S3 cross-account replication design, and some
artifacts still reference the old one:

1. **`bedrock-data-lake.yaml` is referenced throughout but does not exist in the repo.** The only
   root-level templates are `bedrock-firehose-data-lake.yaml`, `bedrock-logging.yaml`, and the
   dashboard. Yet `.github/workflows/main.yml` targets the missing file in two steps (`CFN Nag scan`
   and `CFN Lint`), so **CI is broken on every pull request** — those steps fail before the Firehose
   template is ever scanned. `deploy.md`, `integration-tests/README.md`, and
   `.kiro/specs/bedrock-logging-data-lake/aws-deployment.md` all treat it as the primary template,
   and README's "Legacy S3 replication architecture" section says it is "preserved for reference" —
   which is no longer true.
2. **README deployment parameters don't match the template.** README states "the template declares
   `SourceAccountIds` as a required parameter" and passes
   `ParameterKey=SourceAccountIds,ParameterValue=$SOURCE_ACCOUNT_ID`. The template declares no such
   parameter — only a required `OrganizationId`. As written, the README's deploy command fails with
   a parameter validation error. README also carries a prerequisites note claiming
   `SourceAccountIds` "is declared for deployment compatibility but is not currently interpolated,"
   which describes a state of the template that no longer exists.
3. **`scripts/test_bedrock_invocation.py` reads a stack output that no longer exists.** It looks up
   `BedrockInferenceProfileArn` from the `bedrock-logging` stack and raises `ValueError` if absent;
   the current template exports only `BedrockLogsGroupName` and `BedrockLoggingRoleArn`. Pass
   `--model-id` explicitly to use it. Same output is referenced in `aws-deployment.md` Step 6.
4. **`scripts/bedrock_logs_etl.py` is an orphan.** It's a Glue Spark job from the replication-based
   design, superseded by Firehose's inline Parquet conversion — and its presence sits oddly beside
   the `test_no_glue_crawler_or_etl` assertion. CI still runs `bandit` against it. Same orphan
   status for `scripts/test_lambda_handler.py`.
5. **The test suite can't fail the build.** `main.yml` runs `pytest tests/ --tb=short -v || true`,
   so every security assertion in `tests/` is advisory only. Dropping the `|| true` would make the
   suite enforcing — worth doing once the missing-template steps above are fixed.

Also note `.kiro/specs/bedrock-logging-data-lake/aws-deployment.md` carries `inclusion: always`
frontmatter, so it loads into every agent session in this workspace. It contains real account IDs and
named AWS CLI profiles, and it instructs an `s3Config` block on the Bedrock logging call that both
README Step 3 and `.kiro/steering/redeployment.md` explicitly say to omit under the Firehose
architecture. `deploy.md` carries the same stale `s3Config` block.
