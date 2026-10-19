"""Unit tests for privacy authorization and redaction ordering."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from bedrock_usage_insights.models import (
    ContentAnalysisPolicy,
    ContentAnalysisState,
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
from fixture_support import make_configuration, make_invocation_record

UTC = timezone.utc


def enabled_configuration(**changes):
    values = {
        "enabled": True,
        "content_analysis": ContentAnalysisPolicy(
            state=ContentAnalysisState.ENABLED,
            approved_accounts=("123456789012",),
            approved_regions=("us-east-1",),
            access_policy_reference="privacy-policy-v1",
            retention_days=30,
            max_preview_length=100,
        ),
    }
    values.update(changes)
    return make_configuration(**values)


def content_record(**changes):
    values = {
        "sensitive_content": SensitiveContent(
            prompt_preview="private secret prompt",
            response_preview="private response",
        )
    }
    values.update(changes)
    return make_invocation_record(**values)


def test_disabled_content_returns_sanitized_record_and_blocks_content():
    record = content_record()
    decision = DefaultPrivacyGate().authorize(record, make_configuration(enabled=True))

    assert not decision.allowed
    assert decision.content is None
    assert decision.content_state is ContentAccessState.DISABLED
    assert decision.reason == "content_disabled"
    assert decision.record is not None
    assert decision.record.sensitive_content is None
    assert record.sensitive_content is not None


def test_missing_content_is_unavailable_but_non_content_record_is_preserved():
    record = make_invocation_record(sensitive_content=None)
    decision = DefaultPrivacyGate(
        boundary=PrivacyBoundary(now=datetime(2026, 2, 3, 4, 5, tzinfo=UTC))
    ).authorize(record, enabled_configuration())

    assert not decision.allowed
    assert decision.content_state is ContentAccessState.ABSENT
    assert decision.reason == "content_unavailable"
    assert decision.non_content_record is not None
    assert decision.non_content_record.model_id == record.model_id
    assert decision.non_content_record.input_token_count == record.input_token_count


def test_approved_boundaries_and_redaction_are_required_before_content_access():
    record = content_record()
    gate = DefaultPrivacyGate(
        boundary=PrivacyBoundary(
            now=datetime(2026, 2, 3, 4, 5, tzinfo=UTC),
            approved_access_policies=frozenset({"privacy-policy-v1"}),
        )
    )

    decision = gate.authorize(record, enabled_configuration(privacy=PrivacyPolicy(exclusion_rules=("secret",))))

    assert decision.allowed
    assert decision.content_state is ContentAccessState.AVAILABLE
    assert decision.content is not None
    assert decision.content.redaction_status is RedactionStatus.REDACTED
    assert decision.content.prompt_preview == "private [REDACTED] prompt"
    assert decision.record is not None
    assert decision.record.sensitive_content is None
    assert "secret" not in decision.content.prompt_preview


def test_unapproved_account_region_and_policy_fail_closed():
    record = content_record()
    for configuration in (
        enabled_configuration(
            content_analysis=ContentAnalysisPolicy(
                state=ContentAnalysisState.ENABLED,
                approved_accounts=("999999999999",),
                approved_regions=("us-east-1",),
                access_policy_reference="privacy-policy-v1",
                retention_days=30,
            )
        ),
        enabled_configuration(
            content_analysis=ContentAnalysisPolicy(
                state=ContentAnalysisState.ENABLED,
                approved_accounts=("123456789012",),
                approved_regions=("eu-west-1",),
                access_policy_reference="privacy-policy-v1",
                retention_days=30,
            )
        ),
    ):
        decision = DefaultPrivacyGate(
            boundary=PrivacyBoundary(now=datetime(2026, 2, 3, 4, 5, tzinfo=UTC))
        ).authorize(record, configuration)
        assert not decision.allowed
        assert decision.content is None
        assert decision.record is not None
        assert decision.record.sensitive_content is None
        assert decision.content_state is ContentAccessState.UNAUTHORIZED

    decision = DefaultPrivacyGate(
        boundary=PrivacyBoundary(approved_access_policies=frozenset({"other-policy"}))
    ).authorize(record, enabled_configuration())
    assert not decision.allowed
    assert decision.reason == "access_policy_not_approved"


def test_expired_content_is_not_sent_to_redactor():
    class ExplodingRedactor:
        def redact(self, content, configuration):
            raise AssertionError("expired content must not reach the redactor")

    record = content_record(
        source_timestamp_utc=datetime(2025, 1, 1, tzinfo=UTC)
    )
    decision = DefaultPrivacyGate(
        redactor=ExplodingRedactor(),
        boundary=PrivacyBoundary(now=datetime(2026, 2, 3, tzinfo=UTC)),
    ).authorize(record, enabled_configuration())

    assert not decision.allowed
    assert decision.content_state is ContentAccessState.EXPIRED
    assert decision.reason == "content_expired"
    assert decision.record is not None
    assert decision.record.sensitive_content is None


def test_redaction_failure_blocks_dependent_content_but_preserves_non_content():
    class FailingRedactor:
        def redact(self, content, configuration):
            return RedactionResult(
                failure=RedactionFailure(code="REDACTION_FAILED", detail="secret prompt leaked")
            )

    decision = DefaultPrivacyGate(
        redactor=FailingRedactor(),
        boundary=PrivacyBoundary(now=datetime(2026, 2, 3, 4, 5, tzinfo=UTC)),
    ).authorize(
        content_record(), enabled_configuration()
    )

    assert not decision.allowed
    assert decision.content is None
    assert decision.redaction_event
    assert decision.content_state is ContentAccessState.REDACTION_FAILED
    assert "secret prompt" not in (decision.reason or "")
    assert decision.record is not None
    assert decision.record.sensitive_content is None
    assert decision.record.model_id == "amazon.nova-pro-v1:0"


def test_excluded_fields_make_only_content_dependent_path_unavailable():
    configuration = enabled_configuration(
        privacy=PrivacyPolicy(exclusion_rules=("prompt_preview", "response_preview"))
    )
    decision = DefaultPrivacyGate(
        boundary=PrivacyBoundary(now=datetime(2026, 2, 3, 4, 5, tzinfo=UTC))
    ).authorize(content_record(), configuration)

    assert not decision.allowed
    assert decision.content is None
    assert decision.content_state is ContentAccessState.EXCLUDED
    assert decision.reason == "content_excluded"
    assert decision.record is not None
    assert decision.record.output_token_count == 20


def test_malformed_and_previously_redacted_content_are_not_exposed():
    malformed = content_record(
        sensitive_content=SensitiveContent(prompt_preview=object())  # type: ignore[arg-type]
    )
    gate = DefaultPrivacyGate(
        boundary=PrivacyBoundary(now=datetime(2026, 2, 3, 4, 5, tzinfo=UTC))
    )
    malformed_decision = gate.authorize(malformed, enabled_configuration())
    assert not malformed_decision.allowed
    assert malformed_decision.content_state is ContentAccessState.MALFORMED
    assert malformed_decision.record is not None
    assert malformed_decision.record.sensitive_content is None

    redacted = content_record(
        sensitive_content=SensitiveContent(redaction_status=RedactionStatus.REDACTED)
    )
    redacted_decision = gate.authorize(redacted, enabled_configuration())
    assert not redacted_decision.allowed
    assert redacted_decision.content_state is ContentAccessState.REDACTED
    assert redacted_decision.reason == "content_redacted"


def test_not_assessed_prompt_quality_contains_no_score_or_evidence():
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


def test_default_decision_and_diagnostics_never_serialize_raw_content():
    content = "top-secret-value"
    decision = DefaultPrivacyGate(
        redactor=type(
            "Redactor",
            (),
            {"redact": lambda self, value, configuration: RedactionResult(
                failure=RedactionFailure(detail=content)
            )},
        )(),
        boundary=PrivacyBoundary(now=datetime(2026, 2, 3, 4, 5, tzinfo=UTC)),
    ).authorize(content_record(
        sensitive_content=SensitiveContent(prompt_preview=content, response_preview=content)
    ), enabled_configuration())

    serialized = decision.__dict__
    assert content not in repr(serialized)
    assert decision.content is None
    assert decision.record is not None
    assert decision.record.to_dict().get("sensitive_content") is None
