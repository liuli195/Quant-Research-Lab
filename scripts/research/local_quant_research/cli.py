from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[3]
if __package__ in {None, ""}:
    sys.path.insert(0, str(REPO_ROOT))


class _BootstrapError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _bootstrap_request(frozen_inputs: Path, staging: Path) -> Mapping[str, object]:
    request_path = Path(frozen_inputs).resolve()
    execution_root = request_path.parent
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _BootstrapError("invalid_frozen_inputs") from exc
    if not isinstance(request, Mapping) or request.get("schema_version") != 2:
        raise _BootstrapError("invalid_frozen_inputs")
    expected_staging = Path(str(request.get("staging", ""))).resolve()
    if Path(staging).resolve() != expected_staging:
        raise _BootstrapError("staging_mismatch")
    output_root = (REPO_ROOT / ".local/quant-research").resolve()
    repository = Path(str(request.get("repository", ""))).resolve()
    market_data = Path(str(request.get("market_data", ""))).resolve()
    runtime_cache = Path(str(request.get("runtime_cache", ""))).resolve()
    live_repository = Path(str(request.get("live_repository", ""))).resolve()
    if (
        live_repository != REPO_ROOT
        or not _inside(execution_root, output_root)
        or not _inside(expected_staging, output_root)
        or repository != (execution_root / "repository").resolve()
        or market_data != (execution_root / "market-data").resolve()
        or runtime_cache != (execution_root / "runtime-cache").resolve()
    ):
        raise _BootstrapError("unsafe_frozen_inputs")
    return request


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local quantitative research")
    subparsers = parser.add_subparsers(dest="action", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    promote = subparsers.add_parser("promote")
    promote.add_argument("--strategy-id", required=True)
    promote.add_argument("--run-id", required=True)
    promote.add_argument("--analysis-id", required=True)
    return parser


def promote_archive(
    repo_root: Path,
    strategy_id: str,
    run_id: str,
    analysis_id: str,
):
    if __package__ in {None, ""}:
        from scripts.research.local_quant_research.archive import (
            promote_archive as implementation,
        )
    else:
        from .archive import promote_archive as implementation

    return implementation(repo_root, strategy_id, run_id, analysis_id)


def _private_execute(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--frozen-inputs", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        bootstrap = _bootstrap_request(args.frozen_inputs, args.staging)
    except _BootstrapError as exc:
        print(
            json.dumps(
                {"status": "evidence_insufficient", "reasons": [exc.code]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    frozen_repository = Path(str(bootstrap["repository"])).resolve()
    for name in tuple(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            del sys.modules[name]
    guard_path = (
        frozen_repository
        / "scripts/research/local_quant_research/adapter_guard.py"
    )
    guard_spec = importlib.util.spec_from_file_location(
        "_frozen_local_quant_research_adapter_guard",
        guard_path,
    )
    if guard_spec is None or guard_spec.loader is None:
        print(
            json.dumps(
                {"status": "evidence_insufficient", "reasons": ["invalid_frozen_inputs"]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    guard = importlib.util.module_from_spec(guard_spec)
    guard_spec.loader.exec_module(guard)
    sys.modules["scripts.research.local_quant_research.adapter_guard"] = guard
    guard.install_access_guard(
        args.staging,
        execution_root=args.frozen_inputs.resolve().parent,
        repository_root=REPO_ROOT,
        venv_root=REPO_ROOT / ".venv",
        runtime_cache_root=Path(str(bootstrap["runtime_cache"])),
    )
    sys.path.insert(0, str(frozen_repository))
    importlib.invalidate_caches()
    from scripts.research.local_quant_research.contracts import StrategyEvidenceError
    from scripts.research.local_quant_research.performance import PerformanceGateError
    from scripts.research.local_quant_research.result_package import ResultContractError
    from scripts.research.local_quant_research.runner import (
        ConfigurationError,
        execute_frozen_inputs,
    )
    from scripts.research.local_quant_research.strategy_loader import (
        ConfigurationError as StrategyConfigurationError,
    )
    from scripts.research.market_data.storage import MarketDataError

    try:
        document = execute_frozen_inputs(args.frozen_inputs, args.staging)
    except (ConfigurationError, StrategyConfigurationError) as exc:
        document = {"status": "evidence_insufficient", "reasons": [exc.code]}
    except StrategyEvidenceError as exc:
        document = {"status": "evidence_insufficient", "reasons": [exc.code]}
    except MarketDataError as exc:
        message = str(exc).lower()
        if "missing" in message or "not found" in message:
            document = {
                "status": "evidence_insufficient",
                "reasons": ["market_data_missing"],
            }
        else:
            document = {"status": "failed", "reasons": ["market_data_failed"]}
    except PerformanceGateError as exc:
        document = {"status": "failed", "reasons": [exc.code]}
    except ResultContractError:
        document = {"status": "failed", "reasons": ["result_contract_failed"]}
    except PermissionError:
        document = {"status": "failed", "reasons": ["access_guard_violation"]}
    except Exception:
        document = {"status": "failed", "reasons": ["execution_failed"]}
    print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    return {
        "complete": 0,
        "failed": 1,
        "evidence_insufficient": 2,
    }[str(document["status"])]


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values[:1] == ["_execute"]:
        return _private_execute(values[1:])
    args = _parser().parse_args(values)
    if args.action == "promote":
        result = promote_archive(
            REPO_ROOT,
            args.strategy_id,
            args.run_id,
            args.analysis_id,
        )
        document = {
            "status": result.status,
            "reused": result.reused,
            "source": None if result.source is None else str(result.source),
            "target": None if result.target is None else str(result.target),
            "reasons": list(result.reasons),
        }
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
        return {"complete": 0, "conflict": 1, "failed": 2}[result.status]
    if __package__ in {None, ""}:
        from scripts.research.local_quant_research.runner import run_project
    else:
        from .runner import run_project

    result = run_project(args.config, repo_root=REPO_ROOT)
    print(json.dumps(result.to_document(), ensure_ascii=False, sort_keys=True))
    return {"complete": 0, "failed": 1, "evidence_insufficient": 2}[result.status]


if __name__ == "__main__":
    raise SystemExit(main())
