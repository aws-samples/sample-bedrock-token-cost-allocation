"""Property-based tests for configuration governance and semantic round trips."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Callable

import pytest
from hypothesis import given, settings, strategies as st

from bedrock_usage_insights.configuration import (
    InMemoryConfigurationStore,
    SchemaValidationError,
    parse_configuration,
)
from bedrock_usage_insights.models import (
    ContentAnalysisPolicy,
    DashboardPublicationPolicy,
    InsightConfiguration,
    ModelAssistancePolicy,
    PrivacyPolicy,
)
from bedrock_usage_insights.oracle import ConfigurationSchemaOracle
from fixture_support import configuration_strategy


ConfigurationMutation = Callable[[InsightConfiguration], tuple[InsightConfiguration, str]]


def valid_configuration_strategy():
    return configuration_strategy().filter(lambda config: not ConfigurationSchemaOracle.validate(config))


def _replace_category(
    configuration: InsightConfiguration,
    index: int,
    **changes: object,
) -> InsightConfiguration:
    categories = list(configuration.categories)
    categories[index] = replace(categories[index], **changes)
    return replace(configuration, categories=tuple(categories))


def _invalid_category_id(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return _replace_category(configuration, 0, category_id=""), "categories[0].category_id"


def _duplicate_category_id(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return _replace_category(
        configuration,
        1,
        category_id=configuration.categories[0].category_id,
    ), "categories"


def _oversized_category_description(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return _replace_category(configuration, 0, description="x" * 501), "categories[0].description"


def _unsupported_category_signal(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return _replace_category(configuration, 0, signals=("unsupported_signal",)), "categories[0].signals"


def _non_finite_category_threshold(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return _replace_category(configuration, 0, evidence_threshold=math.nan), "categories[0].evidence_threshold"


def _unsupported_exposure(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return _replace_category(configuration, 0, exposure_level="individuals_only"), "categories[0].exposure_level"


def _unsupported_signal_definition(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    signals = list(configuration.signal_definitions)
    signals[0] = replace(signals[0], name="unsupported_signal")
    return replace(configuration, signal_definitions=tuple(signals)), "signal_definitions[0].name"


def _duplicate_signal_definition(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return replace(
        configuration,
        signal_definitions=(configuration.signal_definitions[0],) * 2,
    ), "signal_definitions"


def _invalid_confidence_threshold(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return replace(
        configuration,
        confidence_thresholds={"low": 0.0, "medium": 0.5, "high": math.inf},
    ), "confidence_thresholds"


def _missing_scope(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return replace(configuration, analysis_scope=None), "analysis_scope"


def _unsafe_content_approval(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return replace(configuration, content_analysis=ContentAnalysisPolicy(state="enabled")), "content_analysis"


def _invalid_minimum_group_size(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return replace(configuration, privacy=replace(configuration.privacy, minimum_group_size=4)), "privacy.minimum_group_size"


def _raw_content_output(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return replace(
        configuration,
        privacy=replace(configuration.privacy, exclude_raw_content_from_derived_output=False),
    ), "privacy.exclude_raw_content_from_derived_output"


def _invalid_retention(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return replace(configuration, retention_days=0), "retention_days"


def _unsafe_dashboard(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return replace(
        configuration,
        dashboard_publication=DashboardPublicationPolicy(
            enabled=True,
            aggregate_only=False,
            approved_role_reference=None,
        ),
    ), "dashboard_publication.aggregate_only"


def _incomplete_model_assistance(configuration: InsightConfiguration) -> tuple[InsightConfiguration, str]:
    return replace(configuration, model_assistance=ModelAssistancePolicy(enabled=True)), "model_assistance"


INVALID_MUTATIONS: tuple[ConfigurationMutation, ...] = (
    _invalid_category_id,
    _duplicate_category_id,
    _oversized_category_description,
    _unsupported_category_signal,
    _non_finite_category_threshold,
    _unsupported_exposure,
    _unsupported_signal_definition,
    _duplicate_signal_definition,
    _invalid_confidence_threshold,
    _missing_scope,
    _unsafe_content_approval,
    _invalid_minimum_group_size,
    _raw_content_output,
    _invalid_retention,
    _unsafe_dashboard,
    _incomplete_model_assistance,
)


# Feature: bedrock-usage-insights, Property 1: Configuration oracle and semantic round trip.
@settings(max_examples=100)
@given(valid_configuration_strategy())
def test_property_1_valid_configurations_have_semantic_json_round_trip(configuration):
    serialized = configuration.to_dict()
    restored = parse_configuration(json.dumps(serialized))

    assert restored.to_dict() == serialized
    assert restored.configuration_version == configuration.configuration_version
    assert restored.effective_at == configuration.effective_at
    assert restored.categories == configuration.categories
    assert restored.confidence_thresholds == configuration.confidence_thresholds
    assert restored.privacy == configuration.privacy
    assert restored.content_analysis == configuration.content_analysis
    assert restored.analysis_scope == configuration.analysis_scope


# Feature: bedrock-usage-insights, Property 1: Configuration oracle and semantic round trip.
@settings(max_examples=100)
@given(valid_configuration_strategy(), st.sampled_from(INVALID_MUTATIONS))
def test_property_1_invalid_fields_are_rejected_before_acceptance(configuration, mutation):
    invalid, expected_path = mutation(configuration)
    store = InMemoryConfigurationStore()

    issues = store.validate(invalid)

    assert issues
    assert any(issue.path == expected_path or issue.path.startswith(expected_path + ".") for issue in issues)
    with pytest.raises(SchemaValidationError):
        store.accept(invalid)
    assert store.versions == ()
    assert store.audits == ()


# Feature: bedrock-usage-insights, Property 1: Configuration oracle and semantic round trip.
@settings(max_examples=100)
@given(valid_configuration_strategy())
def test_property_1_missing_content_state_is_rejected_before_acceptance(configuration):
    candidate = configuration.to_dict()
    candidate["content_analysis"].pop("state")
    store = InMemoryConfigurationStore()

    issues = store.validate(candidate)

    assert any(issue.path == "content_analysis.state" for issue in issues)
    with pytest.raises(SchemaValidationError):
        store.accept(candidate)
    assert store.versions == ()


# Feature: bedrock-usage-insights, Property 1: Configuration oracle and semantic round trip.
@settings(max_examples=100)
@given(valid_configuration_strategy(), st.sampled_from(INVALID_MUTATIONS))
def test_property_1_rejected_candidate_cannot_replace_prior_accepted_version(configuration, mutation):
    store = InMemoryConfigurationStore()
    accepted = store.accept(configuration)
    previous_versions = store.versions
    previous_audits = store.audits
    previous_active = store.active(configuration.analysis_scope)

    invalid, _ = mutation(configuration)
    with pytest.raises(SchemaValidationError):
        store.accept(invalid)

    assert store.active(configuration.analysis_scope) is previous_active
    assert store.get(accepted.configuration_version) is accepted
    assert store.versions == previous_versions
    assert store.audits == previous_audits
