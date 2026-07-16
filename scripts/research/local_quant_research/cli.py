from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    REPO_ROOT = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.research.local_quant_research.archive import promote_archive
else:
    REPO_ROOT = Path(__file__).resolve().parents[3]
    from .archive import promote_archive


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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
