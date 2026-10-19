"""Feature-owned staging and publication boundary.

Implementations of this port may use S3, Glue, or Athena, but those clients
are injected by the application layer. Importing this module performs no AWS
resource discovery or mutation.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from .models import AggregateInsightResult, AnalysisRun, InsightsResult


class PersistenceCoordinator(Protocol):
    """Port for run-scoped staging, manifest validation, and publication."""

    def prepare_run(self, run: AnalysisRun) -> None:
        """Create feature-owned staging state for a run."""
        ...

    def write_results(
        self,
        run: AnalysisRun,
        results: Sequence[InsightsResult],
        aggregates: Sequence[AggregateInsightResult],
    ) -> None:
        """Write only feature-owned derived outputs to staging."""
        ...

    def publish(self, run: AnalysisRun) -> AnalysisRun:
        """Validate and commit a run without changing source data."""
        ...

    def abort(self, run: AnalysisRun) -> AnalysisRun:
        """Mark staging unsuccessful without presenting it as complete."""
        ...


__all__ = ["PersistenceCoordinator"]
