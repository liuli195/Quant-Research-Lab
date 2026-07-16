"""Strategy-agnostic analysis over standard JoinQuant-compatible results."""

from .benchmarks import BenchmarkAlignmentError, calculate_benchmark_statistics
from .cvar import calculate_cvar, rolling_compound_returns
from .evidence import ScenarioResult, build_evidence_matrix
from .robustness import block_bootstrap, summarize_bootstrap

__all__ = [
    "BenchmarkAlignmentError",
    "ScenarioResult",
    "block_bootstrap",
    "build_evidence_matrix",
    "calculate_benchmark_statistics",
    "calculate_cvar",
    "rolling_compound_returns",
    "summarize_bootstrap",
]
