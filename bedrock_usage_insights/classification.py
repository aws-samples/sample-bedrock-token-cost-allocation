"""Deterministic, signal-only usage classification."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Optional

from .models import (
    Applicability,
    ApplicabilityMarkers,
    CategoryDefinition,
    ClassificationResult,
    ConfidenceBand,
    EvidenceReference,
    InsightConfiguration,
    Signal,
    SignalAvailability,
    UnknownReason,
    UsageCategory,
)

USE_LIMITATION = (
    "Results are for aggregate usage analysis and prompt improvement and are not for "
    "employee ranking, performance evaluation, disciplinary action, or automated access decisions."
)

_CONFLICTING_CATEGORIES = frozenset(
    {
        UsageCategory.CODE_DEVELOPMENT.value,
        UsageCategory.PERSONAL_WORK.value,
        UsageCategory.POTENTIALLY_WASTEFUL_WORK.value,
    }
)


class DeterministicClassifier:
    """Classify an ordered signal set using only active configured categories."""

    def classify(
        self,
        signal_set: Iterable[Signal],
        configuration: InsightConfiguration,
    ) -> ClassificationResult:
        signals = tuple(signal_set)
        if not configuration.enabled:
            return _unknown_result(
                UnknownReason.OUT_OF_SCOPE,
                signals=(),
                limitations=(USE_LIMITATION, "Usage insights analysis is disabled."),
            )

        categories = _active_categories(configuration)
        if threshold_error := _threshold_error(configuration, categories):
            return _unknown_result(
                UnknownReason.ANALYSIS_ERROR,
                limitations=(USE_LIMITATION, threshold_error),
            )

        configured_signal_names = {
            definition.name
            for definition in configuration.signal_definitions
            if definition.enabled
        }
        configured_signals = _configured_signals(configuration, signals)
        category_signal_names = {
            signal_name
            for category in categories
            for signal_name in category.signals
            if signal_name in configured_signals
        }
        signal_by_name = {
            name: signal
            for name, signal in configured_signals.items()
            if name in category_signal_names
        }
        permitted_signal_names = configured_signal_names
        scores: dict[str, float] = {}
        thresholds: dict[str, float] = {}
        qualifying: list[CategoryDefinition] = []
        contributing_names: set[str] = set()

        for category in categories:
            score, contributing = _score_category(
                category,
                signal_by_name,
                permitted_signal_names,
            )
            scores[category.category_id] = score
            thresholds[category.category_id] = category.evidence_threshold
            if score >= category.evidence_threshold and category.signals:
                qualifying.append(category)
                contributing_names.update(contributing)

        evidence = _evidence_reference(signal_by_name, contributing_names)
        if not qualifying:
            return _unknown_result(
                UnknownReason.INSUFFICIENT_EVIDENCE,
                scores=scores,
                thresholds=thresholds,
                evidence=evidence,
                signals=signal_by_name.values(),
                limitations=(USE_LIMITATION,),
            )

        if _has_conflict(qualifying):
            winner = _precedence_winner(qualifying, configuration.precedence_rules)
            if winner is None:
                candidates = ", ".join(category.category_id for category in qualifying)
                return _unknown_result(
                    UnknownReason.CONFLICTING_EVIDENCE,
                    scores=scores,
                    thresholds=thresholds,
                    evidence=evidence,
                    signals=signal_by_name.values(),
                    limitations=(
                        USE_LIMITATION,
                        f"Conflicting category candidates were retained: {candidates}.",
                    ),
                )
            primary = winner
        else:
            primary = qualifying[0]

        secondary = tuple(
            category.category_id
            for category in qualifying
            if category.category_id != primary.category_id
        )
        score = scores[primary.category_id]
        confidence = _confidence_band(score, configuration)
        thresholds = {
            **thresholds,
            "confidence_low": configuration.confidence_thresholds[ConfidenceBand.LOW.value],
            "confidence_medium": configuration.confidence_thresholds[ConfidenceBand.MEDIUM.value],
            "confidence_high": configuration.confidence_thresholds[ConfidenceBand.HIGH.value],
        }
        limitations = (
            USE_LIMITATION,
            "Categories describe likely or signal-consistent usage patterns, not confirmed intent, productivity, policy compliance, or employee characteristics.",
        )
        return ClassificationResult(
            primary_category=primary.category_id,
            secondary_categories=secondary,
            category_scores=scores,
            confidence_band=confidence,
            evidence_thresholds=thresholds,
            evidence_reference=evidence,
            limitations=limitations,
            applicability=_applicability(
                primary=True,
                secondary=bool(secondary),
                scores=True,
                confidence=True,
                evidence=evidence is not None,
                unknown=False,
            ),
        )


def classify(
    signal_set: Iterable[Signal],
    configuration: InsightConfiguration,
) -> ClassificationResult:
    """Classify signals with the deterministic usage-insights policy."""

    return DeterministicClassifier().classify(signal_set, configuration)


def _active_categories(configuration: InsightConfiguration) -> tuple[CategoryDefinition, ...]:
    seen: set[str] = set()
    result: list[CategoryDefinition] = []
    for category in configuration.categories:
        if (
            category.enabled
            and category.category_id not in seen
            and category.category_id != UsageCategory.INSUFFICIENT_EVIDENCE.value
        ):
            seen.add(category.category_id)
            result.append(category)
    return tuple(result)


def _configured_signals(
    configuration: InsightConfiguration,
    signals: Iterable[Signal],
) -> dict[str, Signal]:
    definitions = {
        definition.name: definition
        for definition in configuration.signal_definitions
        if definition.enabled
    }
    result: dict[str, Signal] = {}
    for signal in signals:
        definition = definitions.get(signal.name)
        if definition is None or signal.name in result:
            continue
        result[signal.name] = replace(
            signal,
            source_field_class=definition.source_field_class,
            provenance=definition.provenance,
        )
    return result


def _score_category(
    category: CategoryDefinition,
    signals: dict[str, Signal],
    permitted_signal_names: set[str],
) -> tuple[float, set[str]]:
    names = tuple(
        dict.fromkeys(
            signal_name
            for signal_name in category.signals
            if signal_name in permitted_signal_names
        )
    )
    if not names:
        return 0.0, set()
    contributing = {
        name
        for name in names
        if name in signals and _usable_signal(signals[name])
    }
    return len(contributing) / len(names), contributing


def _usable_signal(signal: Signal) -> bool:
    if signal.availability is not SignalAvailability.AVAILABLE or signal.value is None:
        return False
    return not isinstance(signal.value, float) or math.isfinite(signal.value)


def _threshold_error(
    configuration: InsightConfiguration,
    categories: Sequence[CategoryDefinition],
) -> Optional[str]:
    for category in categories:
        for field_name in ("score_min", "score_max", "evidence_threshold"):
            value = getattr(category, field_name)
            if not _finite_unit(value):
                return (
                    f"Category {category.category_id} has a non-finite or out-of-range "
                    f"{field_name}; classification was not produced."
                )
    thresholds = configuration.confidence_thresholds
    if not isinstance(thresholds, Mapping):
        return "Confidence thresholds are unavailable; classification was not produced."
    expected = {band.value for band in ConfidenceBand}
    if set(thresholds) != expected or any(
        not _finite_unit(thresholds.get(name)) for name in expected
    ):
        return "Confidence thresholds must be finite values in [0.00, 1.00]."
    if not (
        thresholds[ConfidenceBand.LOW.value]
        <= thresholds[ConfidenceBand.MEDIUM.value]
        <= thresholds[ConfidenceBand.HIGH.value]
    ):
        return "Confidence thresholds must be ordered low <= medium <= high."
    return None


def _finite_unit(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


def _has_conflict(categories: Sequence[CategoryDefinition]) -> bool:
    return len({category.category_id for category in categories} & _CONFLICTING_CATEGORIES) > 1


def _precedence_winner(
    categories: Sequence[CategoryDefinition],
    rules: Sequence[str],
) -> Optional[CategoryDefinition]:
    category_by_id = {category.category_id: category for category in categories}
    candidates = set(category_by_id)
    for rule in rules:
        if not isinstance(rule, str):
            continue
        normalized = rule.strip()
        if normalized.startswith(("prefer:", "precedence:")):
            winner = normalized.split(":", 1)[1].strip()
            if winner in candidates:
                return category_by_id[winner]
            continue

        separator = ">" if ">" in normalized else "_over_" if "_over_" in normalized else None
        if separator is None:
            continue
        winner, loser = (part.strip() for part in normalized.split(separator, 1))
        if winner in candidates and loser in candidates:
            return category_by_id[winner]
    return None


def _confidence_band(score: float, configuration: InsightConfiguration) -> ConfidenceBand:
    thresholds = configuration.confidence_thresholds
    if score >= thresholds[ConfidenceBand.HIGH.value]:
        return ConfidenceBand.HIGH
    if score >= thresholds[ConfidenceBand.MEDIUM.value]:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


def _evidence_reference(
    signals: dict[str, Signal],
    contributing_names: set[str],
) -> Optional[EvidenceReference]:
    names = tuple(sorted(contributing_names))
    if not names:
        return None
    selected = [signals[name] for name in names]
    field_classes = tuple(dict.fromkeys(signal.source_field_class for signal in selected))
    summaries = tuple(signal.summary for signal in selected if signal.summary)
    return EvidenceReference(
        signal_names=names,
        source_field_classes=field_classes,
        summaries=summaries,
        counts={name: 1 for name in names},
    )


def _unknown_result(
    reason: UnknownReason,
    *,
    scores: Optional[dict[str, float]] = None,
    thresholds: Optional[dict[str, float]] = None,
    evidence: Optional[EvidenceReference] = None,
    signals: Iterable[Signal] = (),
    limitations: tuple[str, ...],
) -> ClassificationResult:
    signal_values = tuple(signals)
    if evidence is None:
        available = {
            signal.name
            for signal in signal_values
            if _usable_signal(signal)
        }
        deduplicated: dict[str, Signal] = {}
        for signal in signal_values:
            if signal.name not in deduplicated:
                deduplicated[signal.name] = signal
        evidence = _evidence_reference(deduplicated, available)
    return ClassificationResult(
        category_scores=scores or {},
        unknown_reason=reason,
        evidence_thresholds=thresholds or {},
        evidence_reference=evidence,
        limitations=limitations,
        applicability=_applicability(
            primary=False,
            secondary=False,
            scores=bool(scores),
            confidence=False,
            evidence=evidence is not None,
            unknown=True,
        ),
    )


def _applicability(
    *,
    primary: bool,
    secondary: bool,
    scores: bool,
    confidence: bool,
    evidence: bool,
    unknown: bool,
) -> ApplicabilityMarkers:
    def marker(value: bool) -> Applicability:
        return Applicability.APPLICABLE if value else Applicability.NOT_APPLICABLE

    return ApplicabilityMarkers(
        primary_category=marker(primary),
        secondary_categories=marker(secondary),
        scores=marker(scores),
        confidence_band=marker(confidence),
        evidence_reference=marker(evidence),
        unknown_reason=marker(unknown),
    )


__all__ = ["USE_LIMITATION", "DeterministicClassifier", "classify"]
