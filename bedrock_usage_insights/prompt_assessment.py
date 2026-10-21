"""Deterministic, content-safe prompt-quality assessment."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .models import (
    ConfidenceBand,
    ContentAnalysisState,
    EvidenceReference,
    InsightConfiguration,
    PromptAssessmentStatus,
    PromptDimension,
    PromptDimensionConfiguration,
    PromptQualityAssessment,
    PromptQualityDimensionResult,
    SensitiveContent,
    SourceFieldClass,
)


_PROMPT_FEATURE_SIGNALS = {
    PromptDimension.CLARITY.value: (
        "prompt_context_marker",
        "prompt_specificity_marker",
    ),
    PromptDimension.SPECIFICITY.value: ("prompt_specificity_marker",),
    PromptDimension.CONTEXT_COMPLETENESS.value: ("prompt_context_marker",),
    PromptDimension.TASK_DEFINITION.value: ("prompt_task_marker",),
    PromptDimension.OUTPUT_CONSTRAINTS.value: ("prompt_output_constraint_marker",),
}

_WORD_RE = re.compile(r"[\w]+(?:['’-][\w]+)*", re.UNICODE)
_SENTENCE_RE = re.compile(r"[^.!?]+(?:[.!?]+|$)", re.UNICODE)
_NUMBER_RE = re.compile(r"\b\d+(?:[.,:/-]\d+)*\b")
_QUOTED_RE = re.compile(r"(?:\"[^\"]+\"|'[^']+'|`[^`]+`)")
_ACTION_RE = re.compile(
    r"\b(?:analy[sz]e|build|calculate|classify|compare|create|describe|design|draft|"
    r"extract|find|generate|implement|list|prepare|recommend|refactor|return|summari[sz]e|"
    r"transform|write)\b",
    re.IGNORECASE,
)
_CONTEXT_RE = re.compile(
    r"\b(?:assume|background|because|context|constraint|details?|given|input|provided|"
    r"requirement|scenario|using|where|when|without)\b",
    re.IGNORECASE,
)
_OUTPUT_RE = re.compile(
    r"\b(?:answer|column|format|json|markdown|output|respond|response|schema|table|"
    r"template|xml|yaml)\b|(?:^|\n)\s*(?:[-*]|\d+[.)])\s+",
    re.IGNORECASE,
)


class PromptAssessor(Protocol):
    """Port for independently configured prompt-quality dimensions."""

    def assess(
        self,
        content: SensitiveContent | None,
        configuration: InsightConfiguration,
    ) -> PromptQualityAssessment:
        """Assess prompt characteristics or mark content unavailable."""
        ...


@dataclass(frozen=True)
class _PromptFeatures:
    word_count: int
    sentence_count: int
    average_sentence_words: float
    action_markers: int
    context_markers: int
    output_markers: int
    specificity_markers: int
    punctuation_issues: int
    line_count: int


class DeterministicPromptAssessor:
    """Assess prompt characteristics using bounded lexical features only.

    The assessor accepts content only at the authorized boundary.  A privacy
    decision can be passed in place of ``SensitiveContent``; denied decisions
    are converted to content-free ``not_assessed`` results.  Raw content is
    never copied into a score, evidence summary, exception, or result model.
    """

    def assess(
        self,
        content: SensitiveContent | None,
        configuration: InsightConfiguration,
    ) -> PromptQualityAssessment:
        dimensions = _dimension_configurations(configuration)
        content_state = _content_analysis_state(configuration)

        if not _configuration_allows_content(configuration):
            return _not_assessed_all(
                dimensions,
                "content_analysis_disabled",
                content_state,
            )

        prompt, unavailable_reason = _authorized_prompt(content)
        if unavailable_reason is not None:
            return _not_assessed_all(dimensions, unavailable_reason, content_state)

        assert prompt is not None
        features = _extract_features(prompt)
        results: dict[str, PromptQualityDimensionResult] = {}
        for dimension in PromptDimension:
            name = dimension.value
            settings = dimensions[name]
            if not _enabled(settings):
                results[name] = _not_assessed(name, "dimension_disabled")
                continue
            if not _valid_threshold(settings):
                results[name] = _not_assessed(name, "invalid_dimension_configuration")
                continue
            score, evidence_count = _score_dimension(dimension, features)
            results[name] = _assessed(
                dimension,
                score,
                evidence_count,
                settings.threshold,
            )

        return PromptQualityAssessment(
            dimensions=results,
            content_state=content_state,
        )


# Descriptive alias for callers using the component name from the design.
PromptQualityAssessor = DeterministicPromptAssessor


def assess_prompt(
    authorized_content: SensitiveContent | None | Any,
    configuration: InsightConfiguration | Mapping[str, PromptDimensionConfiguration],
) -> PromptQualityAssessment:
    """Assess authorized content with either a full or dimension-only config.

    The full configuration is the normal API.  Accepting a dimension mapping
    keeps the pure component usable by adapters that already enforce privacy
    and content enablement at their boundary.
    """

    if isinstance(configuration, InsightConfiguration):
        return DeterministicPromptAssessor().assess(authorized_content, configuration)

    dimensions = _coerce_dimension_mapping(configuration)
    prompt, reason = _authorized_prompt(authorized_content)
    if reason is not None:
        return _not_assessed_all(dimensions, reason, ContentAnalysisState.ENABLED)
    if prompt is None:
        return _not_assessed_all(dimensions, "content_unavailable", ContentAnalysisState.ENABLED)

    features = _extract_features(prompt)
    results: dict[str, PromptQualityDimensionResult] = {}
    for dimension in PromptDimension:
        name = dimension.value
        settings = dimensions[name]
        if not _enabled(settings):
            results[name] = _not_assessed(name, "dimension_disabled")
        elif not _valid_threshold(settings):
            results[name] = _not_assessed(name, "invalid_dimension_configuration")
        else:
            score, evidence_count = _score_dimension(dimension, features)
            results[name] = _assessed(dimension, score, evidence_count, settings.threshold)
    return PromptQualityAssessment(
        dimensions=results,
        content_state=ContentAnalysisState.ENABLED,
    )


def _dimension_configurations(
    configuration: InsightConfiguration,
) -> dict[str, PromptDimensionConfiguration]:
    return _coerce_dimension_mapping(configuration.prompt_dimensions)


def _coerce_dimension_mapping(
    values: Mapping[str, PromptDimensionConfiguration],
) -> dict[str, PromptDimensionConfiguration]:
    return {
        dimension.value: values.get(dimension.value, PromptDimensionConfiguration())
        for dimension in PromptDimension
    }


def _configuration_allows_content(configuration: InsightConfiguration) -> bool:
    return configuration.enabled and configuration.content_analysis.state is ContentAnalysisState.ENABLED


def _content_analysis_state(configuration: InsightConfiguration) -> ContentAnalysisState:
    state = configuration.content_analysis.state
    return state if isinstance(state, ContentAnalysisState) else ContentAnalysisState.DISABLED


def _authorized_prompt(content: Any) -> tuple[str | None, str | None]:
    """Return a prompt only when the supplied boundary permits it."""

    # PrivacyDecision is intentionally handled structurally to avoid coupling
    # this pure assessor to a privacy implementation or AWS adapter.
    if hasattr(content, "allowed") and hasattr(content, "content"):
        if not bool(getattr(content, "allowed")):
            return None, _privacy_reason(getattr(content, "content_state", None))
        content = getattr(content, "content")

    if content is None:
        return None, "content_unavailable"
    if not isinstance(content, SensitiveContent):
        return None, "content_malformed"
    redaction_status = getattr(content, "redaction_status", None)
    redaction_value = getattr(redaction_status, "value", redaction_status)
    if redaction_value == "failed":
        return None, "content_redaction_failed"
    if redaction_value not in {"not_requested", "redacted"}:
        return None, "content_malformed"
    prompt = content.prompt_preview
    response = content.response_preview
    if not isinstance(prompt, (str, type(None))) or not isinstance(response, (str, type(None))):
        return None, "content_malformed"
    if prompt is None:
        return None, "content_unavailable"
    if not prompt.strip():
        return None, "content_unavailable"
    return prompt, None


def _privacy_reason(state: Any) -> str:
    value = getattr(state, "value", state)
    return {
        "content_disabled": "content_analysis_disabled",
        "content_unavailable": "content_unavailable",
        "content_redacted": "content_redacted",
        "content_malformed": "content_malformed",
        "content_expired": "content_expired",
        "content_excluded": "content_excluded",
        "content_policy_blocked": "content_unauthorized",
        "redaction_failed": "content_redaction_failed",
    }.get(value, "content_unavailable")


def _extract_features(prompt: str) -> _PromptFeatures:
    words = _WORD_RE.findall(prompt)
    sentence_lengths = [len(_WORD_RE.findall(sentence)) for sentence in _SENTENCE_RE.findall(prompt)]
    sentence_lengths = [length for length in sentence_lengths if length > 0]
    sentence_count = max(1, len(sentence_lengths))
    average = len(words) / sentence_count if words else 0.0
    punctuation_issues = len(re.findall(r"(?:!!+|\?\?+|\.\.\.|[^.!?\s][.!?]{3,})", prompt))
    return _PromptFeatures(
        word_count=len(words),
        sentence_count=sentence_count,
        average_sentence_words=average,
        action_markers=len(_ACTION_RE.findall(prompt)),
        context_markers=len(_CONTEXT_RE.findall(prompt)),
        output_markers=len(_OUTPUT_RE.findall(prompt)),
        specificity_markers=len(_NUMBER_RE.findall(prompt)) + len(_QUOTED_RE.findall(prompt)),
        punctuation_issues=punctuation_issues,
        line_count=max(1, prompt.count("\n") + 1),
    )


def _score_dimension(
    dimension: PromptDimension,
    features: _PromptFeatures,
) -> tuple[float, int]:
    if dimension is PromptDimension.CLARITY:
        length_score = min(1.0, features.word_count / 12.0)
        sentence_score = 1.0 if 4 <= features.average_sentence_words <= 45 else 0.7
        punctuation_score = max(0.0, 1.0 - 0.25 * features.punctuation_issues)
        score = 0.35 * length_score + 0.45 * sentence_score + 0.20 * punctuation_score
        evidence = features.word_count + features.sentence_count
    elif dimension is PromptDimension.SPECIFICITY:
        score = min(1.0, (features.specificity_markers + features.context_markers) / 4.0)
        evidence = features.specificity_markers + features.context_markers
    elif dimension is PromptDimension.CONTEXT_COMPLETENESS:
        score = min(1.0, features.context_markers / 3.0)
        evidence = features.context_markers
    elif dimension is PromptDimension.TASK_DEFINITION:
        score = min(1.0, features.action_markers / 2.0)
        evidence = features.action_markers
    else:
        score = min(1.0, features.output_markers / 2.0)
        evidence = features.output_markers
    return round(max(0.0, min(1.0, score)), 6), max(0, evidence)


def _assessed(
    dimension: PromptDimension,
    score: float,
    evidence_count: int,
    threshold: float,
) -> PromptQualityDimensionResult:
    signal_names = _PROMPT_FEATURE_SIGNALS[dimension.value]
    return PromptQualityDimensionResult(
        dimension=dimension.value,
        status=PromptAssessmentStatus.ASSESSED,
        score=score,
        confidence_band=_confidence(evidence_count),
        evidence_reference=EvidenceReference(
            signal_names=signal_names,
            source_field_classes=(SourceFieldClass.PERMITTED_CONTENT_DERIVED_FEATURE,),
            summaries=("bounded content-derived feature counts",),
            counts={"feature_count": evidence_count},
        ),
        threshold_interpretation=(
            f"Configured threshold {threshold:.2f}; score meets the threshold."
            if score >= threshold
            else f"Configured threshold {threshold:.2f}; score is below the threshold."
        ),
    )


def _confidence(evidence_count: int) -> ConfidenceBand:
    if evidence_count >= 3:
        return ConfidenceBand.HIGH
    if evidence_count >= 1:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


def _not_assessed(dimension: str, reason: str) -> PromptQualityDimensionResult:
    return PromptQualityDimensionResult(
        dimension=dimension,
        status=PromptAssessmentStatus.NOT_ASSESSED,
        not_assessed_reason=reason,
    )


def _not_assessed_all(
    dimensions: Mapping[str, PromptDimensionConfiguration],
    reason: str,
    content_state: ContentAnalysisState,
) -> PromptQualityAssessment:
    return PromptQualityAssessment(
        dimensions={
            dimension.value: _not_assessed(dimension.value, reason)
            for dimension in PromptDimension
        },
        content_state=content_state,
    )


def _enabled(settings: PromptDimensionConfiguration) -> bool:
    return isinstance(settings, PromptDimensionConfiguration) and settings.enabled is True


def _valid_threshold(settings: PromptDimensionConfiguration) -> bool:
    return (
        isinstance(settings, PromptDimensionConfiguration)
        and isinstance(settings.threshold, (int, float))
        and not isinstance(settings.threshold, bool)
        and math.isfinite(settings.threshold)
        and 0.0 <= settings.threshold <= 1.0
    )


__all__ = [
    "DeterministicPromptAssessor",
    "PromptAssessor",
    "PromptQualityAssessor",
    "assess_prompt",
]
