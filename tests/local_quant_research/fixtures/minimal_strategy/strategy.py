from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Mapping

import numpy as np

from scripts.research.local_quant_research.contracts import (
    ExecutionBundle,
    ExecutionRun,
    LedgerInput,
    OrderBuffer,
    OrderProgram,
    PreparedStrategy,
    ResultExtension,
    StrategyDescriptor,
)

if TYPE_CHECKING:
    from scripts.research.market_data.query import SnapshotView


def _noop(*args: object) -> None:
    return None


class MinimalStrategy:
    descriptor = StrategyDescriptor(
        strategy_id="minimal-fixture",
        contract_version="1",
        source_files=(Path("strategy.py"),),
        extension_names=(),
        accounting={},
    )

    def prepare(
        self,
        snapshot: SnapshotView,
        config: Mapping[str, object],
    ) -> PreparedStrategy:
        ledger_input = LedgerInput(
            dates=np.array(["2026-01-05", "2026-01-06"], dtype="datetime64[D]"),
            symbols=("TEST",),
            close=np.ones((2, 1)),
            initial_cash=100_000.0,
            group_ids=np.zeros(1, dtype=np.int64),
            cash_sharing=True,
            frequency="1d",
        )
        orders = OrderBuffer(
            enabled=np.zeros(1, dtype=np.bool_),
            side=np.zeros(1, dtype=np.int8),
            size=np.zeros(1),
            price=np.zeros(1),
            fixed_fees=np.zeros(1),
            size_granularity=np.ones(1),
            allow_partial=np.zeros(1, dtype=np.bool_),
            priority=np.zeros(1, dtype=np.int64),
        )
        return PreparedStrategy(
            ledger_input=ledger_input,
            primary_program=OrderProgram(
                program_id="minimal",
                prepare_segment_nb=_noop,
                after_fill_nb=_noop,
                after_segment_nb=None,
                inputs=(),
                params=(),
                state=(),
                trace={},
                orders=orders,
            ),
            context=None,
        )

    def followup_program(
        self,
        prepared: PreparedStrategy,
        primary_run: ExecutionRun,
    ) -> OrderProgram | None:
        return None

    def build_extensions(
        self,
        prepared: PreparedStrategy,
        execution: ExecutionBundle,
    ) -> tuple[ResultExtension, ...]:
        return ()


MODULE = MinimalStrategy()
