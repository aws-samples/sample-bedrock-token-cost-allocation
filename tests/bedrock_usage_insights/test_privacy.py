"""Example-based tests for privacy authorization and redaction boundaries."""

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from bedrock_usage_insights.models import (
    AnalysisScope,
    ContentAnalysisPolicy,
    ContentAnalysisState,
    InsightConfiguration,
    InvocationRecord,
    PrivacyPolicy,
    RedactionStatus,
    SensitiveContent,
)
from bedrock_usage_insights.privacy import (
    ContentAccessState,
    DefaultPrivacyGate,
    PrivacyBoundary,
    RedactionFailure,
    RedactionResult,
    not_assessed_prompt_quality,
)

UTC = timezone.utc
NOW = datetime(2026, 2, 3, 4, 5, tzinfo=UTC)
ACCOUNT = "123456789012"
REGION = "us-east-1"
POLICY = "privacy-policy-v1"


def make_configuration(**changes) -> InsightConfiguration:
    values = {
        "configuration_version": "config-v1",
        "effective_at": datetime(2026, 1, 1, tzinfo=UTC),
        "author_or_service": "privacy-tests",
        "change_reason": "privacy test",
        "analysis_scope": AnalysisScope(
            accounts=(ACCOUNT,),
            regions=(REGION,),
            models=("amazon.nova-pro-v1:0",),
            operations=("InvokeModel",),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        ),
        "enabled": False,
    }
    values.update(changes)
    return InsightConfiguration(**values)


def enabled_configuration(**changes) -> InsightConfiguration:
    values = {
        "enabled": True,
        "content_analysis": ContentAnalysisPolicy(
            state=ContentAnalysisState.ENABLED,
            approved_accounts=(ACCOUNT,),
            approved_regions=(REGION,),
            access_policy_reference=POLICY,
            retention_days=30,
            max_preview_length=100,
        ),
    }
    values.update(changes)
    return make_configuration(**values)


def make_record(**changes) -> InvocationRecord:
    values = {
        "source_request_id": "request-1",
        "source_timestamp_utc": NOW,
        "source_account": ACCOUNT,
        "source_region": REGION,
        "model_id": "amazon.nova-pro-v1:0",
        "operation": "InvokeModel",
        "analysis_run_id": "run-1",
        "configuration_version": "config-v1",
        "analysis_logic_version": "1.0",
        "input_token_count": 12,
        "output_token_count": 20,
        "request_metadata": {"team": "platform"},
        "sensitive_content": SensitiveContent(
            prompt_preview="private secret prompt",
            response_preview="private response",
        ),
    }
    values.update(changes)
    return InvocationRecord(**values)


def gate(**changes) -> DefaultPrivacyGate:
    return DefaultPrivacyGate(
        boundary=PrivacyBoundary(
            now=NOW,
            approved_access_policies=frozenset({POLICY}),
        ),
        **changes,
    )


def test_disabled_feature_strips_content_and_blocks_content_analysis():
    decision = gate().authorize(make_record(), make_configuration())

    assert not decision.allowed
    assert decision.content is None
    assert decision.content_state is ContentAccessState.DISABLED
    assert decision.reason == "feature_disabled"
    assert decision.record is not None
    assert decision.record.sensitive_content is None
    assert make_record().sensitive_content is not None


def test_disabled_content_analysis_strips_content_and_preserves_non_content_record():
    configuration = make_configuration(enabled=True)

    decision = gate().authorize(make_record(), configuration)

    assert not decision.allowed
    assert decision.content_state is ContentAccessState.DISABLED
    assert decision.reason == "content_disabled"
    assert decision.non_content_record is not None
    assert decision.non_content_record.model_id == "amazon.nova-pro-v1:0"
    assert decision.non_content_record.input_token_count == 12


def test_approved_content_is_redacted_before_authorized_access():
    decision = gate().authorize(make_record(), enabled_configuration())

    assert decision.allowed
    assert decision.content_state is ContentAccessState.AVAILABLE
    assert decision.content is not None
    assert decision.content.redaction_status is RedactionStatus.REDACTED
    assert decision.content.prompt_preview == "private secret prompt"
    assert decision.content.response_preview == "private response"
    assert decision.record is not None
    assert decision.record.sensitive_content is None


def test_successful_rule_redaction_replaces_sensitive_values_and_truncates_output():
    configuration = enabled_configuration(
        privacy=PrivacyPolicy(exclusion_rules=("secret",)),
        content_analysis=ContentAnalysisPolicy(
            state=ContentAnalysisState.ENABLED,
            approved_accounts=(ACCOUNT,),
            approved_regions=(REGION,),
            access_policy_reference=POLICY,
            retention_days=30,
            max_preview_length=18,
        ),
    )

    decision = gate().authorize(
        make_record(
            sensitive_content=SensitiveContent(
                prompt_preview="private secret prompt with trailing data",
                response_preview="private response",
            )
        ),
        configuration,
    )

    assert decision.allowed
    assert decision.content is not None
    assert decision.content.redaction_status is RedactionStatus.REDACTED
    assert "secret" not in (decision.content.prompt_preview or "")
    assert len(decision.content.prompt_preview or "") <= 18
    assert len(decision.content.response_preview or "") <= 18


@pytest.mark.parametrize(
    ("policy_changes", "expected_reason"),
    (
        ({"approved_accounts": ()}, "configuration_invalid"),
        ({"approved_regions": ()}, "configuration_invalid"),
        ({"access_policy_reference": None}, "configuration_invalid"),
    ),
)
def test_missing_approval_boundaries_fail_closed_without_exposing_content(
    policy_changes, expected_reason
):
    base = enabled_configuration().content_analysis
    configuration = enabled_configuration(
        content_analysis=replace(base, **policy_changes)
    )

    decision = gate().authorize(make_record(), configuration)

    assert not decision.allowed
    assert decision.content is None
    assert decision.content_state is ContentAccessState.UNAUTHORIZED
    assert decision.reason == expected_reason
    assert decision.record is not None
    assert decision.record.sensitive_content is None


@pytest.mark.parametrize(
    ("record_changes", "configuration_changes", "boundary", "expected_reason"),
    (
        (
            {},
            {
                "content_analysis": ContentAnalysisPolicy(
                    state=ContentAnalysisState.ENABLED,
                    approved_accounts=("999999999999",),
                    approved_regions=(REGION,),
                    access_policy_reference=POLICY,
                    retention_days=30,
                )
            },
            PrivacyBoundary(now=NOW, approved_access_policies=frozenset({POLICY})),
            "account_not_approved",
        ),
        (
            {},
            {
                "content_analysis": ContentAnalysisPolicy(
                    state=ContentAnalysisState.ENABLED,
                    approved_accounts=(ACCOUNT,),
                    approved_regions=("eu-west-1",),
                    access_policy_reference=POLICY,
                    retention_days=30,
                )
            },
            PrivacyBoundary(now=NOW, approved_access_policies=frozenset({POLICY})),
            "region_not_approved",
        ),
        (
            {},
            {},
            PrivacyBoundary(now=NOW, approved_access_policies=frozenset({"other-policy"})),
            "access_policy_not_approved",
        ),
    ),
)
def test_unapproved_account_region_and_access_policy_fail_closed(
    record_changes, configuration_changes, boundary, expected_reason
):
    configuration = enabled_configuration(**configuration_changes)
    decision = DefaultPrivacyGate(boundary=boundary).authorize(
        make_record(**record_changes), configuration
    )

    assert not decision.allowed
    assert decision.content is None
    assert decision.content_state is ContentAccessState.UNAUTHORIZED
    assert decision.reason == expected_reason
    assert decision.record is not None
    assert decision.record.sensitive_content is None


def test_expired_content_is_rejected_before_redaction():
    class ExplodingRedactor:
        def redact(self, content, configuration):
            raise AssertionError("expired content must not reach the redactor")

    decision = DefaultPrivacyGate(
        redactor=ExplodingRedactor(),
        boundary=PrivacyBoundary(now=NOW, approved_access_policies=frozenset({POLICY})),
    ).authorize(
        make_record(source_timestamp_utc=datetime(2025, 1, 1, tzinfo=UTC)),
        enabled_configuration(),
    )

    assert not decision.allowed
    assert decision.content_state is ContentAccessState.EXPIRED
    assert decision.reason == "content_expired"
    assert decision.record is not None
    assert decision.record.sensitive_content is None


def test_malformed_content_is_rejected_without_calling_redactor():
    decision = gate().authorize(
        make_record(sensitive_content=SensitiveContent(prompt_preview=object())),  # type: ignore[arg-type]
        enabled_configuration(),
    )

    assert not decision.allowed
    assert decision.content_state is ContentAccessState.MALFORMED
    assert decision.reason == "content_malformed"
    assert decision.content is None
    assert decision.record is not None
    assert decision.record.sensitive_content is None


def test_previously_redacted_content_is_not_treated_as_available():
    decision = gate().authorize(
        make_record(
            sensitive_content=SensitiveContent(redaction_status=RedactionStatus.REDACTED)
        ),
        enabled_configuration(),
    )

    assert not decision.allowed
    assert decision.content_state is ContentAccessState.REDACTED
    assert decision.reason == "content_redacted"
    assert decision.record is not None
    assert decision.record.sensitive_content is None


def test_redaction_failure_blocks_dependent_content_and_preserves_non_content_signals():
    class FailingRedactor:
        def redact(self, content, configuration):
            return RedactionResult(
                failure=RedactionFailure(
                    code="REDACTION_FAILED",
                    detail="secret prompt leaked",
                )
            )

    decision = gate(redactor=FailingRedactor()).authorize(
        make_record(), enabled_configuration()
    )

    assert not decision.allowed
    assert decision.content is None
    assert decision.redaction_event
    assert decision.content_state is ContentAccessState.REDACTION_FAILED
    assert decision.reason == "redaction_failed"
    assert "secret prompt" not in repr(decision)
    assert decision.non_content_record is not None
    assert decision.non_content_record.model_id == "amazon.nova-pro-v1:0"
    assert decision.non_content_record.operation == "InvokeModel"
    assert decision.non_content_record.input_token_count == 12
    assert decision.non_content_record.output_token_count == 20
    assert decision.non_content_record.sensitive_content is None


def test_excluded_content_blocks_only_content_dependent_path():
    configuration = enabled_configuration(
        privacy=PrivacyPolicy(exclusion_rules=("prompt_preview", "response_preview"))
    )

    decision = gate().authorize(make_record(), configuration)

    assert not decision.allowed
    assert decision.content is None
    assert decision.content_state is ContentAccessState.EXCLUDED
    assert decision.reason == "content_excluded"
    assert decision.non_content_record is not None
    assert decision.non_content_record.output_token_count == 20
    assert decision.non_content_record.sensitive_content is None


def test_missing_content_is_unavailable_but_non_content_signals_remain_available():
    decision = gate().authorize(
        make_record(sensitive_content=None), enabled_configuration()
    )

    assert not decision.allowed
    assert decision.content_state is ContentAccessState.ABSENT
    assert decision.reason == "content_unavailable"
    assert decision.non_content_record is not None
    assert decision.non_content_record.source_request_id == "request-1"
    assert decision.non_content_record.model_id == "amazon.nova-pro-v1:0"
    assert decision.non_content_record.input_token_count == 12
    assert decision.non_content_record.request_metadata == {"team": "platform"}


def test_not_assessed_prompt_quality_has_no_content_or_evidence():
    assessment = not_assessed_prompt_quality(enabled_configuration(), "content_redacted")

    assert assessment.content_state is ContentAnalysisState.ENABLED
    assert set(assessment.dimensions) == {
        "clarity",
        "specificity",
        "context_completeness",
        "task_definition",
        "output_constraints",
    }
    for dimension in assessment.dimensions.values():
        assert dimension.status.value == "not_assessed"
        assert dimension.score is None
        assert dimension.confidence_band is None
        assert dimension.evidence_reference is None
        assert dimension.not_assessed_reason == "content_redacted"


def test_diagnostics_and_derived_record_serialization_never_contains_raw_content():
    content = "top-secret-value"

    class FailingRedactor:
        def redact(self, value, configuration):
            return RedactionResult(failure=RedactionFailure(detail=content))

    decision = DefaultPrivacyGate(
        redactor=FailingRedactor(),
        boundary=PrivacyBoundary(now=NOW, approved_access_policies=frozenset({POLICY})),
    ).authorize(
        make_record(
            sensitive_content=SensitiveContent(
                prompt_preview=content,
                response_preview=content,
            )
        ),
        enabled_configuration(),
    )

    assert content not in repr(decision)
    assert decision.content is None
    assert decision.record is not None
    assert decision.record.to_dict().get("sensitive_content") is None
    assert content not in repr(decision.record.to_dict())
