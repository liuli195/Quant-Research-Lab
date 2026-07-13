"""Strategy-agnostic local quantitative research runner."""

from .contracts import RunConfig, RunResult, RunStatus
from .runner import load_run_config, run_project

__all__ = [
    "RunConfig",
    "RunResult",
    "RunStatus",
    "load_run_config",
    "run_project",
]
