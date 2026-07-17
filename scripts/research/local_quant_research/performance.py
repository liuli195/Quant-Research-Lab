from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable, TypeVar


T = TypeVar("T")
PERFORMANCE_LIMIT_SECONDS = 180.0


class PerformanceGateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PerformanceSample:
    name: str
    seconds: float
    digest: str

    def to_document(self) -> dict[str, object]:
        return {
            "name": self.name,
            "seconds": self.seconds,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class PerformanceEvidence:
    cold: PerformanceSample
    warm: PerformanceSample

    def to_document(self) -> dict[str, object]:
        return {
            "cold": self.cold.to_document(),
            "warm": self.warm.to_document(),
        }


def _sample(
    name: str,
    operation: Callable[[], T],
    digest: Callable[[T], str],
) -> tuple[T, PerformanceSample]:
    started = time.perf_counter()
    outcome = operation()
    outcome_digest = str(digest(outcome))
    seconds = time.perf_counter() - started
    if seconds >= PERFORMANCE_LIMIT_SECONDS:
        raise PerformanceGateError(
            f"{name}_performance_limit",
            f"{name} execution exceeded the 180 second limit",
        )
    return outcome, PerformanceSample(name, seconds, outcome_digest)


def run_cold_warm(
    operation: Callable[[], T],
    *,
    digest: Callable[[T], str],
) -> tuple[T, PerformanceEvidence]:
    cold_outcome, cold = _sample("cold", operation, digest)
    del cold_outcome
    warm_outcome, warm = _sample("warm", operation, digest)
    if cold.digest != warm.digest:
        raise PerformanceGateError(
            "execution_digest_mismatch",
            "cold and warm execution digests differ",
        )
    return warm_outcome, PerformanceEvidence(cold, warm)


def include_shared_work(
    evidence: PerformanceEvidence,
    seconds: float,
) -> PerformanceEvidence:
    shared_seconds = float(seconds)
    if shared_seconds < 0:
        raise ValueError("shared performance duration must be non-negative")
    cold = replace(evidence.cold, seconds=evidence.cold.seconds + shared_seconds)
    warm = replace(evidence.warm, seconds=evidence.warm.seconds + shared_seconds)
    for sample in (cold, warm):
        if sample.seconds >= PERFORMANCE_LIMIT_SECONDS:
            raise PerformanceGateError(
                f"{sample.name}_performance_limit",
                f"{sample.name} execution exceeded the 180 second limit",
            )
    return PerformanceEvidence(cold, warm)
