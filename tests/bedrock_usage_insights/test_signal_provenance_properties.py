"""Property tests for Signal provenance and non-sensitive evidence."""

from datetime import date, datetime, timezone

from hypothesis import given, settings, strategies as st

from bedrock_usage_insights.models import (
    Applicability,
    ApplicabilityMarkers,
    AnalysisScope,
    CategoryDefinition,
    ClassificationResult,
    ContentAnalysisPolicy,
    ContentAnalysisState,
    ConfidenceBand,
    DIRECT_SIGNAL_NAMES,
    EvidenceReference,
    IdentityMetadata,
    InsightConfiguration,
    InvocationRecord,
    RedactionStatus,
    SensitiveContent,
    SignalAvailability,
    SignalProvenance,
    SourceFieldClass,
    CONTENT_SIGNAL_NAMES,
)
from bedrock_usage_insights.oracle import CanonicalSchemaOracle
from bedrock_usage_insights.prompt_assessment import assess_prompt
from bedrock_usage_insights.signals import extract_signals

UTC = timezone.utc
_DIRECT_NAMES = tuple(sorted(DIRECT_SIGNAL_NAMES))
_CONTENT_NAMES = tuple(sorted(CONTENT_SIGNAL_NAMES))


@st.composite
def signal_provenance_cases(draw: st.DrawFn):
    """Generate configured direct and content-derived Signals with redacted content."""

    direct_names = tuple(
        draw(
            st.lists(
                st.sampled_from(_DIRECT_NAMES),
                unique=True,
                min_size=1,
                max_size=len(_DIRECT_NAMES),
            )
        )
    )
    content_names = tuple(
        draw(
            st.lists(
                st.sampled_from(_CONTENT_NAMES),
                unique=True,
                min_size=1,
                max_size=len(_CONTENT_NAMES),
            )
        )
    )
    raw_prompt = draw(st.text(min_size=1, max_size=80)) + "__RAW_PROMPT__"
    raw_response = draw(st.text(min_size=1, max_size=80)) + "__RAW_RESPONSE__"
    signal_names = direct_names + content_names

    configuration = InsightConfiguration(
        configuration_version="property-7-v1",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_or_service="property-test",
        change_reason="signal provenance",
        analysis_scope=AnalysisScope(
            accounts=("123456789012",),
            regions=("us-east-1",),
            models=("model",),
            operations=("InvokeModel",),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        ),
        categories=(
            CategoryDefinition(
                category_id="provenance-check",
                display_name="Provenance check",
                description="A category used only for provenance validation.",
                signals=signal_names,
            ),
        ),
        enabled=True,
        content_analysis=ContentAnalysisPolicy(
            state=ContentAnalysisState.ENABLED,
            approved_accounts=("123456789012",),
            approved_regions=("us-east-1",),
            access_policy_reference="approved-policy",
            retention_days=30,
            max_preview_length=256,
        ),
    )
    record = InvocationRecord(
        source_request_id="request-1",
        source_timestamp_utc=datetime(2026, 1, 2, tzinfo=UTC),
        source_account="123456789012",
        source_region="us-east-1",
        model_id="model",
        operation="InvokeModel",
        analysis_run_id="run-1",
        configuration_version="property-7-v1",
        analysis_logic_version="1.0",
        input_token_count=12,
        output_token_count=24,
        cache_read_token_count=3,
        cache_write_token_count=4,
        stop_reason="end_turn",
        content_type="application/json",
        request_metadata={"team": "platform"},
        model_format="messages-v1",
        identity_metadata=IdentityMetadata(
            identity_arn="arn:aws:iam::123456789012:role/role-a",
            session_name="session-a",
            name="name-a",
            account_owner="owner-a",
        ),
        sensitive_content=SensitiveContent(
            prompt_preview=raw_prompt,
            response_preview=raw_response,
            redaction_status=RedactionStatus.REDACTED,
        ),
    )
    return configuration, record, raw_prompt, raw_response


def _classification_evidence(signals) -> EvidenceReference:
    """Build the content-free evidence projection used by a result contract."""

    contributing = tuple(
        signal for signal in signals if signal.availability is SignalAvailability.AVAILABLE
    )
    return EvidenceReference(
        signal_names=tuple(signal.name for signal in contributing),
        source_field_classes=tuple(signal.source_field_class for signal in contributing),
        counts={"contributing_signal_count": len(contributing)},
    )


# Feature: bedrock-usage-insights, Property 7: Signal provenance and non-sensitive evidence
# **Validates: Requirements 3.3, 6.3**
@given(signal_provenance_cases())
@settings(max_examples=100, deadline=None)
def test_property_7_signals_have_provenance_and_evidence_is_non_sensitive(case):
    configuration, record, raw_prompt, raw_response = case
    definitions = {
        definition.name: definition for definition in configuration.signal_definitions
    }
    signals = extract_signals(record, configuration)
    assert signals

    for signal in signals:
        assert isinstance(signal.name, str) and signal.name.strip()
        assert signal.name in configuration.categories[0].signals
        definition = definitions[signal.name]
        assert isinstance(signal.source_field_class, SourceFieldClass)
        assert signal.source_field_class is definition.source_field_class
        assert isinstance(signal.provenance, SignalProvenance)
        assert signal.provenance is definition.provenance
        assert signal.provenance in {
            SignalProvenance.DIRECT_OBSERVATION,
            SignalProvenance.CONTENT_DERIVED,
        }

    evidence = _classification_evidence(signals)
    classification = ClassificationResult(
        primary_category="provenance-check",
        category_scores={"provenance-check": 1.0},
        confidence_band=ConfidenceBand.HIGH,
        evidence_thresholds={"provenance-check": 0.5},
        evidence_reference=evidence,
        applicability=ApplicabilityMarkers(
            primary_category=Applicability.APPLICABLE,
            scores=Applicability.APPLICABLE,
            confidence_band=Applicability.APPLICABLE,
            evidence_reference=Applicability.APPLICABLE,
        ),
    )
    assert CanonicalSchemaOracle.validate_classification(classification, configuration) == ()
    assert set(evidence.signal_names) == {signal.name for signal in signals}
    assert all(name in configuration.categories[0].signals for name in evidence.signal_names)
    assert all(isinstance(field_class, SourceFieldClass) for field_class in evidence.source_field_classes)

    assessment = assess_prompt(record.sensitive_content, configuration)
    assert CanonicalSchemaOracle.validate_prompt_quality(assessment) == ()
    for dimension in assessment.dimensions.values():
        if dimension.evidence_reference is None:
            continue
        reference = dimension.evidence_reference
        assert all(name in CONTENT_SIGNAL_NAMES for name in reference.signal_names)
        assert all(
            field_class is SourceFieldClass.PERMITTED_CONTENT_DERIVED_FEATURE
            for field_class in reference.source_field_classes
        )
        assert all(
            definitions[name].provenance is SignalProvenance.CONTENT_DERIVED
            for name in reference.signal_names
        )

    serialized_evidence = repr(evidence.to_dict())
    serialized_assessment = repr(assessment.to_dict())
    assert raw_prompt not in serialized_evidence
    assert raw_response not in serialized_evidence
    assert raw_prompt not in serialized_assessment
    assert raw_response not in serialized_assessment
    assert "__RAW_PROMPT__" not in serialized_evidence + serialized_assessment
    assert "__RAW_RESPONSE__" not in serialized_evidence + serialized_assessment
