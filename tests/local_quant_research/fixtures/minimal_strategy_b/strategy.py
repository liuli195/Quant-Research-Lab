from __future__ import annotations

import numpy as np

from . import STRATEGY_ID

from scripts.research.local_quant_research.contracts import (
    LedgerInput,
    OrderBuffer,
    OrderProgram,
    PreparedStrategy,
    StrategyDescriptor,
)


def _noop(*args: object) -> None:
    return None


class MinimalStrategyB:
    descriptor = StrategyDescriptor(
        strategy_id=STRATEGY_ID,
        contract_version="1",
        extension_names=(),
        accounting={},
    )

    def prepare(self, snapshot: object, config: object) -> PreparedStrategy:
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
                program_id="minimal-b",
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

    def followup_program(self, prepared: object, primary_run: object) -> None:
        return None

    def build_extensions(
        self,
        prepared: object,
        execution: object,
    ) -> tuple[object, ...]:
        return ()


MODULE = MinimalStrategyB()
