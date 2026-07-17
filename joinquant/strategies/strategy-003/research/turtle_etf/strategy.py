from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from scripts.research.local_quant_research.contracts import (
    ExecutionBundle,
    ExecutionRun,
    OrderProgram,
    PreparedStrategy,
    ResultExtension,
    StrategyDescriptor,
)

from ._attribution import build_turtle_attribution
from ._delayed import build_delayed_program
from ._kernel import prepare_turtle_strategy

if TYPE_CHECKING:
    from scripts.research.market_data.query import SnapshotView


@dataclass(frozen=True, slots=True)
class TurtleStrategyModule:
    descriptor: StrategyDescriptor

    def prepare(
        self,
        snapshot: SnapshotView,
        config: Mapping[str, object],
    ) -> PreparedStrategy:
        return prepare_turtle_strategy(snapshot, config)

    def followup_program(
        self,
        prepared: PreparedStrategy,
        primary_run: ExecutionRun,
    ) -> OrderProgram | None:
        return build_delayed_program(prepared, primary_run)

    def build_extensions(
        self,
        prepared: PreparedStrategy,
        execution: ExecutionBundle,
    ) -> tuple[ResultExtension, ...]:
        return (build_turtle_attribution(prepared, execution),)


MODULE = TurtleStrategyModule(
    descriptor=StrategyDescriptor(
        strategy_id="strategy-003",
        contract_version="strategy-module/1",
        extension_names=("turtle_etf",),
        accounting={"lot_size": 100, "cash_sharing": True},
    )
)
