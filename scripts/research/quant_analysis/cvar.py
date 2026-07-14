from __future__ import annotations

import numpy as np

from .evidence import ScenarioResult, evidence_digest


MIN_TAIL_OBSERVATIONS = 20.0


def _returns(values: np.ndarray) -> np.ndarray:
    normalized = np.asarray(values, dtype=np.float64)
    if normalized.ndim != 1 or normalized.size == 0 or not np.isfinite(normalized).all():
        raise ValueError("returns must be a finite one-dimensional array")
    if np.any(normalized <= -1.0):
        raise ValueError("returns must be greater than -100%")
    return normalized


def calculate_cvar(returns: np.ndarray, confidence: float) -> float:
    values = _returns(returns)
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between zero and one")
    ordered = np.sort(values)
    tail_mass = values.size * (1.0 - confidence)
    nearest = round(tail_mass)
    if np.isclose(tail_mass, nearest, rtol=0.0, atol=1e-12):
        tail_mass = float(nearest)
    whole = int(np.floor(tail_mass))
    fraction = tail_mass - whole
    total = float(np.sum(ordered[:whole]))
    if fraction > 0.0:
        total += fraction * float(ordered[whole])
    return max(0.0, -(total / tail_mass))


def rolling_compound_returns(returns: np.ndarray, *, window: int) -> np.ndarray:
    values = _returns(returns)
    if window <= 0 or values.size < window:
        raise ValueError("rolling compound window exceeds the available returns")
    windows = np.lib.stride_tricks.sliding_window_view(values, window)
    return np.prod(1.0 + windows, axis=1) - 1.0


def calculate_cvar_scenarios(returns: np.ndarray) -> tuple[ScenarioResult, ...]:
    values = _returns(returns)
    definitions = (
        ("cvar-1d-95", values, 0.95, 0.025),
        ("cvar-1d-99", values, 0.99, 0.04),
        ("cvar-5d-95", rolling_compound_returns(values, window=5), 0.95, 0.05),
    )
    results: list[ScenarioResult] = []
    for scenario_id, samples, confidence, threshold in definitions:
        tail_observations = float(samples.size * (1.0 - confidence))
        sufficient = tail_observations >= MIN_TAIL_OBSERVATIONS
        cvar = calculate_cvar(samples, confidence) if sufficient else None
        passed = cvar is not None and cvar <= threshold
        results.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="cvar",
                status=(
                    "evidence_insufficient"
                    if not sufficient
                    else ("pass" if passed else "fail")
                ),
                metrics={
                    "cvar": cvar,
                    "confidence": confidence,
                    "horizon_days": 5 if "5d" in scenario_id else 1,
                    "threshold": threshold,
                    "samples": int(samples.size),
                    "tail_observations": tail_observations,
                },
                input_sha256=evidence_digest(
                    {
                        "returns": values.tolist(),
                        "scenario_id": scenario_id,
                        "confidence": confidence,
                        "threshold": threshold,
                    }
                ),
                reasons=(
                    ("insufficient_tail_observations",)
                    if not sufficient
                    else (() if passed else ("cvar_threshold",))
                ),
            )
        )
    return tuple(results)
