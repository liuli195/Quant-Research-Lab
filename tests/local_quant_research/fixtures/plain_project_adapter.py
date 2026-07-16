from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.research.market_data.query import open_snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-manifest", required=True)
    parser.add_argument("--market-data-root", required=True)
    parser.add_argument("--project-config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--code-sha256", required=True)
    parser.add_argument("--config-sha256", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    output = Path(args.output_dir)
    snapshot = open_snapshot(args.snapshot_id, root=Path(args.market_data_root))
    dates = sorted({str(row["date"]) for row in snapshot.rows})
    if len(dates) != 2:
        raise ValueError("plain fixture expects two dates")
    (output / "result.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot.snapshot_id,
                "run_id": args.run_id,
                "rows": len(snapshot.rows),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "project-status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "reason_codes": [],
                "next_action": "return_to_caller",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
