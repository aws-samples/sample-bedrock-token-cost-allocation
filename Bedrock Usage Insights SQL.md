# Bedrock Usage Insights — Athena Query Reference

These queries are optional views over the feature-owned Usage Insights data set. Run every query in the `bedrock-analytics` Athena workgroup, against the `bedrock_logs` database. Replace the example `year`, `month`, and `day` values with a finite UTC date range before execution. The partition predicates are required for scan control and must remain in place when the range is changed.

The examples use these feature-owned tables:

- `bedrock_logs.bedrock_usage_insights_aggregates` contains aggregate-only rows. Its stable fields include `group_type`, `group_key`, `period_start`, `period_end`, `analyzed_record_count`, `classified_record_count`, `unknown_result_count`, `not_assessed_count`, `unknown_result_proportion`, `input_token_total`, `output_token_total`, `category_distribution`, `confidence_coverage`, `prompt_quality_coverage`, `minimum_group_size`, `suppressed`, `analysis_run_id`, `configuration_version`, `analysis_logic_version`, and `publication_status`.
- `bedrock_logs.bedrock_usage_insights_results` contains traceability fields for feature-owned result rows, including `source_request_id`, `source_timestamp_utc`, `source_account`, `source_region`, `model_id`, `analysis_run_id`, `configuration_version`, `year`, `month`, `day`, and `publication_status`. Queries in this document use those traceability fields only inside a reconciliation CTE and do not return individual identifiers.
- `bedrock_logs.bedrock_usage_insights_runs` contains run metadata, including `analysis_run_id` and `status`. The committed publication marker is stored on the feature-owned result and aggregate rows.

A query must include both of these filters before aggregating data:

```sql
run.status = 'completed'
AND aggregate_or_result.publication_status = 'committed'
```

This excludes staged, failed, partial, cancelled, or otherwise uncommitted runs. If the deployed Glue schema exposes the committed marker on the run view rather than the base table, use the feature-owned committed-run view with the same semantics.

## Invocation counts

Returns aggregate invocation coverage by reporting group and day. It exposes counts only, not request IDs, identities, role-session names, or other individual subjects.

```sql
-- Workgroup: bedrock-analytics
-- Database: bedrock_logs
-- Example UTC range: 2026-08-01 through 2026-08-31
SELECT
    a.period_start,
    a.period_end,
    a.group_type,
    a.group_key,
    SUM(a.analyzed_record_count)   AS invocation_count,
    SUM(a.classified_record_count) AS classified_invocation_count,
    SUM(a.not_assessed_count)      AS not_assessed_count
FROM bedrock_logs.bedrock_usage_insights_aggregates a
JOIN bedrock_logs.bedrock_usage_insights_runs run
    ON run.analysis_run_id = a.analysis_run_id
WHERE a.year = '2026'
  AND a.month = '08'
  AND a.day BETWEEN '01' AND '31'
  AND a.period_start >= DATE '2026-08-01'
  AND a.period_end   <  DATE '2026-09-01'
  AND run.status = 'completed'
  AND a.publication_status = 'committed'
  AND a.suppressed = false
GROUP BY
    a.period_start,
    a.period_end,
    a.group_type,
    a.group_key
ORDER BY a.period_start, a.group_type, a.group_key;
```

## Token totals

Returns aggregate input and output token totals by model and reporting period. Cache token columns, when available in a deployed aggregate schema, can be added to the select list without adding individual fields.

```sql
-- Workgroup: bedrock-analytics
-- Database: bedrock_logs
-- Example UTC range: 2026-08-01 through 2026-08-31
SELECT
    a.period_start,
    a.period_end,
    a.group_key AS model_group,
    SUM(a.analyzed_record_count) AS invocation_count,
    SUM(a.input_token_total)     AS input_token_total,
    SUM(a.output_token_total)    AS output_token_total,
    SUM(a.input_token_total + a.output_token_total) AS total_token_count
FROM bedrock_logs.bedrock_usage_insights_aggregates a
JOIN bedrock_logs.bedrock_usage_insights_runs run
    ON run.analysis_run_id = a.analysis_run_id
WHERE a.year = '2026'
  AND a.month = '08'
  AND a.day BETWEEN '01' AND '31'
  AND a.period_start >= DATE '2026-08-01'
  AND a.period_end   <  DATE '2026-09-01'
  AND a.group_type = 'model'
  AND run.status = 'completed'
  AND a.publication_status = 'committed'
  AND a.suppressed = false
GROUP BY a.period_start, a.period_end, a.group_key
ORDER BY a.period_start, total_token_count DESC;
```

## Prompt-quality coverage

`prompt_quality_coverage` is a map whose keys are enabled dimensions such as `clarity`, `specificity`, `context_completeness`, `task_definition`, and `output_constraints`. Each value is the number of records with a numeric assessment for that dimension. `not_assessed` records remain in the denominator so missing, redacted, expired, malformed, or excluded content is visible as coverage loss.

```sql
-- Workgroup: bedrock-analytics
-- Database: bedrock_logs
-- Example UTC range: 2026-08-01 through 2026-08-31
SELECT
    a.period_start,
    a.period_end,
    a.group_type,
    a.group_key,
    quality.dimension,
    SUM(quality.assessed_count) AS assessed_count,
    SUM(a.analyzed_record_count) AS analyzed_record_count,
    SUM(a.not_assessed_count) AS not_assessed_count,
    CAST(SUM(quality.assessed_count) AS DOUBLE)
        / NULLIF(SUM(a.analyzed_record_count), 0) AS assessed_coverage
FROM bedrock_logs.bedrock_usage_insights_aggregates a
JOIN bedrock_logs.bedrock_usage_insights_runs run
    ON run.analysis_run_id = a.analysis_run_id
CROSS JOIN UNNEST(map_entries(a.prompt_quality_coverage)) AS quality (dimension, assessed_count)
WHERE a.year = '2026'
  AND a.month = '08'
  AND a.day BETWEEN '01' AND '31'
  AND a.period_start >= DATE '2026-08-01'
  AND a.period_end   <  DATE '2026-09-01'
  AND run.status = 'completed'
  AND a.publication_status = 'committed'
  AND a.suppressed = false
GROUP BY
    a.period_start,
    a.period_end,
    a.group_type,
    a.group_key,
    quality.dimension
ORDER BY a.period_start, a.group_type, a.group_key, quality.dimension;
```

A coverage value describes whether the permitted prompt preview could be assessed. It is not a quality judgment about a person, team, business unit, or prompt author.

## Unknown-result rates

Returns the aggregate unknown-result rate and its component counts. A zero-record period returns `NULL` for the rate rather than an invented zero. `unknown_result_reason` detail can be added as another aggregate map when the deployed aggregate schema provides it; do not expose individual result rows for this report.

```sql
-- Workgroup: bedrock-analytics
-- Database: bedrock_logs
-- Example UTC range: 2026-08-01 through 2026-08-31
SELECT
    a.period_start,
    a.period_end,
    a.group_type,
    a.group_key,
    SUM(a.analyzed_record_count) AS analyzed_record_count,
    SUM(a.unknown_result_count)  AS unknown_result_count,
    SUM(a.not_assessed_count)    AS not_assessed_count,
    CAST(SUM(a.unknown_result_count) AS DOUBLE)
        / NULLIF(SUM(a.analyzed_record_count), 0) AS unknown_result_rate,
    CAST(SUM(a.unknown_result_count) AS DOUBLE)
        / NULLIF(SUM(a.analyzed_record_count) + SUM(a.not_assessed_count), 0)
        AS unknown_and_not_assessed_rate
FROM bedrock_logs.bedrock_usage_insights_aggregates a
JOIN bedrock_logs.bedrock_usage_insights_runs run
    ON run.analysis_run_id = a.analysis_run_id
WHERE a.year = '2026'
  AND a.month = '08'
  AND a.day BETWEEN '01' AND '31'
  AND a.period_start >= DATE '2026-08-01'
  AND a.period_end   <  DATE '2026-09-01'
  AND run.status = 'completed'
  AND a.publication_status = 'committed'
  AND a.suppressed = false
GROUP BY a.period_start, a.period_end, a.group_type, a.group_key
ORDER BY a.period_start, unknown_result_rate DESC;
```

## Estimated or allocated CUR cost

This query reconciles feature-owned result rows to the existing `bedrock_logs.bedrock_invocations_view`, then allocates the matching CUR hourly amount across the reconciled invocation count. The final output is aggregate-only. Request IDs appear only in the internal reconciliation CTE and are never returned.

The reconciliation join uses all of the following keys:

| Insights result | `bedrock_invocations_view` | Purpose |
|---|---|---|
| `source_request_id` | `requestid` | Match the source invocation |
| `source_account` | `accountid` | Prevent cross-account matches |
| `model_id` | `modelid` | Prevent cross-model matches |
| `source_timestamp_utc` | `timestamp` | Match the UTC source event; CUR is then joined on the containing UTC hour |

The CUR join uses `line_item_usage_account_id`, `line_item_resource_id`, and the hourly range from `line_item_usage_start_date` through `line_item_usage_end_date`. The resource ID predicate may need to be adjusted if the account's CUR export stores a different Bedrock resource identifier.

Values returned by this query are **estimated or allocated CUR measures, not the authoritative billing ledger**. An invocation-log reconciliation can omit, duplicate, or differently classify usage, and an allocated amount is an analytical attribution of a CUR line item. Reconcile results with the official AWS bill before using them for billing or financial settlement.

```sql
-- Workgroup: bedrock-analytics
-- Database: bedrock_logs; CUR database: cid_data_export
-- Example UTC range: 2026-08-01 through 2026-08-31
WITH committed_runs AS (
    SELECT analysis_run_id
    FROM bedrock_logs.bedrock_usage_insights_runs
    WHERE status = 'completed'
),
reconciled_results AS (
    SELECT DISTINCT
        r.analysis_run_id,
        r.source_request_id,
        r.source_account,
        r.source_region,
        r.model_id,
        CAST(r.source_timestamp_utc AS timestamp) AS source_timestamp_utc
    FROM bedrock_logs.bedrock_usage_insights_results r
    JOIN committed_runs run
        ON run.analysis_run_id = r.analysis_run_id
    JOIN bedrock_logs.bedrock_invocations_view v
        ON  r.source_request_id = v.requestid
        AND r.source_account    = v.accountid
        AND r.model_id          = v.modelid
        AND CAST(r.source_timestamp_utc AS timestamp)
            = CAST(from_iso8601_timestamp(v.timestamp) AS timestamp)
    WHERE r.year = '2026'
      AND r.month = '08'
      AND r.day BETWEEN '01' AND '31'
      AND v.year = '2026'
      AND v.month = '08'
      AND v.day BETWEEN '01' AND '31'
      AND r.publication_status = 'committed'
),
usage_buckets AS (
    SELECT
        source_account,
        source_region,
        model_id,
        date_trunc('hour', source_timestamp_utc) AS usage_hour,
        COUNT(*) AS invocation_count
    FROM reconciled_results
    GROUP BY
        source_account,
        source_region,
        model_id,
        date_trunc('hour', source_timestamp_utc)
),
bucket_totals AS (
    SELECT
        source_account,
        model_id,
        usage_hour,
        SUM(invocation_count) AS bucket_invocation_count
    FROM usage_buckets
    GROUP BY source_account, model_id, usage_hour
),
cur_buckets AS (
    SELECT
        line_item_usage_account_id AS source_account,
        line_item_resource_id     AS model_id,
        line_item_usage_start_date,
        line_item_usage_end_date,
        SUM(line_item_unblended_cost) AS cur_unblended_cost
    FROM cid_data_export.cur2
    WHERE line_item_resource_id LIKE '%infer%'
      AND line_item_usage_start_date < TIMESTAMP '2026-09-01 00:00:00 UTC'
      AND line_item_usage_end_date   > TIMESTAMP '2026-08-01 00:00:00 UTC'
    GROUP BY
        line_item_usage_account_id,
        line_item_resource_id,
        line_item_usage_start_date,
        line_item_usage_end_date
)
SELECT
    u.source_account,
    u.source_region,
    u.model_id,
    CAST(u.usage_hour AS date) AS usage_date,
    SUM(u.invocation_count) AS reconciled_invocation_count,
    SUM(
        c.cur_unblended_cost
        * u.invocation_count
        / NULLIF(t.bucket_invocation_count, 0)
    ) AS allocated_cur_cost,
    'allocated' AS cost_measure_label
FROM usage_buckets u
JOIN bucket_totals t
    ON  u.source_account = t.source_account
    AND u.model_id       = t.model_id
    AND u.usage_hour     = t.usage_hour
JOIN cur_buckets c
    ON  u.source_account = c.source_account
    AND u.model_id      = c.model_id
    AND u.usage_hour   >= c.line_item_usage_start_date
    AND u.usage_hour    < c.line_item_usage_end_date
GROUP BY
    u.source_account,
    u.source_region,
    u.model_id,
    CAST(u.usage_hour AS date)
ORDER BY usage_date DESC, allocated_cur_cost DESC;
```

The allocation is across source Regions for each account/model/hour, using reconciled invocation counts as the allocation basis. It remains an analytical allocation, not billed cost.

## Optional feature and existing-query independence

Usage Insights is additive. These queries require the feature-owned tables and committed runs, but they do not replace `bedrock_logs.bedrock_invocations`, `bedrock_logs.bedrock_invocations_view`, the existing CUR queries, or the existing Bedrock Invocation Usage Dashboard data set. If the optional feature is disabled, unavailable, or undeployed, continue using [Bedrock Data Lake SQL](./Bedrock%20Data%20Lake%20SQL.md) and the existing dashboard without an insights-specific dependency. No query in this document changes a source table, source view, Firehose resource, CUR table, or existing dashboard definition.
