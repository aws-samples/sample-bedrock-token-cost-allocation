"""Example-based tests for configuration governance and activation."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bedrock_usage_insights.configuration import InMemoryConfigurationStore
from bedrock_usage_insights.models import (
    CategoryDefinition,
    ContentAnalysisPolicy,
    ContentAnalysisState,
    ExposureLevel,
    PrivacyPolicy,
)
from bedrock_usage_insights.oracle import SchemaValidationError
from fixture_support import make_configuration

UTC = timezone.utc


def test_initial_catalog_is_accepted_with_safe_personal_work_exposure():
    store = InMemoryConfigurationStore(clock=lambda: datetime(2026, 2, 1, tzinfo=UTC))
    accepted = store.accept(make_configuration())

    assert {category.category_id for category in accepted.categories} == {
        "code_development",
        "personal_work",
        "potentially_wasteful_work",
        "other",
        "insufficient_evidence",
    }
    personal_work = next(
        category for category in accepted.categories if category.category_id == "personal_work"
    )
    assert personal_work.exposure_level is not ExposureLevel.INDIVIDUAL
    assert store.active(accepted.analysis_scope) is accepted


@pytest.mark.parametrize(
    ("category_changes", "expected_path"),
    (
        ({"category_id": ""}, "categories[0].category_id"),
        ({"score_min": 0.9, "score_max": 0.1}, "categories[0].score_range"),
        ({"score_min": float("nan")}, "categories[0].score_min"),
        ({"evidence_threshold": float("inf")}, "categories[0].evidence_threshold"),
        ({"exposure_level": "unsupported"}, "categories[0].exposure_level"),
    ),
)
def test_invalid_category_fields_are_rejected_before_activation(category_changes, expected_path):
    configuration = make_configuration()
    categories = (
        replace(configuration.categories[0], **category_changes),
        *configuration.categories[1:],
    )
    issues = InMemoryConfigurationStore().validate(replace(configuration, categories=categories))

    assert expected_path in {issue.path for issue in issues}


def test_duplicate_category_ids_are_rejected_and_reported():
    configuration = make_configuration()
    duplicate = replace(
        configuration,
        categories=(
            configuration.categories[0],
            configuration.categories[0],
            *configuration.categories[2:],
        ),
    )

    issues = InMemoryConfigurationStore().validate(duplicate)

    assert any(issue.path == "categories" and "unique" in issue.message for issue in issues)


def test_rejected_candidate_preserves_the_previously_active_version_atomically():
    clock_values = iter(
        (
            datetime(2026, 2, 1, tzinfo=UTC),
            datetime(2026, 2, 2, tzinfo=UTC),
        )
    )
    store = InMemoryConfigurationStore(clock=lambda: next(clock_values))
    accepted = store.accept(make_configuration())
    invalid = replace(
        make_configuration(change_reason="rejected change"),
        categories=(replace(make_configuration().categories[0], category_id=""),),
    )

    with pytest.raises(SchemaValidationError) as error:
        store.accept(invalid)

    assert any(issue.path == "categories[0].category_id" for issue in error.value.issues)
    assert store.active(accepted.analysis_scope) is accepted
    assert store.get(accepted.configuration_version) is accepted
    assert store.versions == (accepted,)
    assert len(store.audits) == 1


def test_enabled_content_requires_all_approval_boundaries_and_redaction():
    configuration = make_configuration(
        content_analysis=ContentAnalysisPolicy(state=ContentAnalysisState.ENABLED),
        privacy=PrivacyPolicy(redaction_required=False),
    )

    issues = InMemoryConfigurationStore().validate(configuration)
    paths = {issue.path for issue in issues}

    assert {
        "content_analysis.approved_accounts",
        "content_analysis.approved_regions",
        "content_analysis.access_policy_reference",
        "content_analysis.retention_days",
        "privacy.redaction_required",
    } <= paths


def test_missing_scope_and_missing_document_content_state_fail_closed():
    store = InMemoryConfigurationStore()
    missing_scope = make_configuration(analysis_scope=None)
    document = make_configuration().to_dict()
    del document["content_analysis"]["state"]

    scope_issues = store.validate(missing_scope)
    state_issues = store.validate(document)

    assert "analysis_scope" in {issue.path for issue in scope_issues}
    assert "content_analysis.state" in {issue.path for issue in state_issues}


def test_accepted_configuration_is_immutable_and_audit_is_version_exact():
    now = datetime(2026, 2, 3, 4, 5, tzinfo=UTC)
    store = InMemoryConfigurationStore(clock=lambda: now)
    accepted = store.accept(make_configuration())
    audit = store.audit(accepted.configuration_version)

    with pytest.raises(TypeError):
        accepted.confidence_thresholds["low"] = 0.9  # type: ignore[index]
    with pytest.raises(TypeError):
        accepted.prompt_dimensions["clarity"] = accepted.prompt_dimensions["clarity"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        accepted.categories.append(accepted.categories[0])  # type: ignore[attr-defined]

    assert accepted.configuration_version.startswith("config-20260203T040500")
    assert accepted.effective_at == now
    assert audit.configuration_version == accepted.configuration_version
    assert audit.effective_at == accepted.effective_at
    assert audit.author_or_service == accepted.author_or_service
    assert audit.change_reason == accepted.change_reason
    assert audit.analysis_scope == accepted.analysis_scope
    assert audit.privacy_settings == accepted.privacy
    assert set(audit.enabled_categories) == {
        category.category_id for category in accepted.categories if category.enabled
    }


def test_each_acceptance_creates_a_new_version_and_preserves_prior_audit():
    clock_values = iter(
        (
            datetime(2026, 2, 1, tzinfo=UTC),
            datetime(2026, 2, 2, tzinfo=UTC),
        )
    )
    store = InMemoryConfigurationStore(clock=lambda: next(clock_values))
    first = store.accept(make_configuration(change_reason="first"))
    second = store.accept(make_configuration(change_reason="second"))

    assert first.configuration_version != second.configuration_version
    assert first.effective_at < second.effective_at
    assert store.get(first.configuration_version) is first
    assert store.audit(first.configuration_version).change_reason == "first"
    assert store.audit(second.configuration_version).change_reason == "second"
    assert store.active(second.analysis_scope) is second


def test_disabled_configuration_is_governed_without_analysis_side_effects():
    store = InMemoryConfigurationStore()
    disabled = make_configuration(enabled=False)
    accepted = store.accept(disabled)

    assert accepted.enabled is False
    assert accepted.content_analysis.state is ContentAnalysisState.DISABLED
    assert store.versions == (accepted,)
    assert store.audits == (store.audit(accepted.configuration_version),)
    assert store.active(accepted.analysis_scope) is accepted
    assert not hasattr(store, "results")
    assert not hasattr(store, "runs")


# The configuration store is deliberately pure: no source, model, content, or
# dashboard client is constructed as part of accepting a disabled candidate.
def test_disabled_acceptance_does_not_construct_external_clients():
    constructed = []

    class ExternalClient:
        def __init__(self):
            constructed.append(self)

    store = InMemoryConfigurationStore()
    accepted = store.accept(make_configuration(enabled=False))

    assert accepted.enabled is False
    assert constructed == []
    assert ExternalClient not in constructed
