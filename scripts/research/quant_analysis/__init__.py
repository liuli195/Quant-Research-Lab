"""Deterministic, strategy-agnostic quantitative analysis."""

from .attribution import calculate_attribution
from .benchmarks import calculate_benchmark_statistics
from .contracts import (
    STANDARD_TABLES,
    AnalysisBundle,
    AnalysisContractError,
    read_analysis_table,
    validate_analysis_bundle,
    write_analysis_table,
)
from .metrics import calculate_performance

__all__ = [
    "STANDARD_TABLES",
    "AnalysisBundle",
    "AnalysisContractError",
    "calculate_attribution",
    "calculate_benchmark_statistics",
    "calculate_performance",
    "read_analysis_table",
    "validate_analysis_bundle",
    "write_analysis_table",
]
