from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[3]
if __package__ in {None, ""}:
    sys.path.insert(0, str(REPO_ROOT))


class _BootstrapError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RUN_ID = re.compile(r"[0-9a-f]{64}")
_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _absolute_path(value: object) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise _BootstrapError("unsafe_frozen_inputs")
    path = Path(value)
    if not path.is_absolute():
        raise _BootstrapError("unsafe_frozen_inputs")
    return Path(os.path.abspath(path))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _is_reparse(path: Path, details: os.stat_result) -> bool:
    return (
        stat.S_ISLNK(details.st_mode)
        or bool(
            getattr(details, "st_file_attributes", 0)
            & _FILE_ATTRIBUTE_REPARSE_POINT
        )
        or bool(getattr(os.path, "isjunction", lambda _path: False)(path))
    )


def _require_plain_path(
    path: Path,
    *,
    kind: str,
    allow_missing: bool = False,
) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        final = index == len(parts) - 1
        try:
            details = os.lstat(current)
        except FileNotFoundError:
            if allow_missing and final:
                return
            raise _BootstrapError("unsafe_frozen_inputs") from None
        except OSError as exc:
            raise _BootstrapError("unsafe_frozen_inputs") from exc
        if _is_reparse(current, details):
            raise _BootstrapError("unsafe_frozen_inputs")
        if not final and not stat.S_ISDIR(details.st_mode):
            raise _BootstrapError("unsafe_frozen_inputs")
        if final and (
            (kind == "directory" and not stat.S_ISDIR(details.st_mode))
            or (kind == "file" and not stat.S_ISREG(details.st_mode))
        ):
            raise _BootstrapError("unsafe_frozen_inputs")


def _bootstrap_request(frozen_inputs: Path, staging: Path) -> Mapping[str, object]:
    request_path = _absolute_path(frozen_inputs)
    _require_plain_path(request_path, kind="file")
    execution_root = request_path.parent
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _BootstrapError("invalid_frozen_inputs") from exc
    if not isinstance(request, Mapping) or request.get("schema_version") != 2:
        raise _BootstrapError("invalid_frozen_inputs")
    expected_staging = _absolute_path(request.get("staging", ""))
    supplied_staging = _absolute_path(staging)
    if not _same_path(supplied_staging, expected_staging):
        raise _BootstrapError("staging_mismatch")
    project_id = request.get("project_id")
    run_id = request.get("run_id")
    attempt_id = request.get("attempt_id")
    if (
        not isinstance(project_id, str)
        or _PROJECT_ID.fullmatch(project_id) is None
        or not isinstance(run_id, str)
        or _RUN_ID.fullmatch(run_id) is None
        or not isinstance(attempt_id, str)
        or _ATTEMPT_ID.fullmatch(attempt_id) is None
    ):
        raise _BootstrapError("unsafe_frozen_inputs")
    output_root = Path(os.path.abspath(REPO_ROOT / ".local/quant-research"))
    project_root = output_root / project_id
    expected_execution = project_root / f".{run_id}.{attempt_id}.inputs"
    expected_staging_name = project_root / f".{run_id}.{attempt_id}.tmp"
    repository = _absolute_path(request.get("repository", ""))
    market_data = _absolute_path(request.get("market_data", ""))
    runtime_cache = _absolute_path(request.get("runtime_cache", ""))
    live_repository = _absolute_path(request.get("live_repository", ""))
    declared_output = _absolute_path(request.get("output_root", ""))
    if (
        not _same_path(live_repository, REPO_ROOT)
        or not _same_path(declared_output, output_root)
        or not _same_path(execution_root, expected_execution)
        or request_path.name != "request.json"
        or not _same_path(expected_staging, expected_staging_name)
        or not _same_path(repository, execution_root / "repository")
        or not _same_path(market_data, execution_root / "market-data")
        or not _same_path(runtime_cache, execution_root / "runtime-cache")
    ):
        raise _BootstrapError("unsafe_frozen_inputs")
    for path in (
        output_root,
        project_root,
        execution_root,
        repository,
        market_data,
        runtime_cache,
    ):
        _require_plain_path(path, kind="directory")
    _require_plain_path(expected_staging, kind="directory", allow_missing=True)
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
        frozen_repository = _absolute_path(bootstrap["repository"])
        runtime_cache = _absolute_path(bootstrap["runtime_cache"])
        numba_cache = runtime_cache / "numba"
        matplotlib_cache = runtime_cache / "matplotlib"
        numba_cache.mkdir()
        matplotlib_cache.mkdir()
        for name in ("TMP", "TEMP", "TMPDIR"):
            os.environ[name] = str(runtime_cache)
        os.environ["NUMBA_CACHE_DIR"] = str(numba_cache)
        os.environ["MPLCONFIGDIR"] = str(matplotlib_cache)
        os.environ["XDG_CACHE_HOME"] = str(runtime_cache)
        tempfile.tempdir = str(runtime_cache)
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
    except _BootstrapError as exc:
        print(
            json.dumps(
                {"status": "evidence_insufficient", "reasons": [exc.code]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"status": "failed", "reasons": ["frozen_bootstrap_failed"]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1

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
