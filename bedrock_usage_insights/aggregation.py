"""Aggregate reporting and minimum-group suppression boundary."""

from __future__ import annotations

from typing import Protocol, Sequence

from .models import AggregateInsightResult, InsightConfiguration, InsightsResult


class Aggregator(Protocol):
    """Port for aggregate arithmetic, cost labels, and suppression."""

    def aggregate(
        self,
        results: Sequence[InsightsResult],
        configuration: InsightConfiguration,
    ) -> tuple[AggregateInsightResult, ...]:
        """Produce aggregate-only rows while suppressing small groups."""
        ...


__all__ = ["Aggregator"]
