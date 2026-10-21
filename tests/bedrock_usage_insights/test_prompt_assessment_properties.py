"""Property tests for prompt-quality score and dimension independence."""

from datetime import date, datetime, timezone

from hypothesis import given, settings, strategies as st

from bedrock_usage_insights.models import (
    AnalysisScope,
    ContentAnalysisPolicy,
    ContentAnalysisState,
    InsightConfiguration,
    PromptAssessmentStatus,
    PromptDimension,
    PromptDimensionConfiguration,
    SensitiveContent,
)
from bedrock_usage_insights.oracle import ConfigurationSchemaOracle
from bedrock_usage_insights.prompt_assessment import DeterministicPromptAssessor

UTC = timezone.utc
ACCOUNT = "123456789012"
REGION = "us-east-1"
DIMENSIONS = tuple(dimension.value for dimension in PromptDimension)


def _configuration(
    prompt_dimensions: dict[str, PromptDimensionConfiguration],
) -> InsightConfiguration:
    return InsightConfiguration(
        configuration_version="property-11-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="property-test",
        change_reason="prompt-quality dimension independence",
        analysis_scope=AnalysisScope(
            accounts=(ACCOUNT,),
            regions=(REGION,),
            models=("model",),
            operations=("InvokeModel",),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        ),
        enabled=True,
        content_analysis=ContentAnalysisPolicy(
            state=ContentAnalysisState.ENABLED,
            approved_accounts=(ACCOUNT,),
            approved_regions=(REGION,),
            access_policy_reference="approved-policy",
            retention_days=30,
            max_preview_length=512,
        ),
        prompt_dimensions=prompt_dimensions,
    )


@st.composite
def prompt_assessment_cases(draw: st.DrawFn):
    prompt = draw(
        st.one_of(
            st.sampled_from(
                (
                    "Build a concise JSON response using the provided context.",
                    "Compare the records and return a table with totals.",
                    "Analyze the input and describe the result.",
                )
            ),
            st.text(
                alphabet=st.characters(blacklist_categories=("Cs",)),
                min_size=1,
                max_size=256,
            ).filter(lambda value: bool(value.strip())),
        )
    )
    content = draw(
        st.one_of(
            st.none(),
            st.builds(
                SensitiveContent,
                prompt_preview=st.just(prompt),
                response_preview=st.one_of(st.none(), st.text(max_size=64)),
            ),
        )
    )
    dimensions = {
        dimension: PromptDimensionConfiguration(
            enabled=draw(st.booleans()),
            threshold=draw(
                st.floats(
                    min_value=0.0,
                    max_value=1.0,
                    allow_nan=False,
                    allow_infinity=False,
                )
            ),
        )
        for dimension in DIMENSIONS
    }
    target_dimension = draw(st.sampled_from(DIMENSIONS))
    return content, dimensions, target_dimension


# Feature: bedrock-usage-insights, Property 11: Prompt-quality score and dimension independence
# **Validates: Requirements 5.1, 5.2, 5.5**
@given(prompt_assessment_cases())
@settings(max_examples=100, deadline=None)
def test_property_11_prompt_quality_scores_are_bounded_and_dimensions_are_independent(case):
    content, dimensions, target_dimension = case
    configuration = _configuration(dimensions)
    assessment = DeterministicPromptAssessor().assess(content, configuration)

    assert set(assessment.dimensions) == set(DIMENSIONS)
    for dimension in DIMENSIONS:
        result = assessment.dimensions[dimension]
        assert result.status in {
            PromptAssessmentStatus.ASSESSED,
            PromptAssessmentStatus.NOT_ASSESSED,
        }
        if result.status is PromptAssessmentStatus.ASSESSED:
            assert isinstance(result.score, (int, float))
            assert not isinstance(result.score, bool)
            assert 0.0 <= result.score <= 1.0
            assert result.confidence_band is not None
            assert result.evidence_reference is not None
            assert result.evidence_reference.signal_names
            assert result.evidence_reference.source_field_classes
        else:
            assert result.score is None
            assert result.confidence_band is None
            assert result.evidence_reference is None
            assert result.not_assessed_reason

    disabled_target_dimensions = dict(dimensions)
    disabled_target_dimensions[target_dimension] = PromptDimensionConfiguration(
        enabled=False,
        threshold=dimensions[target_dimension].threshold,
    )
    disabled_target_configuration = _configuration(disabled_target_dimensions)
    disabled_target_assessment = DeterministicPromptAssessor().assess(
        content,
        disabled_target_configuration,
    )
    for dimension in DIMENSIONS:
        if dimension != target_dimension:
            assert disabled_target_assessment.dimensions[dimension] == assessment.dimensions[dimension]

    all_disabled_configuration = _configuration(
        {
            dimension: PromptDimensionConfiguration(enabled=False, threshold=0.5)
            for dimension in DIMENSIONS
        }
    )
    assert not ConfigurationSchemaOracle.validate(all_disabled_configuration)
    all_disabled_assessment = DeterministicPromptAssessor().assess(
        SensitiveContent(prompt_preview="Analyze the provided data."),
        all_disabled_configuration,
    )
    for result in all_disabled_assessment.dimensions.values():
        assert result.status is PromptAssessmentStatus.NOT_ASSESSED
        assert result.score is None
        assert result.confidence_band is None
        assert result.evidence_reference is None
        assert result.not_assessed_reason == "dimension_disabled"
