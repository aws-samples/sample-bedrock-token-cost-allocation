# Bedrock Data Lake — Athena Query Reference

All queries run in the `bedrock-analytics` workgroup against account `260990198475`.

---

## Quick preview

```sql
SELECT * FROM bedrock_logs.bedrock_invocations_processed LIMIT 10;
```

---

## Cost attribution by IAM principal — joined on identity ARN and CUR tags

Joins Bedrock invocation logs to CUR on `identity_arn` matched against `line_item_iam_principal`. Add whichever `resource_tags_user_*` columns you have activated in your CUR to the SELECT — they come straight from the CUR table with no transformation.

> **CUR prerequisite:** `line_item_iam_principal` is available in CUR v2. Enable it under **Cost & Usage Report → Report content**. Activated cost allocation tags appear as `resource_tags_user_<tag_key>` columns (hyphens in tag keys become underscores).

```sql
SELECT
    b.account_id,
    b.identity_arn,
    regexp_replace(b.identity_arn, '/[^/]+$', '') AS identity_role_arn,
    c.line_item_iam_principal,
    tags,
    -- Add your activated CUR tag columns here, e.g.:
    --   c.resource_tags_user_team,
    --   c.resource_tags_user_project,
    --   c.resource_tags_user_cost_centre
    b.modelid,
    DATE_FORMAT(from_iso8601_timestamp(b.timestamp), '%Y-%m-%d') AS log_date,
    COUNT(b.requestid)               AS invocation_count,
    SUM(b.input.inputtokencount)     AS total_input_tokens,
    SUM(b.output.outputtokencount)   AS total_output_tokens,
    SUM(c.line_item_unblended_cost)  AS total_unblended_cost
FROM bedrock_logs.bedrock_invocations_processed b
JOIN cid_data_export.cur2 c
    ON  b.account_id = c.line_item_usage_account_id
    AND b.modelid    = c.line_item_resource_id
    -- Strip STS session suffix so assumed-role/my-role/session matches role/my-role in CUR
    AND regexp_replace(b.identity_arn, '/[^/]+$', '') = regexp_replace(c.line_item_iam_principal, '/[^/]+$', '')
    AND date_trunc('hour', from_iso8601_timestamp(b.timestamp)) >= c.line_item_usage_start_date
    AND date_trunc('hour', from_iso8601_timestamp(b.timestamp))  < c.line_item_usage_end_date
WHERE b.modelid LIKE '%infer%'
  AND c.line_item_resource_id LIKE '%infer%'
  AND c.line_item_iam_principal IS NOT NULL
GROUP BY
    b.account_id,
    b.identity_arn,
    regexp_replace(b.identity_arn, '/[^/]+$', ''),
    c.line_item_iam_principal,
    -- Add your tag columns to GROUP BY to match the SELECT above
    b.modelid,
    tags,
    DATE_FORMAT(from_iso8601_timestamp(b.timestamp), '%Y-%m-%d')
ORDER BY total_unblended_cost DESC;
```

### Join logic

| Bedrock field | CUR field | Notes |
|---|---|---|
| `account_id` | `line_item_usage_account_id` | Exact match |
| `modelid` | `line_item_resource_id` | Both are inference profile ARNs |
| `regexp_replace(identity_arn, '/[^/]+$', '')` | `regexp_replace(line_item_iam_principal, '/[^/]+$', '')` | Strip STS session suffix so `assumed-role/my-role/session` matches `role/my-role` |
| `date_trunc('hour', timestamp)` | `line_item_usage_start_date` → `line_item_usage_end_date` | Bedrock log hour falls within CUR hourly bucket |

### Why the ARN normalisation is needed

Bedrock logs record the full STS session ARN:
```
arn:aws:sts::123456789012:assumed-role/my-role/session-name
```
CUR records the IAM role ARN:
```
arn:aws:iam::123456789012:role/my-role
```
`regexp_replace(..., '/[^/]+$', '')` strips the last path segment from both sides before comparing, aligning them at the role level.

---

## Invocations by account and model

```sql
SELECT account_id, modelid, COUNT(*) AS invocations
FROM bedrock_logs.bedrock_invocations_processed
GROUP BY account_id, modelid
ORDER BY invocations DESC;
```

---

## CUR cost joined to Bedrock invocation logs

Joins Bedrock invocation logs to CUR line items on inference profile ARN and hourly time window.
Returns per-account, per-model, per-day cost with invocation count and token totals.

```sql
WITH bedrock AS (
    SELECT
        account_id,
        modelid,
        timestamp,
        requestid,
        operation,
        date_trunc('hour', from_iso8601_timestamp(timestamp)) AS log_hour,
        input.inputtokencount  AS input_tokens,
        output.outputtokencount AS output_tokens
    FROM "bedrock_logs"."bedrock_invocations_processed"
    WHERE modelid LIKE '%infer%'
),
cur_bedrock AS (
    SELECT
        line_item_usage_account_id,
        line_item_resource_id,
        line_item_usage_start_date,
        line_item_usage_end_date,
        SUM(line_item_unblended_cost) AS total_cost,
        SUM(line_item_usage_amount)   AS total_usage
    FROM "cid_data_export"."cur2"
    WHERE line_item_resource_id LIKE '%infer%'
    GROUP BY
        line_item_usage_account_id,
        line_item_resource_id,
        line_item_usage_start_date,
        line_item_usage_end_date
)
SELECT
    b.account_id,
    b.modelid,
    c.line_item_resource_id,
    DATE_FORMAT(from_iso8601_timestamp(b.timestamp), '%Y-%m-%d') AS log_date,
    COUNT(b.requestid)      AS invocation_count,
    SUM(b.input_tokens)     AS total_input_tokens,
    SUM(b.output_tokens)    AS total_output_tokens,
    SUM(c.total_cost)       AS total_unblended_cost
FROM bedrock b
JOIN cur_bedrock c
    ON  b.account_id = c.line_item_usage_account_id
    AND b.modelid    = c.line_item_resource_id
    AND b.log_hour  >= c.line_item_usage_start_date
    AND b.log_hour   < c.line_item_usage_end_date
GROUP BY
    b.account_id,
    b.modelid,
    c.line_item_resource_id,
    DATE_FORMAT(from_iso8601_timestamp(b.timestamp), '%Y-%m-%d')
ORDER BY total_unblended_cost DESC;
```

### Join logic

| Bedrock field | CUR field | Match type |
|---|---|---|
| `account_id` | `line_item_usage_account_id` | Exact |
| `modelid` | `line_item_resource_id` | Exact (both are inference profile ARNs) |
| `date_trunc('hour', timestamp)` | `line_item_usage_start_date` to `line_item_usage_end_date` | Range (Bedrock log hour falls within CUR hourly bucket) |

---

## Invocations with prompt and response text joined to CUR cost

Extracts the user prompt and assistant response from the invocation logs and joins to CUR cost data.

```sql
WITH bedrock AS (
    SELECT
        account_id,
        modelid,
        timestamp,
        requestid,
        date_trunc('hour', from_iso8601_timestamp(timestamp)) AS log_hour,
        input.inputtokencount   AS input_tokens,
        output.outputtokencount AS output_tokens,
        -- Extract user prompt (Nova messages array / Claude Anthropic direct string / Titan / older Claude)
        COALESCE(
            json_extract_scalar(input.inputbodyjson,  '$.messages[0].content[0].text'),
            json_extract_scalar(input.inputbodyjson,  '$.messages[0].content'),
            json_extract_scalar(input.inputbodyjson,  '$.prompt'),
            json_extract_scalar(input.inputbodyjson,  '$.inputText')
        ) AS user_prompt,
        -- Extract assistant response (Nova / Claude Anthropic / Titan / older Claude)
        COALESCE(
            json_extract_scalar(output.outputbodyjson, '$.output.message.content[0].text'),
            json_extract_scalar(output.outputbodyjson, '$.content[0].text'),
            json_extract_scalar(output.outputbodyjson, '$.completion'),
            json_extract_scalar(output.outputbodyjson, '$.outputText')
        ) AS assistant_response
    FROM "bedrock_logs"."bedrock_invocations_processed"
    WHERE modelid LIKE '%infer%'
),
cur_bedrock AS (
    SELECT
        line_item_usage_account_id,
        line_item_resource_id,
        line_item_usage_start_date,
        line_item_usage_end_date,
        SUM(line_item_unblended_cost) AS total_cost
    FROM "cid_data_export"."cur2"
    WHERE line_item_resource_id LIKE '%infer%'
    GROUP BY
        line_item_usage_account_id,
        line_item_resource_id,
        line_item_usage_start_date,
        line_item_usage_end_date
)
SELECT
    b.account_id,
    b.modelid,
    b.timestamp,
    b.requestid,
    b.input_tokens,
    b.output_tokens,
    c.total_cost                          AS estimated_cost,
    b.user_prompt,
    substr(b.assistant_response, 1, 500)  AS assistant_response_preview
FROM bedrock b
JOIN cur_bedrock c
    ON  b.account_id = c.line_item_usage_account_id
    AND b.modelid    = c.line_item_resource_id
    AND b.log_hour  >= c.line_item_usage_start_date
    AND b.log_hour   < c.line_item_usage_end_date
ORDER BY b.timestamp DESC;
```

```sql
SELECT
    DATE_FORMAT(from_iso8601_timestamp(b.timestamp), '%Y-%m-%d') AS log_date,
    b.account_id,
    COUNT(b.requestid)  AS invocations,
    SUM(c.line_item_unblended_cost) AS daily_cost
FROM "bedrock_logs"."bedrock_invocations_processed" b
JOIN "cid_data_export"."cur2" c
    ON  b.account_id = c.line_item_usage_account_id
    AND b.modelid    = c.line_item_resource_id
    AND date_trunc('hour', from_iso8601_timestamp(b.timestamp)) >= c.line_item_usage_start_date
    AND date_trunc('hour', from_iso8601_timestamp(b.timestamp))  < c.line_item_usage_end_date
WHERE b.modelid LIKE '%infer%'
AND   c.line_item_resource_id LIKE '%infer%'
GROUP BY
    DATE_FORMAT(from_iso8601_timestamp(b.timestamp), '%Y-%m-%d'),
    b.account_id
ORDER BY log_date DESC, daily_cost DESC;
```
