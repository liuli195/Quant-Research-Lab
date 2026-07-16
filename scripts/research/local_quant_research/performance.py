from __future__ import annotations

import time
from dataclasses import dataclass
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
    seconds = time.perf_counter() - started
    if seconds >= PERFORMANCE_LIMIT_SECONDS:
        raise PerformanceGateError(
            f"{name}_performance_limit",
            f"{name} execution exceeded the 180 second limit",
        )
    return outcome, PerformanceSample(name, seconds, str(digest(outcome)))


def run_cold_warm(
    operation: Callable[[], T],
    *,
    digest: Callable[[T], str],
) -> tuple[T, PerformanceEvidence]:
    _, cold = _sample("cold", operation, digest)
    warm_outcome, warm = _sample("warm", operation, digest)
    if cold.digest != warm.digest:
        raise PerformanceGateError(
            "execution_digest_mismatch",
            "cold and warm execution digests differ",
        )
    return warm_outcome, PerformanceEvidence(cold, warm)
