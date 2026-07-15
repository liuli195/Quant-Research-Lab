from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

import pandas as pd

if __package__ in {None, ""}:
    REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
    sys.path.insert(0, str(REPOSITORY_ROOT))
else:
    REPOSITORY_ROOT = Path(__file__).resolve().parents[5]

from scripts.research.market_data.query import open_snapshot
from scripts.research.market_data.storage import MarketDataError

if __package__ in {None, ""}:
    RESEARCH_ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(RESEARCH_ROOT))
    from turtle_etf.result_adapter import ResultContractError
    from turtle_etf.single_scenario import (
        SingleScenarioError,
        execute_prepared_scenario,
        validate_single_scenario_config,
        write_project_status,
    )
    from turtle_etf.vectorbt_benchmark import PerformanceGateError
    from turtle_etf.vectorbt_inputs import prepare_simulation_inputs
else:
    from .result_adapter import ResultContractError
    from .single_scenario import (
        SingleScenarioError,
        execute_prepared_scenario,
        validate_single_scenario_config,
        write_project_status,
    )
    from .vectorbt_benchmark import PerformanceGateError
    from .vectorbt_inputs import prepare_simulation_inputs


class ProjectInputError(ValueError):
    """Raised when frozen project inputs cannot identify one scenario."""


def _load_json(path: Path, name: str) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectInputError(f"invalid_{name}") from exc
    if not isinstance(value, dict):
        raise ProjectInputError(f"invalid_{name}")
    return value


def _market_frames(
    rows: tuple[Mapping[str, object], ...],
    config: Mapping[str, object],
) -> dict[str, pd.DataFrame]:
    universe = config.get("universe")
    if not isinstance(universe, list) or not universe:
        raise ProjectInputError("invalid_universe")
    securities = tuple(str(item.get("security", "")) for item in universe if isinstance(item, Mapping))
    if len(securities) != len(universe) or any(not security for security in securities):
        raise ProjectInputError("invalid_universe")
    frame = pd.DataFrame([dict(row) for row in rows])
    if frame.empty or set(frame["security"].astype(str)) != set(securities):
        raise ProjectInputError("snapshot_universe_mismatch")
    return {
        security: frame.loc[frame["security"].astype(str) == security].copy()
        for security in securities
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one local vectorbt scenario")
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--market-data-root", type=Path, required=True)
    parser.add_argument("--project-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--code-sha256", required=True)
    parser.add_argument("--config-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        expected_manifest = (
            args.market_data_root / "snapshots" / f"{args.snapshot_id}.json"
        ).resolve()
        if args.snapshot_manifest.resolve() != expected_manifest:
            raise ProjectInputError("snapshot_manifest_identity_mismatch")
        config = _load_json(args.project_config, "project_config")
        validate_single_scenario_config(config)
        snapshot = open_snapshot(args.snapshot_id, root=args.market_data_root)
        frames = _market_frames(snapshot.rows, config)
        prepared = prepare_simulation_inputs(
            frames,
            config,
            corporate_actions=snapshot.corporate_actions,
            corporate_actions_digest=snapshot.corporate_actions_digest,
        )
        execute_prepared_scenario(
            prepared_inputs=prepared,
            config=config,
            output_dir=args.output_dir,
            run_id=args.run_id,
            snapshot_id=args.snapshot_id,
            code_sha256=args.code_sha256,
            config_sha256=args.config_sha256,
            code_path=Path(__file__),
        )
        write_project_status(
            args.output_dir,
            status="complete",
            reason_codes=(),
            next_action="return_to_caller",
        )
        return 0
    except (PerformanceGateError, ResultContractError, OSError):
        write_project_status(
            args.output_dir,
            status="failed",
            reason_codes=("local_vectorbt_execution_failed",),
        )
        return 1
    except (ProjectInputError, SingleScenarioError, MarketDataError, ValueError):
        write_project_status(
            args.output_dir,
            status="evidence_insufficient",
            reason_codes=("project_input_invalid",),
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
