from __future__ import annotations

import math

import numpy as np


def block_bootstrap(
    returns: np.ndarray,
    block_size: int,
    paths: int,
    horizon: int,
    seed: int,
) -> np.ndarray:
    values = np.asarray(returns, dtype=np.float64)
    if values.ndim != 1 or values.size == 0 or not np.isfinite(values).all():
        raise ValueError("returns must be a finite one-dimensional array")
    if np.any(values <= -1.0):
        raise ValueError("returns must be greater than -100%")
    if block_size <= 0 or paths <= 0 or horizon <= 0:
        raise ValueError("block_size, paths and horizon must be positive")
    output = np.empty((paths, horizon), dtype=np.float64)
    generator = np.random.default_rng(seed)
    blocks = math.ceil(horizon / block_size)
    offsets = np.arange(block_size, dtype=np.int64)
    batch_size = min(256, paths)
    for first in range(0, paths, batch_size):
        count = min(batch_size, paths - first)
        starts = generator.integers(0, values.size, size=(count, blocks))
        indices = (starts[:, :, None] + offsets) % values.size
        output[first : first + count] = values[indices].reshape(count, -1)[:, :horizon]
    return output


def summarize_bootstrap(paths: np.ndarray) -> dict[str, float]:
    values = np.asarray(paths, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] == 0 or values.shape[1] == 0:
        raise ValueError("bootstrap paths must be a non-empty matrix")
    wealth = np.cumprod(1.0 + values, axis=1)
    peaks = np.maximum(1.0, np.maximum.accumulate(wealth, axis=1))
    max_drawdown = np.min(wealth / peaks - 1.0, axis=1)
    terminal = wealth[:, -1] - 1.0
    return {
        "probability_drawdown_over_20pct": float(np.mean(max_drawdown < -0.20)),
        "probability_drawdown_over_30pct": float(np.mean(max_drawdown < -0.30)),
        "median_terminal_return": float(np.median(terminal)),
    }
