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
from .cvar import calculate_cvar
from .evidence import ScenarioResult, build_evidence_matrix
from .robustness import block_bootstrap, run_path_scenarios
from .stress import calculate_historical_stress, calculate_position_shocks

__all__ = [
    "STANDARD_TABLES",
    "AnalysisBundle",
    "AnalysisContractError",
    "ScenarioResult",
    "block_bootstrap",
    "build_evidence_matrix",
    "calculate_attribution",
    "calculate_benchmark_statistics",
    "calculate_cvar",
    "calculate_historical_stress",
    "calculate_performance",
    "calculate_position_shocks",
    "read_analysis_table",
    "run_path_scenarios",
    "validate_analysis_bundle",
    "write_analysis_table",
]
