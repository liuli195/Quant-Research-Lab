from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
import time


_SKILL_SCRIPTS = Path(__file__).resolve().parent
_REPOSITORY = _SKILL_SCRIPTS.parents[3]
for path in (_SKILL_SCRIPTS, _REPOSITORY):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

reporting = importlib.import_module("quant_analysis.reporting")
unified_analysis = importlib.import_module("quant_analysis.unified_analysis")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the read-only standard quant analysis Skill backend"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--repository", type=Path, required=True)
    run.add_argument("--package", type=Path, action="append", required=True)
    run.add_argument("--analysis-plan", type=Path, required=True)
    run.add_argument("--benchmark-manifest", type=Path, required=True)
    report = commands.add_parser("report")
    report.add_argument("--repository", type=Path, required=True)
    report.add_argument("--workspace", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "run":
        started = time.perf_counter()
        result = unified_analysis.run_standard_analysis(
            args.repository,
            args.package,
            args.analysis_plan,
            args.benchmark_manifest,
        )
        print(
            json.dumps(
                {
                    "analysis_id": result["analysis_id"],
                    "status": "complete",
                    "next_action": result["next_action"],
                    "baseline": result["baseline"],
                    "evidence_matrix": result["evidence_matrix"],
                    "analysis_seconds": time.perf_counter() - started,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    delivery = reporting.write_standard_analysis_delivery(
        args.repository, args.workspace
    )
    print(json.dumps(delivery, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
