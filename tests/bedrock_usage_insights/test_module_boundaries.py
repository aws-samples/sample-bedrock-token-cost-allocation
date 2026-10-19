"""Smoke tests for the additive, dependency-free feature boundaries."""

import importlib


FEATURE_MODULES = (
    "configuration",
    "source_access",
    "normalization",
    "privacy",
    "signals",
    "classification",
    "prompt_assessment",
    "explainability",
    "aggregation",
    "run_control",
    "persistence",
)


def test_feature_boundaries_import_without_aws_clients():
    """Disabled mode can load the feature without creating AWS dependencies."""
    for module_name in FEATURE_MODULES:
        module = importlib.import_module(f"bedrock_usage_insights.{module_name}")
        assert module.__name__ == f"bedrock_usage_insights.{module_name}"


def test_feature_boundaries_expose_ports():
    """Each boundary exposes a named port for later implementations."""
    expected_ports = {
        "configuration": "ConfigurationStore",
        "source_access": "SourceReader",
        "normalization": "RecordNormalizer",
        "privacy": "PrivacyGate",
        "signals": "SignalExtractor",
        "classification": "Classifier",
        "prompt_assessment": "PromptAssessor",
        "explainability": "ResultBuilder",
        "aggregation": "Aggregator",
        "run_control": "RunController",
        "persistence": "PersistenceCoordinator",
    }
    for module_name, port_name in expected_ports.items():
        module = importlib.import_module(f"bedrock_usage_insights.{module_name}")
        assert hasattr(module, port_name)
